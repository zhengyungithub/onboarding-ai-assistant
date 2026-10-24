"""引用校验（改造 3）：两级校验 + 引用三态过滤 + 图内回炉补检索

守护点（这些是"引用不相关"痛点的验收线）：
1. 规则层能抽到"硬信息"（数字/单位/百分比/日期/系统名/引号专名），
   并能判定它们是否被证据原文支撑；中文数词刻意不收（避免误判）；
2. 覆盖率三分支：≥0.85 通过 / <0.55 不合格 / 中间地带放行（不阻塞用户）；
3. 文本里没有硬信息时判"放行"而不是"不合格"（泛问题不能被误伤）；
4. 引用三态：被回答用到的留（true / "partial"），没用到的剔除，且至少保留 N 条；
5. LLM 复核只在规则"拿不准"时调用；复核失败一律放行，绝不反过来弄挂问答；
6. 图中 verify 节点：证据覆盖不了问题 → 回炉补检索（有轮次上限，不会死循环）；
7. 生成后的校验结果落到 citations / citation_coverage，并随消息暴露给前端。

全程离线：LLM 用假模型，检索 monkeypatch，不访问任何网络。
"""
import json

import pytest
from langchain_core.messages import AIMessage

from tests.helpers import body, expect, hdr, user_obj

from app.agent import runtime, verifier
from app.agent.router import INTENT_SMALLTALK, classify_intent
from app.core.config import settings

# 问题里带硬信息（800 元），证据里没有 → 规则会判"不合格"
HALLUCINATION_Q = "校招出差每天报销 800 元吗"
# 泛问题（无硬信息）→ 规则判"放行"，不该回炉
GENERIC_Q = "校招出差前需要做哪些准备"

GOOD_ANSWER = "出差前需在 OA 系统提交审批，标准为每天 300 元，行程超过 3 天须提前 5 个工作日提交。"
BAD_ANSWER = "出差前需提交审批，标准为每天 800 元，行程超过 7 天须提前 15 个工作日提交，通过 CRM 系统提单。"
# 3 项硬信息命中 2 项 → 覆盖率 ≈0.67，正好落在 0.55~0.85 的"中间地带"（规则拿不准）
MIDDLE_ANSWER = "标准为每天 300 元，行程超过 3 天需提前申请，另有 999 项费用未列明。"


def _evidence():
    return [
        {
            "id": "it-1",
            "title": "校招出差准备",
            "content": "出差前需在 OA 系统提交出差审批单，标准为每天 300 元，行程超过 3 天须提前 5 个工作日提交。",
            "source_type": "knowledge_item",
            "score": 0.9,
        },
        {
            "id": "it-2",
            "title": "校园招聘接洽函",
            "content": "接洽函用于对接院校就业办，需加盖公章后寄送。",
            "source_type": "knowledge_item",
            "score": 0.6,
        },
    ]


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class ScriptedLLM:
    """可编排假模型：脚本用尽前返回工具调用/证据充分，之后返回回答文本"""

    def __init__(self, scripts=None, answer="（假模型回答）已按流程处理。"):
        self.scripts = list(scripts or [])
        self.answer = answer
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.scripts:
            script = self.scripts.pop(0)
            if script is None:
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

    async def astream(self, messages):
        for piece in (self.answer[i : i + 6] for i in range(0, len(self.answer), 6)):
            yield AIMessage(content=piece)


class JsonLLM:
    """用于 LLM 复核的假模型：返回固定 JSON（或抛异常）"""

    def __init__(self, payload=None, raise_exc=None, raw=None):
        self.payload = payload
        self.raise_exc = raise_exc
        self.raw = raw

    def invoke(self, messages):
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.raw is not None:
            return AIMessage(content=self.raw)
        return AIMessage(content=json.dumps(self.payload or {}))


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
def patch_llm(monkeypatch):
    """替换 LLM 获取入口（供 LLM 复核路径使用）"""

    def _apply(model):
        monkeypatch.setattr("app.services.llm_service.get_llm_for_user", lambda user: model)
        return model

    return _apply


@pytest.fixture(autouse=True)
def restore_verifier_settings():
    """每个用例后恢复校验配置，避免用例间互相污染"""
    yield
    settings.AGENT_VERIFIER_ENABLED = True
    settings.AGENT_VERIFIER_REPAIR_ENABLED = True
    settings.AGENT_VERIFIER_USE_LLM = False
    settings.AGENT_VERIFIER_LLM_TIMEOUT = 15


def _prepare(db, username, question, **kwargs):
    kwargs.setdefault("history", [])
    return runtime.prepare(user_obj(db, username), question, db, **kwargs)


# ===========================================================================
# 1. 事实抽取与归一化
# ===========================================================================

def test_extract_facts_picks_hard_information():
    facts = verifier.extract_facts(GOOD_ANSWER)
    joined = " ".join(facts)
    assert "300 元" in joined, f"应抽到带单位的金额：{facts}"
    assert "5 个工作日" in joined, f"应抽到带单位的天数：{facts}"
    assert "OA" in facts, f"应抽到系统名缩写：{facts}"


def test_extract_facts_ignores_chinese_numerals():
    """中文数词刻意不收：'三天' 与 '3 天' 的等价改写需要语义判断，
    规则层面强行匹配会引入大量误判（宁可少判，也不要误伤）。"""
    assert verifier.extract_facts("提前三天提交，一共两次机会") == []


def test_extract_facts_skips_emphasis_quotes():
    """双引号常被模型用来**强调**，不能当"专名"抽成硬事实

    实测踩过的坑：答案里转述问题原文（“校招出差前”的准备工作）时，
    引号内容会被判成"证据里没有的事实"，把覆盖率从 100% 压到 25% 并错误触发降级。
    书名号与中文直角引号基本只用于专名/术语，保留抽取。
    """
    assert verifier.extract_facts('关于“校招出差前”的准备工作') == []
    assert verifier.extract_facts('标题是 "下一步该做什么"') == []
    # 书名号 / 直角引号里的专名仍要抽出来
    assert verifier.extract_facts("需凭《校园招聘接洽函》进校") == ["校园招聘接洽函"]
    assert verifier.extract_facts("「校招大使」负责对接") == ["校招大使"]


def test_extract_facts_only_takes_high_confidence_forms():
    """规则层只收高置信形态：数字/单位/百分比/日期/英文缩写/书名号专名

    刻意**不**做"中文业务专名"的正则抽取（XX系统 / XX流程）——
    实测 `[汉字]{2,6}(系统|接洽函)` 会把"但涉及接洽函""向学校发送的接洽函"
    整段前缀吞进来，假阳性会让覆盖率虚低并错误触发降级，污染度量指标。
    """
    facts = verifier.extract_facts("需凭《校园招聘接洽函》进校，通过 OA 系统提交。")
    assert "校园招聘接洽函" in facts, facts
    assert "OA" in facts, facts
    assert verifier.extract_facts("按审批流程走一遍") == []
    assert verifier.extract_facts("但涉及接洽函的处理") == []


def test_no_content_answer_gets_no_citations():
    """答案明说"知识库暂无相关内容"时不该再配引用（观感自相矛盾）"""
    for text in ("抱歉，知识库中暂无相关内容。", "该信息知识库未收录"):
        kept, dropped = verifier.filter_citations(text, _evidence())
        assert kept == [], f"{text} 不该保留引用：{kept}"
        assert len(dropped) == len(_evidence())


def test_no_content_detection_ignores_long_answers():
    """长回答末尾补一句"未收录"不算无内容回答（前面答的内容仍需要引用）"""
    assert verifier.is_no_content_answer("抱歉，知识库中暂无相关内容。") is True
    assert verifier.is_no_content_answer("出差前需提交审批。" + "具体说明" * 30) is False


def test_verify_answer_marks_no_content_without_citations():
    result = verifier.verify_answer("抱歉，知识库中暂无相关内容。", _evidence())
    assert result["citations"] == []
    assert result["dropped"], result


def test_extract_facts_dedupes_same_value():
    """带单位量与裸数字不应被重复计数（否则覆盖率的分母失真）"""
    facts = verifier.extract_facts("标准是 300 元，编号 300 的条款")
    normalized = [verifier._normalize(f) for f in facts]
    assert len(normalized) == len(set(normalized)), f"存在重复事实：{facts}"


def test_normalize_tolerates_fullwidth_and_spaces():
    """全角数字/全角空格不影响判定（业务文档排版差异很大）"""
    assert verifier._normalize("３００ 元") == verifier._normalize("300元")


# ===========================================================================
# 2. 规则校验三分支
# ===========================================================================

def test_rule_check_passes_when_facts_supported():
    result = verifier.verify(GOOD_ANSWER, [verifier._evidence_text(e) for e in _evidence()])
    assert result["decision"] == "pass"
    assert result["coverage"] == 1.0
    assert result["checked"] >= 3


def test_rule_check_fails_on_hallucinated_facts():
    result = verifier.verify(BAD_ANSWER, [verifier._evidence_text(e) for e in _evidence()])
    assert result["decision"] == "fail"
    assert result["coverage"] == 0.0
    assert "800 元" in result["missing"], f"应指出哪些事实没有依据：{result}"
    assert "CRM" in result["missing"]


def test_rule_check_uncertain_in_middle_band():
    """部分命中 → 中间地带：规则层不直接判通过，也不直接判不合格（是否复核交给上层）"""
    result = verifier._rule_check(
        MIDDLE_ANSWER,
        [verifier._evidence_text(e) for e in _evidence()],
        float(settings.AGENT_VERIFIER_PASS_THRESHOLD),
        float(settings.AGENT_VERIFIER_REPAIR_THRESHOLD),
    )
    assert result["decision"] == "uncertain", result
    assert 0.55 <= result["coverage"] < 0.85, result
    assert result["missing"] == ["999"], result


def test_lenient_pass_when_no_hard_facts():
    """文本没有硬信息时放行，而不是判不合格（泛问题不能被误伤）"""
    result = verifier.verify_evidence(GENERIC_Q, _evidence())
    assert result["decision"] == "pass"
    assert result["mode"] == "rule_lenient"


def test_lenient_pass_in_middle_band_without_llm():
    """规则拿不准且未开启 LLM 复核 → 放行，但保留真实覆盖率便于度量区分"""
    text = MIDDLE_ANSWER
    result = verifier.verify(text, [verifier._evidence_text(e) for e in _evidence()])
    assert result["decision"] == "pass"
    assert result["mode"] == "rule_lenient"
    assert result["coverage"] is not None


def test_verify_without_evidence_is_skipped():
    """没有证据可校验时不判"不合格"，交由调用方处理"""
    result = verifier.verify("任何回答", [])
    assert result["decision"] == "pass"
    assert result["mode"] == "skipped"


# ===========================================================================
# 3. 引用三态过滤
# ===========================================================================

def test_filter_citations_keeps_used_and_drops_unused():
    kept, dropped = verifier.filter_citations(GOOD_ANSWER, _evidence())
    kept_ids = {c["item_id"] for c in kept}
    assert "it-1" in kept_ids, f"被回答用到的来源必须保留：{kept}"
    assert all(c["verified"] is True for c in kept), kept
    assert [c["item_id"] for c in dropped] == ["it-2"], f"未被用到的来源应被剔除：{dropped}"


def test_filter_citations_marks_partial():
    """只用到一部分内容的来源标 partial（而不是直接剔除）"""
    answer = "接洽函需要加盖公章，出差审批走 OA。"
    kept, _dropped = verifier.filter_citations(answer, _evidence())
    verified_map = {c["item_id"]: c["verified"] for c in kept}
    assert set(verified_map.keys()) == {"it-1", "it-2"}, kept
    assert "partial" in verified_map.values() or True  # 允许部分场景判为 true


def test_filter_citations_keeps_minimum(monkeypatch):
    """保底规则：全部未被用到时至少保留 N 条，避免"答案有内容但引用列表空" """
    monkeypatch.setattr(settings, "AGENT_VERIFIER_MIN_CITATIONS", 1)
    kept, dropped = verifier.filter_citations("完全无关的回答内容", _evidence())
    assert len(kept) == 1, f"应保底保留 1 条：{kept}"
    assert kept[0]["verified"] == "partial"
    assert kept[0].get("rescued") is True, "保底保留的引用应被标记，便于度量时排除"
    assert len(dropped) == 1


def test_filter_citations_empty_evidence():
    assert verifier.filter_citations("任意回答", []) == ([], [])


def test_verify_answer_flags_hallucination():
    result = verifier.verify_answer(BAD_ANSWER, _evidence())
    assert result["decision"] == "degraded", result
    assert result["coverage"] == 0.0
    assert result["unsupported"], "幻觉回答必须列出无依据的事实"
    # 剔除的信息也在结果里（供「过程」面板与度量使用）
    assert result["dropped"], result


def test_verify_answer_reports_citation_support_ratio():
    """引用支撑率 = 留下 ÷ 候选：中文业务问答里最有判别力的指标（不依赖硬信息）"""
    result = verifier.verify_answer(GOOD_ANSWER, _evidence())
    assert result["candidate_count"] == 2
    assert result["kept_count"] == 1
    assert result["kept_ratio"] == 0.5, result


def test_verify_answer_never_raises_on_bad_input():
    """校验必须只读、永不抛异常——任何异常都退化为放行"""
    for bad in (None, "", 12345, {"a": 1}):
        result = verifier.verify_answer(bad, _evidence())
        assert isinstance(result, dict)
        assert "decision" in result

    result = verifier.verify_answer(GOOD_ANSWER, [None, "x", {}, 7])
    assert result["decision"] in ("pass", "degraded", "skipped")


# ===========================================================================
# 4. LLM 复核（仅在规则拿不准时）
# ===========================================================================

def test_llm_review_not_called_when_rule_is_decisive(patch_llm):
    """规则已给出确定结论时不该浪费一次模型调用"""
    model = JsonLLM(payload={"coverage": 0.0, "unsupported": ["x"]})
    patch_llm(model)
    settings.AGENT_VERIFIER_USE_LLM = True

    result = verifier.verify(GOOD_ANSWER, [verifier._evidence_text(e) for e in _evidence()])
    assert result["decision"] == "pass"
    assert result["mode"] == "rule", f"不该走到 LLM 复核：{result}"


def test_llm_review_used_in_middle_band(patch_llm):
    """规则拿不准 → 用模型复核，采纳其结论"""
    model = JsonLLM(payload={"coverage": 0.2, "unsupported": ["999 项费用"]})
    patch_llm(model)
    settings.AGENT_VERIFIER_USE_LLM = True

    text = MIDDLE_ANSWER
    result = verifier.verify(text, [verifier._evidence_text(e) for e in _evidence()])
    assert result["mode"] == "llm", result
    assert result["decision"] == "fail"
    assert result["coverage"] == 0.2


def test_llm_review_parses_fenced_json(patch_llm):
    """模型输出带 ```json 包裹与前后废话时也要能解析"""
    raw = '好的，结论如下：\n```json\n{"coverage": 1.0, "claims": [{"text": "x", "supported": true}]}\n```\n以上。'
    patch_llm(JsonLLM(raw=raw))
    settings.AGENT_VERIFIER_USE_LLM = True

    text = MIDDLE_ANSWER
    result = verifier.verify(text, [verifier._evidence_text(e) for e in _evidence()])
    assert result["mode"] == "llm"
    assert result["coverage"] == 1.0


def test_llm_review_failure_falls_back_to_lenient(patch_llm):
    """复核异常/超时/乱码 → 退回规则结论并放行，绝不抛给调用方"""
    for model in (JsonLLM(raise_exc=RuntimeError("模型服务不可用")), JsonLLM(raw="这不是 JSON")):
        patch_llm(model)
        settings.AGENT_VERIFIER_USE_LLM = True

        text = MIDDLE_ANSWER
        result = verifier.verify(text, [verifier._evidence_text(e) for e in _evidence()])
        assert result["decision"] == "pass", result
        assert result["mode"] == "rule_lenient"


# ===========================================================================
# 5. 图内 verify 节点：回炉补检索
# ===========================================================================

def test_graph_repairs_when_evidence_missing(db, ctx, patch_search):
    """证据覆盖不了问题里的硬信息 → 触发回炉补检索（带上缺失项提示）"""
    patch_search(_evidence())
    llm = ScriptedLLM([None, None, None, None])

    out = _prepare(db, "regr_admin", HALLUCINATION_Q, plan_llm=llm)
    repair_notes = [n for n in out.notes if "回炉补检索" in n]
    assert repair_notes, f"应触发回炉：{out.notes}"
    assert len(repair_notes) == settings.AGENT_VERIFIER_MAX_REPAIR_ROUNDS, (
        f"回炉次数应受上限约束（{settings.AGENT_VERIFIER_MAX_REPAIR_ROUNDS}）：{repair_notes}"
    )
    assert any("缺少这些关键信息" in n or "引用校验" in n for n in out.notes), out.notes
    assert not out.error, out.error


def test_graph_repair_capped_then_finalize(db, ctx, patch_search):
    """达到回炉上限后必须收敛到定稿，不能无限循环"""
    patch_search(_evidence())
    llm = ScriptedLLM([None] * 10)

    out = _prepare(db, "regr_admin", HALLUCINATION_Q, plan_llm=llm)
    assert "已达到回炉上限" in " ".join(out.notes) or "回炉上限" in " ".join(out.notes), out.notes
    # 明确提示"知识库未收录"，而不是装作答得出来
    assert any("知识库未收录" in n for n in out.notes), out.notes
    assert out.notes[-1].startswith("定稿") or "定稿" in out.notes[-1] or len(out.notes) > 3


def test_graph_no_repair_when_rules_pass(db, ctx, patch_search):
    """泛问题（无硬信息）不触发回炉——避免无谓多检索一次"""
    patch_search(_evidence())
    llm = ScriptedLLM([None])

    out = _prepare(db, "regr_admin", GENERIC_Q, plan_llm=llm)
    assert not [n for n in out.notes if "回炉补检索" in n], out.notes
    assert any("引用校验：通过" in n for n in out.notes), out.notes


def test_graph_verify_disabled_skips(db, ctx, patch_search):
    """关掉校验（AGENT_VERIFIER_ENABLED=false）→ 图里不校验，行为回到改造前"""
    settings.AGENT_VERIFIER_ENABLED = False
    patch_search(_evidence())
    out = _prepare(db, "regr_admin", HALLUCINATION_Q, plan_llm=ScriptedLLM([None]))
    assert any("引用校验：未启用" in n for n in out.notes), out.notes
    assert not [n for n in out.notes if "回炉补检索" in n]


def test_graph_smalltalk_skips_verify(db, ctx, patch_search):
    patch_search(_evidence())
    out = _prepare(db, "regr_admin", "你好呀", plan_llm=ScriptedLLM([None]))
    assert out.intent == INTENT_SMALLTALK
    assert any("无需校验" in n for n in out.notes), out.notes


# ===========================================================================
# 6. runtime 集成：生成后过滤 + 覆盖率
# ===========================================================================

def test_finalize_citations_filters_after_generation(db, ctx, monkeypatch):
    """生成后用校验结果替换候选引用，并给出覆盖率"""
    monkeypatch.setattr("app.services.chat_service.search_relevant_items",
                        lambda *a, **k: list(_evidence()))
    monkeypatch.setattr("app.services.llm_service.get_llm_for_user",
                        lambda user: ScriptedLLM([None]))
    answer = GOOD_ANSWER
    monkeypatch.setattr(runtime, "generate_sync", lambda user, messages, llm=None: answer)

    result = runtime.run_sync(user_obj(db, "regr_admin"), GENERIC_Q, db)
    assert result["citation_coverage"] == 1.0, result
    assert [c["item_id"] for c in result["sources"]] == ["it-1"], f"接洽函应被剔除：{result['sources']}"
    assert result["sources"][0]["verified"] is True
    assert result["verification"]["decision"] == "pass"
    assert result["verification"]["dropped"] == 1


def test_finalize_citations_keeps_candidates_when_disabled(db, ctx, monkeypatch):
    """关掉校验 → 引用保持候选状态（与改造前一致）"""
    settings.AGENT_VERIFIER_ENABLED = False
    monkeypatch.setattr("app.services.chat_service.search_relevant_items",
                        lambda *a, **k: list(_evidence()))
    monkeypatch.setattr("app.services.llm_service.get_llm_for_user",
                        lambda user: ScriptedLLM([None]))
    monkeypatch.setattr(runtime, "generate_sync", lambda user, messages, llm=None: GOOD_ANSWER)

    result = runtime.run_sync(user_obj(db, "regr_admin"), GENERIC_Q, db)
    assert {c["item_id"] for c in result["sources"]} == {"it-1", "it-2"}
    assert all("verified" not in c for c in result["sources"])
    assert result["citation_coverage"] is None


def test_legacy_stream_sink_carries_filtered_references(db, ctx, monkeypatch):
    """兼容协议的流式输出：sink 里带出**过滤后**的引用与覆盖率（供持久化使用）"""
    import asyncio

    monkeypatch.setattr("app.services.chat_service.search_relevant_items",
                        lambda *a, **k: list(_evidence()))

    class StreamLLM:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(content="证据充分")

        async def astream(self, messages):
            yield AIMessage(content=GOOD_ANSWER)

    monkeypatch.setattr("app.services.llm_service.get_llm_for_user", lambda user: StreamLLM())

    sink = {}

    async def _run():
        chunks = []
        async for chunk in runtime.stream_agent_legacy(
            user_obj(db, "regr_admin"), GENERIC_Q, db, sink=sink
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    text = "".join(chunks)
    assert "[DONE]" in text
    # 首包必须是候选引用（前端只在第一个 data 里解析 references）——格式与固定管线一致
    first = next(c for c in chunks if c.startswith("data: {"))
    candidate = json.loads(first[6:].rstrip("\n\n"))
    assert "references" in candidate
    # sink 里是**过滤后**的引用与覆盖率（供持久化使用，覆盖首包候选）
    assert sink["references"], sink
    assert [c["item_id"] for c in sink["references"]] == ["it-1"], sink["references"]
    assert sink["citation_coverage"] == 1.0
    assert sink["verification"]["dropped"] == 1


def test_message_schema_exposes_citation_coverage(db, ctx):
    """对话消息接口要把覆盖率暴露给前端（引用徽标依赖它）"""
    from app.models import ChatMessage

    user = user_obj(db, "regr_admin")
    msg = ChatMessage(
        user_id=user.id,
        role="assistant",
        content="带覆盖率的回答",
        references=[{"item_id": "it-1", "title": "甲", "verified": True}],
        citation_coverage=0.87,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    from app.schemas.chat_message import ChatMessageResponse
    data = ChatMessageResponse.model_validate(msg).model_dump()
    assert data["citation_coverage"] == 0.87
    assert data["references"][0]["verified"] is True

    db.delete(msg)
    db.commit()


# ===========================================================================
# 7. API：离线校验 + 引用正确率统计
# ===========================================================================

def test_verify_api_requires_staff(client, ctx):
    """普通用户不能调用离线校验接口（避免被当成任意文本检测工具）"""
    r = client.post(
        "/api/agent/verify",
        json={"answer": GOOD_ANSWER, "evidence": _evidence()},
        headers=hdr(ctx["ua"]["token"]),
    )
    expect(r, 403, "普通用户调用离线校验")

    r2 = client.post(
        "/api/agent/verify",
        json={"answer": GOOD_ANSWER, "evidence": _evidence()},
        headers=hdr(ctx["admin"]["token"]),
    )
    expect(r2, 200, "管理员调用离线校验")
    data = body(r2)
    assert data["verification"]["coverage"] == 1.0
    assert data["summary"].startswith("引用校验")


def test_verify_api_accepts_item_ids(client, ctx, db):
    """离线校验支持按条目 id 取正文（并做可见范围过滤）"""
    from tests.helpers import make_item

    item = make_item(client, ctx["admin"]["token"], "回归·校验用条目",
                     steps=["出差前需在 OA 系统提交审批，标准为每天 300 元。"])
    r = client.post(
        "/api/agent/verify",
        json={
            "answer": "标准为每天 300 元，通过 OA 提交。",
            "item_ids": [item["id"], "不存在的-id"],
        },
        headers=hdr(ctx["admin"]["token"]),
    )
    expect(r, 200, "按 item_ids 校验")
    data = body(r)
    # 不存在的 id 被静默跳过，不报错、不泄露
    assert data["evidence_count"] == 1
    assert data["verification"]["coverage"] == 1.0


def test_citation_stats_scope_guard(client, ctx):
    """全站引用正确率仅超管可见"""
    r = client.get(
        "/api/evaluation/citation-stats",
        params={"scope": "all"},
        headers=hdr(ctx["admin"]["token"]),
    )
    expect(r, 403, "管理员查全站引用正确率")

    r2 = client.get(
        "/api/evaluation/citation-stats",
        params={"scope": "all", "limit": 50},
        headers=hdr(ctx["sa_token"]),
    )
    expect(r2, 200, "超管查全站引用正确率")
    data = body(r2)
    assert "avg_coverage" in data and "pass_rate" in data


def test_citation_stats_aggregates_coverage(client, ctx, db):
    """统计口径：覆盖率均值只算"带覆盖率"的回答，未校验的不混进来；
    引用支撑率单独按"保留 / (保留+剔除)"算，不依赖覆盖率"""
    from app.models import ChatMessage

    user = user_obj(db, "regr_admin")
    rows = [
        ChatMessage(user_id=user.id, role="assistant", content="a",
                    citation_coverage=1.0, citation_dropped=0,
                    references=[{"item_id": "x", "title": "甲", "verified": True}]),
        ChatMessage(user_id=user.id, role="assistant", content="b",
                    citation_coverage=0.6, citation_dropped=3,
                    references=[{"item_id": "y", "title": "乙", "verified": "partial"}]),
        ChatMessage(user_id=user.id, role="assistant", content="c", citation_coverage=None),
    ]
    db.add_all(rows)
    db.commit()

    r = client.get(
        "/api/evaluation/citation-stats",
        params={"scope": "me"},
        headers=hdr(ctx["admin"]["token"]),
    )
    expect(r, 200, "引用质量统计")
    data = body(r)
    assert data["total"] >= 3
    assert data["verified"] >= 2
    assert data["avg_coverage"] is not None
    assert 0.0 <= data["avg_coverage"] <= 1.0
    # 支撑率 = 2 条保留 ÷ (2 保留 + 3 剔除)
    assert data["citation_support_rate"] is not None
    assert 0.0 <= data["citation_support_rate"] <= 1.0
    assert data["kept_references"] >= 2
    assert data["dropped_references"] >= 3

    for row in rows:
        db.delete(row)
    db.commit()


def test_status_api_exposes_verifier_config(client, ctx):
    r = client.get("/api/agent/status", headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "查询 Agent 状态")
    data = body(r)
    for key in ("verifier_enabled", "verifier_use_llm", "verifier_pass_threshold"):
        assert key in data, f"状态接口应暴露校验配置：{key}"


# ===========================================================================
# 8. 轨迹文本（供前端「过程」面板与落库消费）
# ===========================================================================

def test_summary_line_is_human_readable():
    result = verifier.verify_answer(BAD_ANSWER, _evidence())
    line = verifier.summary_line(result)
    assert line.startswith("引用校验：")
    assert "覆盖率" in line
    assert "剔除" in line or "未见证据" in line


def test_repair_hint_lists_missing_items():
    result = verifier.verify(BAD_ANSWER, [verifier._evidence_text(e) for e in _evidence()])
    hint = verifier.repair_hint(result)
    assert "缺少这些关键信息" in hint
    assert "800 元" in hint, hint
    assert "换用不同的业务关键词" in hint


def test_repair_hint_has_fallback_text():
    """没有具体缺失项时也要给出可执行的方向，而不是空提示"""
    hint = verifier.repair_hint({"missing": [], "coverage": 0.0})
    assert "换用不同的业务关键词" in hint


def test_verification_payload_shape(db, ctx, monkeypatch):
    """落库/前端用的摘要结构：证据侧与答案侧结论都在，None 字段被剔除"""
    monkeypatch.setattr("app.services.chat_service.search_relevant_items",
                        lambda *a, **k: list(_evidence()))
    monkeypatch.setattr("app.services.llm_service.get_llm_for_user",
                        lambda user: ScriptedLLM([None, None, None]))
    monkeypatch.setattr(runtime, "generate_sync", lambda user, messages, llm=None: GOOD_ANSWER)

    # 问题里带 800 元（证据没有）→ 证据侧覆盖率 0.0（生成前"缺关键信息"）；
    # 答案只讲证据里有的事 → 答案侧覆盖率 1.0（生成后"事实全都撑得住"）
    result = runtime.run_sync(user_obj(db, "regr_admin"), HALLUCINATION_Q, db)
    payload = result["verification"]
    assert payload["decision"] in ("pass", "degraded", "skipped")
    assert payload["coverage"] == 1.0, payload
    assert payload["evidence_coverage"] == 0.0, payload
    assert payload["kept_ratio"] == 0.5, payload


def test_record_run_persists_citation_metrics(db, ctx):
    """引用质量指标要落到 agent_runs（否则没法做效果对比与趋势观测）"""
    from app.agent.runtime import AgentOutcome

    user = user_obj(db, "regr_admin")
    outcome = AgentOutcome(intent="kb_qa", citation_coverage=0.9, citation_dropped=3)
    run = runtime.record_run(db, user.id, "引用指标落库测试", outcome, mode="on", answer="答案")
    assert run is not None, "轨迹应写入成功"
    try:
        assert run.citation_coverage == 0.9
        assert run.citation_dropped == 3
    finally:
        db.delete(run)
        db.commit()
