"""Agent 运行时补全（改造 2）：finalize 定稿节点 + 检查点会话恢复 + HITL 写操作审批

守护点：
1. finalize 节点产出最终引用，并正确标记"知识库答不上来"的缺口（巡检原料）；
2. HITL **默认关闭**：模型拿不到高风险写工具，行为与改造前一致（零影响）；
3. HITL 开启后：写操作执行前一定中断（图挂起），审批前**不落库**；
4. 审批通过 → 从中断点恢复并真正执行写操作；审批拒绝 → 跳过写操作且不落库；
5. 检查点关闭时不会挂起（`interrupt` 需要检查点，缺一不可）；
6. thread_id 落 agent_runs（审批恢复要靠它）；
7. /api/agent/approve 的状态与权限校验（404 / 409 / 403）。

全程离线：LLM 用可编排假模型，检查点用内存存储，检索用 monkeypatch 规避。
"""
import pytest
from langchain_core.messages import AIMessage

from tests.helpers import body, expect, hdr, user_obj

from app.agent import runtime
from app.agent.router import INTENT_ACTION, classify_intent, tools_for_intent
from app.core.config import settings
from app.models import AgentRun, KnowledgeItem


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class ScriptedLLM:
    """可编排假模型：先按脚本发起工具调用，脚本用尽后返回回答文本"""

    def __init__(self, scripts=None, answer="（假模型回答）已按流程处理。"):
        self.scripts = list(scripts or [])
        self.answer = answer
        self.calls = 0
        self.bound_tool_names = []

    def bind_tools(self, tools):
        self.bound_tool_names = [getattr(t, "name", str(t)) for t in tools]
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.scripts:
            script = self.scripts.pop(0)
            if script is None:  # 显式表示"证据充分"
                return AIMessage(content="证据充分")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": script["name"],
                        "args": script.get("args", {}),
                        "id": script.get("id") or f"call_{self.calls}",
                    }
                ],
            )
        return AIMessage(content=self.answer)


def _refs(count=2, prefix="工具条目"):
    return [
        {
            "id": f"it-{i}",
            "title": f"{prefix}{i}",
            "content": f"条目{i}的内容：先做 A，再做 B。",
            "source_type": "knowledge_item",
            "score": round(0.9 - i * 0.1, 4),
        }
        for i in range(count)
    ]


WRITE_QUESTION = "帮我记一条待办，然后把校招出差准备的资料存进知识库"


@pytest.fixture
def patch_search(monkeypatch):
    """替换两阶段检索（避免 Qdrant / Embedding 网络调用）"""

    def _apply(refs):
        monkeypatch.setattr(
            "app.services.chat_service.search_relevant_items", lambda *a, **k: list(refs)
        )
        return refs

    return _apply


@pytest.fixture
def memory_checkpoint(monkeypatch):
    """让运行时使用内存检查点（同一实例，保证中断后能恢复）"""
    from langgraph.checkpoint.memory import MemorySaver

    saver = MemorySaver()
    monkeypatch.setattr(runtime, "get_checkpointer", lambda: saver)
    return saver


@pytest.fixture
def hitl_on(monkeypatch):
    """开启 HITL（配合 memory_checkpoint 使用）"""
    monkeypatch.setattr(settings, "AGENT_HITL_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_USE_CHECKPOINT", True)
    return True


def _prepare(db, username, question, **kwargs):
    """跑一次 prepare；显式传 history=[] 以跳过追问改写（保持离线）"""
    kwargs.setdefault("history", [])
    return runtime.prepare(user_obj(db, username), question, db, **kwargs)


def _src_title(test_name: str) -> str:
    """每个用例用独立的条目名，避免会话级共享测试库里的残留互相干扰"""
    return f"回归草稿·{test_name}"


def _write_script(title: str):
    """一段"让模型请求把资料入库成草稿条目"的规划脚本"""
    return [
        {
            "name": "create_draft_item",
            "args": {"title": title, "steps": ["先办出差审批", "再领物料"]},
        },
        None,
    ]


def _count_draft_items(db, title: str) -> int:
    return db.query(KnowledgeItem).filter(KnowledgeItem.title == title).count()


# ---------------------------------------------------------------------------
# 1. finalize 定稿节点
# ---------------------------------------------------------------------------

def test_finalize_produces_citations(db, ctx, patch_search):
    patch_search(_refs(2))
    out = _prepare(db, "regr_admin", "校招出差前需要做哪些准备", plan_llm=ScriptedLLM([]))

    assert not out.error, out.error
    assert len(out.citations) == 2, "定稿应产出 2 条引用"
    assert out.citations == out.sources, "citations 与 sources 必须一致（前端兼容）"
    assert out.unanswered is False
    assert any("定稿" in n for n in out.notes), out.notes


def test_finalize_marks_gap_when_no_evidence(db, ctx, patch_search):
    patch_search([])
    out = _prepare(db, "regr_admin", "一个知识库里完全不存在的问题", plan_llm=ScriptedLLM([]))

    assert out.unanswered is True, "无证据的业务问题应标记为知识库缺口"
    assert out.sources == []
    assert out.short_circuit == runtime.NO_RESULT_TEXT
    assert any("缺口" in n for n in out.notes), out.notes


def test_finalize_smalltalk_has_no_citations(db, ctx):
    out = _prepare(db, "regr_admin", "你好", plan_llm=ScriptedLLM([]))

    assert out.intent == "smalltalk"
    assert out.citations == []
    assert any("闲聊" in n for n in out.notes), out.notes


# ---------------------------------------------------------------------------
# 2. HITL 默认关闭 —— 零影响
# ---------------------------------------------------------------------------

def test_hitl_disabled_by_default_no_write_tools(db, ctx):
    assert settings.AGENT_HITL_ENABLED is False, "默认必须关闭（否则会改变既有行为）"

    admin = user_obj(db, "regr_admin")
    names = {t.name for t in tools_for_intent(INTENT_ACTION, admin)}
    assert "create_draft_item" not in names, "HITL 关闭时不应把写工具交给模型"

    with_hitl = {t.name for t in tools_for_intent(INTENT_ACTION, admin, allow_hitl_writes=True)}
    assert "create_draft_item" in with_hitl, "HITL 开启时才放开写工具"


def test_write_question_is_classified_as_action():
    assert classify_intent(WRITE_QUESTION) == INTENT_ACTION
    # 设计方案里 action 意图的典型表述（"存进知识库"）必须能触发
    assert classify_intent("帮我把校招出差准备的资料存进知识库") == INTENT_ACTION
    assert classify_intent("把这批资料整理成条目") == INTENT_ACTION


# ---------------------------------------------------------------------------
# 3./4. HITL 中断 → 审批 → 恢复
# ---------------------------------------------------------------------------

def test_hitl_interrupts_before_write(db, ctx, memory_checkpoint, hitl_on, patch_search):
    patch_search(_refs(1))
    title = _src_title("interrupts_before_write")
    out = _prepare(
        db, "regr_admin", WRITE_QUESTION,
        plan_llm=ScriptedLLM(_write_script(title)), allow_hitl=True,
    )

    assert out.waiting_approval is True, f"应停在审批上；notes={out.notes}"
    assert out.thread_id, "必须返回 thread_id 才能恢复"
    assert [w["tool"] for w in out.pending_writes] == ["create_draft_item"]
    assert _count_draft_items(db, title) == 0, "未审批前绝不能落库"
    assert any("待人工审批" in n for n in out.notes), out.notes


def test_hitl_approve_executes_write(db, ctx, memory_checkpoint, hitl_on, patch_search):
    patch_search(_refs(1))
    title = _src_title("approve_executes_write")
    llm = ScriptedLLM(_write_script(title))
    out = _prepare(db, "regr_admin", WRITE_QUESTION, plan_llm=llm, allow_hitl=True)
    assert out.waiting_approval is True

    admin = user_obj(db, "regr_admin")
    resumed = runtime.resume_run(
        admin, db,
        thread_id=out.thread_id,
        intent=out.intent,
        decision={"approved": True},
        plan_llm=llm,
    )

    assert not resumed.error, resumed.error
    assert resumed.waiting_approval is False
    assert _count_draft_items(db, title) == 1, "审批通过后应真正落库"
    assert any("已获批准并执行" in n for n in resumed.notes), resumed.notes
    assert not resumed.short_circuit


def test_hitl_reject_skips_write(db, ctx, memory_checkpoint, hitl_on, patch_search):
    patch_search(_refs(1))
    title = _src_title("reject_skips_write")
    llm = ScriptedLLM(_write_script(title))
    out = _prepare(db, "regr_admin", WRITE_QUESTION, plan_llm=llm, allow_hitl=True)
    assert out.waiting_approval is True

    admin = user_obj(db, "regr_admin")
    resumed = runtime.resume_run(
        admin, db,
        thread_id=out.thread_id,
        intent=out.intent,
        decision={"approved": False},
        plan_llm=llm,
    )

    assert not resumed.error, resumed.error
    assert resumed.waiting_approval is False
    assert _count_draft_items(db, title) == 0, "审批拒绝后绝不能落库"
    assert any("未获批准" in n for n in resumed.notes), resumed.notes


def test_no_interrupt_when_checkpoint_disabled(db, ctx, patch_search):
    """未开启 HITL/检查点时不会挂起：写工具压根不在白名单里"""
    patch_search(_refs(1))
    title = _src_title("no_interrupt")
    out = _prepare(
        db, "regr_admin", WRITE_QUESTION,
        plan_llm=ScriptedLLM(_write_script(title)), allow_hitl=True,
    )
    assert out.waiting_approval is False
    assert _count_draft_items(db, title) == 0


# ---------------------------------------------------------------------------
# 5. thread_id 落库
# ---------------------------------------------------------------------------

def test_thread_id_recorded_in_agent_run(db, ctx, memory_checkpoint, hitl_on, patch_search):
    patch_search(_refs(1))
    title = _src_title("thread_id_recorded")
    out = _prepare(
        db, "regr_admin", WRITE_QUESTION,
        plan_llm=ScriptedLLM(_write_script(title)), allow_hitl=True,
    )
    assert out.waiting_approval is True

    admin = user_obj(db, "regr_admin")
    run = runtime.record_run(db, admin.id, WRITE_QUESTION, out, mode="sync")

    assert run is not None
    assert run.thread_id == out.thread_id
    assert run.status == "awaiting_approval", f"状态应记为待审批，实际 {run.status}"


# ---------------------------------------------------------------------------
# 6. 审批接口的状态与权限校验
# ---------------------------------------------------------------------------

def _make_run(db, username, status, thread_id="t-fake"):
    user = user_obj(db, username)
    row = AgentRun(
        user_id=user.id,
        question="待审批问题",
        intent=INTENT_ACTION,
        mode="sync",
        status=status,
        thread_id=thread_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_approve_unknown_run_returns_404(client, db, ctx):
    r = client.post(
        "/api/agent/approve/not-exist-id",
        json={"approved": True},
        headers=hdr(ctx["admin"]["token"]),
    )
    expect(r, 404, "审批不存在的运行")


def test_approve_non_pending_run_returns_409(client, db, ctx):
    row = _make_run(db, "regr_ua", status="ok")
    r = client.post(
        f"/api/agent/approve/{row.id}",
        json={"approved": True},
        headers=hdr(ctx["admin"]["token"]),
    )
    expect(r, 409, "审批非待审批状态的运行")
    assert "无需审批" in body(r)["detail"]


def test_approve_others_run_returns_403(client, db, ctx):
    row = _make_run(db, "regr_ua", status="awaiting_approval")
    # 管理员不是该运行的所有者，且非超管 → 拒绝
    r = client.post(
        f"/api/agent/approve/{row.id}",
        json={"approved": True},
        headers=hdr(ctx["admin"]["token"]),
    )
    expect(r, 403, "审批他人运行")
    assert "无权" in body(r)["detail"]


def test_approve_requires_staff(client, db, ctx):
    row = _make_run(db, "regr_ua", status="awaiting_approval")
    r = client.post(
        f"/api/agent/approve/{row.id}",
        json={"approved": True},
        headers=hdr(ctx["ua"]["token"]),
    )
    assert r.status_code in (401, 403), f"普通用户不应能审批，实际 {r.status_code}"


def test_agent_status_exposes_hitl_config(client, ctx):
    r = client.get("/api/agent/status", headers=hdr(ctx["admin"]["token"]))
    expect(r, 200, "查询 Agent 状态")
    data = body(r)
    assert "hitl_enabled" in data and "hitl_tools" in data
    assert isinstance(data["hitl_tools"], list)
