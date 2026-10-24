"""Agent 图与灰度开关回归（M1）

守护点：
1. 分诊规则：闲聊精确匹配、写操作/流程关键词、默认落到业务问答（保守不误伤）；
2. 工具白名单：按意图收窄；管理员专属工具在任何意图下都不暴露给模型；
3. 图行为：闲聊跳过检索；业务问题必有一次确定性预检索（保证不劣化）；
   规划模型主动补检索形成循环；越权工具与重复检索被拦截；
4. 预算与降级：预算耗尽/规划超时/规划报错/模型不支持工具调用，四种情况都不失败；
5. 灰度开关：off 行为与改造前一致；on 走 Agent 但 SSE 协议不变；shadow 后台记录轨迹；
6. 轨迹落库与查询接口（含权限）。

全程离线：LLM 用可编排的假模型，向量检索用 monkeypatch 规避。
"""
import json
import time

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from tests.helpers import body, expect, hdr, user_obj

from app.agent import runtime
from app.agent.router import classify_intent, should_retrieve, tools_for_intent
from app.core.config import settings
from app.models import AgentRun, Memo


# ---------------------------------------------------------------------------
# 测试替身：可编排的假模型
# ---------------------------------------------------------------------------

class FakeLLM:
    """假模型：规划阶段按预置脚本发起工具调用，生成阶段返回固定文本

    判定规则（不依赖提示词文本，避免提示词微调就把测试打挂）：
    还有未消费的 plan_responses → 这是规划调用；计划用完了 → 返回回答文本。
    """

    def __init__(
        self,
        plan_responses=None,
        answer="（假模型回答）操作要点如下。",
        supports_tools=True,
        fail_invoke=False,
        plan_delay=0.0,
    ):
        self.plan_responses = list(plan_responses or [])
        self.answer = answer
        self.supports_tools = supports_tools
        self.fail_invoke = fail_invoke
        self.plan_delay = plan_delay
        self.invoke_calls = []
        self.stream_calls = 0
        self.bound_tool_names = None

    def bind_tools(self, tools):
        if not self.supports_tools:
            raise AttributeError("this model does not support tool calling")
        self.bound_tool_names = [getattr(t, "name", str(t)) for t in tools]
        return self

    def invoke(self, messages):
        self.invoke_calls.append(messages)
        # 首次 invoke 一定是规划调用（生成在跑图之后）→ 用它模拟规划慢/超时
        if len(self.invoke_calls) == 1 and self.plan_delay:
            time.sleep(self.plan_delay)
        if self.plan_responses:
            script = self.plan_responses.pop(0)
            if script is None:  # 显式表示"证据充分"
                return AIMessage(content="证据充分")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": script["name"],
                        "args": script.get("args", {}),
                        "id": f"call_{len(self.invoke_calls)}",
                    }
                ],
            )
        if self.fail_invoke:
            raise RuntimeError("模型服务不可用")
        return AIMessage(content=self.answer)

    async def astream(self, messages):
        self.stream_calls += 1
        if self.fail_invoke:
            raise RuntimeError("模型服务不可用")
        step = 6
        for i in range(0, len(self.answer), step):
            yield AIMessageChunk(content=self.answer[i : i + step])


@pytest.fixture
def install_llm(monkeypatch):
    """把 Agent 运行时使用的模型替换为假模型"""

    def _install(llm: FakeLLM) -> FakeLLM:
        # 同时替换两个模块里的引用：runtime 走第一个，回退到固定管线时走第二个（chat_service）
        monkeypatch.setattr("app.services.llm_service.get_llm_for_user", lambda user: llm)
        monkeypatch.setattr("app.services.chat_service.get_llm_for_user", lambda user: llm)
        return llm

    return _install


@pytest.fixture
def patch_search(monkeypatch):
    """替换两阶段检索（避免 Qdrant / Embedding 网络调用）"""

    def _apply(refs):
        monkeypatch.setattr(
            "app.services.chat_service.search_relevant_items", lambda *a, **k: list(refs)
        )
        return refs

    return _apply


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


def _sse_text(response_text: str) -> str:
    """把 SSE 响应里的正文片段拼回完整文本（跳过事件 JSON 与结束标记）

    流式输出是分片下发的，直接断言整句会失败——必须先把片段拼起来。
    """
    pieces = []
    for line in response_text.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            pieces.append(payload)
            continue
        if not isinstance(parsed, dict):  # JSON 字符串也算正文
            pieces.append(parsed)
    return "".join(pieces)


def _run(db, username, question, **kwargs):
    """以真实用户身份跑一次 prepare"""
    user = user_obj(db, username)
    return runtime.prepare(user, question, db, **kwargs)


# ---------------------------------------------------------------------------
# 1. 分诊规则
# ---------------------------------------------------------------------------

def test_classify_intent_rules():
    assert classify_intent("你好") == "smalltalk"
    assert classify_intent("谢谢！") == "smalltalk"
    assert classify_intent("你是谁") == "smalltalk"

    assert classify_intent("校招出差前需要做哪些准备") == "kb_qa"
    # 寒暄 + 业务问题 → 仍是业务问题（保守原则：宁多检索，不误判为闲聊）
    assert classify_intent("你好，校招出差前要准备什么") == "kb_qa"
    assert classify_intent("要准备哪些材料呢") == "kb_qa"

    assert classify_intent("校招流程下一步做什么") == "flow_nav"
    assert classify_intent("接下来该做什么") == "flow_nav"

    assert classify_intent("帮我记一条待办：明天交材料") == "action"
    assert classify_intent("提醒我周三联系候选人") == "action"


def test_should_retrieve_by_intent():
    assert should_retrieve("kb_qa") is True
    assert should_retrieve("flow_nav") is True
    assert should_retrieve("smalltalk") is False
    assert should_retrieve("action") is False


def test_tools_for_intent_whitelist(db, ctx):
    admin = user_obj(db, "regr_admin")
    normal = user_obj(db, "regr_ua")

    assert tools_for_intent("smalltalk", normal) == []

    kb_names = {t.name for t in tools_for_intent("kb_qa", normal)}
    assert "knowledge_search" in kb_names
    assert "document_search" in kb_names and "flow_next_step" in kb_names

    action_names = {t.name for t in tools_for_intent("action", normal)}
    assert action_names == {"memo_list", "memo_create", "knowledge_search"}

    # 管理员专属与高风险写工具在任何意图下都不交给模型（M1 不开放条目写操作）
    forbidden = {"create_draft_item", "update_item", "stats_snapshot"}
    for intent in ("smalltalk", "kb_qa", "flow_nav", "action"):
        names = {t.name for t in tools_for_intent(intent, admin)}
        assert not (names & forbidden), f"{intent} 不应开放 {names & forbidden}"


# ---------------------------------------------------------------------------
# 2. 图行为
# ---------------------------------------------------------------------------

def test_smalltalk_skips_retrieval_and_answers(db, ctx, install_llm, patch_search, monkeypatch):
    """闲聊不检索、不给工具，且不会走到"知识库暂无相关内容"""
    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr("app.services.chat_service.search_relevant_items", _spy)
    llm = install_llm(FakeLLM())

    outcome = _run(db, "regr_ua", "你好呀")

    assert outcome.intent == "smalltalk"
    assert outcome.sources == []
    assert called["n"] == 0, "闲聊不应触发检索"
    assert outcome.tool_calls == 0
    assert outcome.notes[0].startswith("分诊：寒暄")
    # 闲聊走的是 route → draft 的近路，根本不经过检索节点
    assert any("闲聊回应" in n for n in outcome.notes)
    assert outcome.short_circuit == "", "闲聊要正常回答，不能走兜底话术"
    # 起草用的是闲聊提示词，而不是"只依据知识库"的约束
    system_content = outcome.answer_messages[0].content
    assert "不需要检索知识库" in system_content


def test_kb_qa_prefetch_establishes_evidence(db, ctx, install_llm, patch_search):
    """业务问题必有一次确定性预检索（证据基线），模型说"证据充分"即起草"""
    patch_search(_refs(3))
    llm = install_llm(FakeLLM())

    outcome = _run(db, "regr_ua", "校招出差前需要做哪些准备")

    assert outcome.ok
    assert outcome.intent == "kb_qa"
    assert outcome.tool_calls == 1, "预检索恰好一次"
    assert [s["title"] for s in outcome.sources] == ["工具条目0", "工具条目1", "工具条目2"]
    assert any("预检索：命中 3 条" in n for n in outcome.notes)
    assert any("规划：证据充分" in n for n in outcome.notes)
    assert outcome.trace and outcome.trace[0]["tool"] == "knowledge_search"
    # 规划提示词里带上了证据摘要，模型知道手上有什么
    plan_human = llm.invoke_calls[0][-1].content
    assert "工具条目0" in plan_human


def test_agent_loop_executes_model_requested_tool(client, db, ctx, install_llm, patch_search):
    """规划模型主动补检索 → 图进入 act → 执行真实工具 → 回到规划 → 起草"""
    patch_search(_refs(2))
    token = ctx["admin"]["token"]
    flow = client.post("/api/flows", json={"name": "Agent循环测试流程"}, headers=hdr(token)).json()
    client.post(
        f"/api/flows/{flow['id']}/nodes",
        json={"name": "简历初筛", "order": 1, "description": "收集简历"},
        headers=hdr(token),
    )
    llm = install_llm(
        FakeLLM(
            plan_responses=[
                {"name": "list_flows", "args": {"keyword": "Agent循环测试流程"}},
                None,  # 第二轮：证据充分
            ]
        )
    )

    outcome = _run(db, "regr_admin", "Agent循环测试流程有哪些节点")

    assert outcome.ok
    assert any("需补充证据 → 调用 list_flows" in n for n in outcome.notes)
    assert any(step["tool"] == "list_flows" and step["ok"] for step in outcome.trace)
    assert outcome.tool_calls == 2, "预检索 1 次 + 模型补检索 1 次"
    assert llm.bound_tool_names is not None and "knowledge_search" in llm.bound_tool_names


def test_act_blocks_tool_outside_intent_whitelist(db, ctx, install_llm, patch_search):
    """业务问答意图下模型想写备忘录 → 被拦截，且不产生任何数据"""
    patch_search(_refs(1))
    install_llm(
        FakeLLM(
            plan_responses=[
                {"name": "memo_create", "args": {"title": "模型擅自写入的待办"}},
                None,
            ]
        )
    )

    before = db.query(Memo).filter(Memo.title == "模型擅自写入的待办").count()
    outcome = _run(db, "regr_ua", "校招出差前需要做哪些准备")
    after = db.query(Memo).filter(Memo.title == "模型擅自写入的待办").count()

    assert before == after == 0, "越权工具不得执行"
    assert any("拦截不可用工具：memo_create" in n for n in outcome.notes)
    assert not any(step["tool"] == "memo_create" for step in outcome.trace)


def test_act_skips_duplicate_retrieval(db, ctx, install_llm, patch_search):
    """模型把预检索用过的查询原样再发一次 → 直接跳过，不浪费一次检索"""
    patch_search(_refs(2))
    question = "校招出差前需要做哪些准备"
    install_llm(
        FakeLLM(
            plan_responses=[
                {"name": "knowledge_search", "args": {"query": question}},
                None,
            ]
        )
    )

    outcome = _run(db, "regr_ua", question)

    assert any("跳过重复检索：knowledge_search" in n for n in outcome.notes)
    searches = [s for s in outcome.trace if s["tool"] == "knowledge_search"]
    assert len(searches) == 1, "重复查询不应真的执行"
    assert outcome.tool_calls == 1


def test_fast_path_skips_plan_llm_when_evidence_sufficient(db, ctx, install_llm, patch_search):
    """证据充足（命中数达标）→ 跳过规划模型调用，直接起草（省一次模型调用，降低首字延迟）"""
    patch_search(_refs(6))
    llm = install_llm(
        FakeLLM(plan_responses=[{"name": "list_flows", "args": {}}])  # 有脚本也不该被用到
    )

    outcome = _run(db, "regr_ua", "校招出差前需要做哪些准备")

    assert any("证据充足" in n and "跳过规划" in n for n in outcome.notes)
    assert llm.invoke_calls == [], "快路径下不应调用规划模型"
    assert outcome.tool_calls == 1, "只应有预检索那一次"
    assert len(outcome.sources) == 6


def test_budget_exhaustion_stops_loop(db, ctx, install_llm, patch_search, monkeypatch):
    """预算耗尽 → 停止补检索，用已有证据起草"""
    monkeypatch.setattr(settings, "AGENT_MAX_TOOL_CALLS", 1)
    patch_search(_refs(2))
    llm = install_llm(
        FakeLLM(
            plan_responses=[
                {"name": "list_flows", "args": {}},
                {"name": "memo_list", "args": {}},
            ]
        )  # 本应第二轮再调一次，但预算已耗尽
    )

    outcome = _run(db, "regr_ua", "校招出差前需要做哪些准备")

    assert any("预算/超时到达上限" in n for n in outcome.notes)
    assert outcome.tool_calls == 1
    assert llm.invoke_calls == [], "预算耗尽时不应再调用规划模型"
    assert outcome.sources, "已有证据仍要用于起草"


# ---------------------------------------------------------------------------
# 3. 降级路径（四种失败都不该让用户看到报错）
# ---------------------------------------------------------------------------

def test_plan_failure_degrades_with_existing_evidence(db, ctx, install_llm, patch_search):
    patch_search(_refs(2))
    install_llm(FakeLLM(fail_invoke=True))

    outcome = _run(db, "regr_ua", "校招出差前需要做哪些准备", history=[])  # 不生成回答

    assert outcome.error == ""
    assert outcome.sources, "规划失败也要用预检索的证据作答"
    assert any("规划调用失败" in n for n in outcome.notes)
    assert any("降级" in n for n in outcome.notes)


def test_plan_timeout_degrades(db, ctx, install_llm, patch_search, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_PLAN_TIMEOUT_SECONDS", 1)
    patch_search(_refs(1))
    install_llm(FakeLLM(plan_delay=3.0))

    started = time.perf_counter()
    outcome = _run(db, "regr_ua", "校招出差前需要做哪些准备", history=[])
    elapsed = time.perf_counter() - started

    assert any("规划超时" in n for n in outcome.notes)
    assert elapsed < 2.5, f"超时应立即降级，实际耗时 {elapsed:.1f}s"
    assert outcome.short_circuit == "" and outcome.ok


def test_model_without_tool_calling_still_works(db, ctx, install_llm, patch_search):
    """模型不支持 function calling → 降级为"仅预检索"，答案依然带引用"""
    patch_search(_refs(2))
    install_llm(FakeLLM(supports_tools=False))

    outcome = _run(db, "regr_ua", "校招出差前需要做哪些准备")

    assert outcome.ok and outcome.sources
    assert any("模型不支持工具调用" in n for n in outcome.notes)


def test_no_evidence_short_circuits_with_same_text(db, ctx, install_llm, patch_search):
    patch_search([])
    install_llm(FakeLLM())

    outcome = _run(db, "regr_ua", "这个问题知识库里肯定没有")

    assert outcome.short_circuit == runtime.NO_RESULT_TEXT
    assert outcome.short_circuit == "抱歉，知识库中暂无相关内容。"
    assert outcome.sources == []


def test_graph_failure_marks_error_for_fallback(db, ctx, install_llm, patch_search, monkeypatch):
    """图执行异常 → 标记 error（由上层回落到固定管线），不向调用方抛异常"""
    patch_search(_refs(1))
    install_llm(FakeLLM())

    def _boom(*a, **k):
        raise RuntimeError("图构建失败")

    monkeypatch.setattr("app.agent.runtime.build_agent_graph", _boom)

    outcome = _run(db, "regr_ua", "校招出差前需要做哪些准备")

    assert outcome.error and "图构建失败" in outcome.error
    assert any("降级" in n for n in outcome.notes)
    assert outcome.sources == []


# ---------------------------------------------------------------------------
# 4. 灰度开关（API 层）
# ---------------------------------------------------------------------------

def test_chat_stream_off_mode_unchanged(client, ctx, monkeypatch):
    """off 模式：完全走原管线（这里用"无检索结果"场景验证兜底话术一字不差）"""
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    monkeypatch.setattr("app.services.chat_service.search_relevant_items", lambda *a, **k: [])

    r = client.post("/api/chat/stream", json={"question": "你好"}, headers=hdr(ctx["ua"]["token"]))

    expect(r, 200, "off 模式流式问答")
    assert "抱歉，知识库中暂无相关内容。" in r.text
    assert "[DONE]" in r.text
    assert '"type"' not in r.text, "off 模式不应出现 Agent 事件"


def test_chat_stream_on_mode_uses_agent_but_keeps_protocol(
    client, ctx, install_llm, patch_search, monkeypatch
):
    """on 模式：走 Agent，但 SSE 协议与固定管线一致（前端零改动）"""
    monkeypatch.setattr(settings, "AGENT_MODE", "on")
    patch_search(_refs(2))
    install_llm(FakeLLM(answer="先办出差审批，再领物料。"))

    r = client.post(
        "/api/chat/stream",
        json={"question": "校招出差前需要做哪些准备"},
        headers=hdr(ctx["ua"]["token"]),
    )

    expect(r, 200, "on 模式流式问答")
    assert '"references"' in r.text and "工具条目0" in r.text
    assert _sse_text(r.text) == "先办出差审批，再领物料。"
    assert '"type"' not in r.text, "兼容协议里不应混入富事件"


def test_chat_stream_on_mode_falls_back_on_agent_error(
    client, ctx, install_llm, patch_search, monkeypatch
):
    """on 模式但 Agent 失效 → 用户看到的是固定管线结果，不是报错"""
    monkeypatch.setattr(settings, "AGENT_MODE", "on")
    monkeypatch.setattr("app.services.chat_service.search_relevant_items", lambda *a, **k: [])
    install_llm(FakeLLM())

    def _boom(*a, **k):
        raise RuntimeError("图构建失败")

    monkeypatch.setattr("app.agent.runtime.build_agent_graph", _boom)

    r = client.post(
        "/api/chat/stream",
        json={"question": "校招出差前需要做哪些准备"},
        headers=hdr(ctx["ua"]["token"]),
    )

    expect(r, 200, "Agent 失效时回退")
    assert "抱歉，知识库中暂无相关内容。" in r.text
    assert "图构建失败" not in r.text, "内部错误不应泄露给用户"


def test_agent_events_endpoint_returns_rich_trace(
    client, ctx, install_llm, patch_search, monkeypatch
):
    """富事件流：分诊 → 过程 → 工具 → 引用 → 文本 → 结束"""
    monkeypatch.setattr(settings, "AGENT_MODE", "on")
    patch_search(_refs(2))
    install_llm(FakeLLM(answer="出差前准备清单：审批、物料、行程。"))

    r = client.post(
        "/api/chat/agent/stream",
        json={"question": "校招出差前需要做哪些准备"},
        headers=hdr(ctx["ua"]["token"]),
    )

    expect(r, 200, "富事件流")
    assert '"type": "start"' in r.text
    assert '"type": "intent"' in r.text and '"kb_qa"' in r.text
    assert '"type": "step"' in r.text and "预检索" in r.text
    assert '"type": "tool"' in r.text and "knowledge_search" in r.text
    assert '"type": "references"' in r.text
    assert _sse_text(r.text) == "出差前准备清单：审批、物料、行程。"
    assert '"type": "done"' in r.text


def test_agent_status_endpoint(client, ctx, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MODE", "shadow")
    r = client.get("/api/agent/status", headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "Agent 状态")
    assert r.json()["mode"] == "shadow"

    r2 = client.get("/api/agent/tools", headers=hdr(ctx["ua"]["token"]))
    expect(r2, 200, "工具清单")
    names = {t["name"] for t in r2.json()["tools"]}
    assert "knowledge_search" in names and "stats_snapshot" not in names


# ---------------------------------------------------------------------------
# 5. 轨迹落库与查询
# ---------------------------------------------------------------------------

def test_record_and_query_run(client, db, ctx, install_llm, patch_search):
    """轨迹写入 → 管理员可查详情（普通用户不可见）"""
    patch_search(_refs(2))
    install_llm(FakeLLM(answer="这是落库用的回答。"))

    user = user_obj(db, "regr_admin")
    result = runtime.run_sync(user, "校招出差前需要做哪些准备", db)
    assert result["intent"] == "kb_qa" and result["sources"]

    outcome = runtime.prepare(user, "校招出差前需要做哪些准备", db, source="sync")
    run = runtime.record_run(db, user.id, "校招出差前需要做哪些准备", outcome, mode="on", answer="X")
    assert run is not None and run.id

    r = client.get(f"/api/agent/runs/{run.id}", headers=hdr(ctx["admin"]["token"]))
    expect(r, 200, "轨迹详情")
    detail = r.json()
    assert detail["intent"] == "kb_qa" and detail["mode"] == "on"
    assert detail["notes"] and detail["trace"], "详情应包含决策轨迹与工具调用"
    assert detail["sources"][0]["title"] == "工具条目0"

    r2 = client.get("/api/agent/runs", params={"mode": "on"}, headers=hdr(ctx["admin"]["token"]))
    expect(r2, 200, "轨迹列表")
    assert r2.json()["total"] >= 1

    # 普通用户无权查看轨迹
    r3 = client.get("/api/agent/runs", headers=hdr(ctx["ua"]["token"]))
    assert r3.status_code == 403
    r4 = client.post("/api/agent/run", params={"question": "你好"}, headers=hdr(ctx["ua"]["token"]))
    assert r4.status_code == 403


def test_shadow_worker_records_readonly_run(db, ctx, install_llm, patch_search):
    """shadow 后台 worker：只读跑一遍并落轨迹，不产生写操作"""
    patch_search(_refs(2))
    install_llm(FakeLLM(answer="shadow 对比用的回答。"))
    user = user_obj(db, "regr_ua")

    before_memos = db.query(Memo).filter(Memo.user_id == user.id).count()
    runtime._shadow_worker(user.id, "校招出差前需要做哪些准备")
    after_memos = db.query(Memo).filter(Memo.user_id == user.id).count()

    assert before_memos == after_memos, "shadow 模式不得产生副作用"
    db.expire_all()
    row = (
        db.query(AgentRun)
        .filter(AgentRun.user_id == user.id, AgentRun.mode == "shadow")
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    assert row is not None
    assert row.intent == "kb_qa"
    assert row.tool_calls == 1 and row.sources
    assert row.notes and row.trace


def test_shadow_dispatch_gating(db, ctx, monkeypatch):
    """shadow 分发：模式与采样率两个开关都要满足才跑"""
    user = user_obj(db, "regr_ua")

    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    assert runtime.maybe_run_shadow(user.id, "随便问问") is False

    monkeypatch.setattr(settings, "AGENT_MODE", "shadow")
    monkeypatch.setattr(settings, "AGENT_SHADOW_RATE", 0.0)
    assert runtime.maybe_run_shadow(user.id, "随便问问") is False

    # 闲聊不跑 shadow（没有对比价值，省一次模型调用）
    monkeypatch.setattr(settings, "AGENT_SHADOW_RATE", 1.0)
    assert runtime.maybe_run_shadow(user.id, "你好") is False


# ---------------------------------------------------------------------------
# 6. 检查点（HITL 的前置能力）
# ---------------------------------------------------------------------------

def test_checkpointer_enabled_runs_graph(db, ctx, install_llm, patch_search, monkeypatch, tmp_path):
    """开启检查点后图仍能正常跑通（SQLite 持久化 + thread_id 配置正确）"""
    patch_search(_refs(1))
    install_llm(FakeLLM())

    monkeypatch.setattr(settings, "AGENT_USE_CHECKPOINT", True)
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_PATH", str(tmp_path / "cp.db"))
    monkeypatch.setattr(runtime, "_checkpointer", None)
    monkeypatch.setattr(runtime, "_checkpointer_prepared", False)

    saver = runtime.get_checkpointer()
    assert saver is not None, "开启后应拿到检查点存储器"
    # 必须是真正的 SQLite 存储（掉到内存存储说明初始化失败，需暴露出来）
    assert type(saver).__name__ == "SqliteSaver", f"期望 SqliteSaver，实际 {type(saver).__name__}"

    outcome = _run(db, "regr_ua", "校招出差前需要做哪些准备")
    assert outcome.ok and outcome.sources

    # 全局单例已缓存，后续不应重复初始化
    assert runtime.get_checkpointer() is saver
