"""LangGraph Agent 图（M1+M2+M3：分诊 → 预检索 → 规划 ↔ 工具执行 → 起草 → 引用校验 → 定稿）

图的形状：

    route ──闲聊──> draft ──> verify ──> finalize ──> END
      │
      └──业务/写操作──> retrieve ──> plan ──需要补充证据──> act ──┐
                                       ↑_______________________|
                                       │
                                       └──证据足够/预算耗尽──> draft ──> verify
                                                                          │
                              verify ──校验不合格且未超回炉上限──> plan <──┘
                                └──通过 / 超回炉上限──> finalize ──> END

五个关键设计决策（都为了让"改造不劣化"）：

1. **预检索由代码保证，不由模型决定**。业务问题先做一次确定性检索建立"证据基线"，
   模型只负责决定"是否还要补充检索"。这样 Agent 化的失败面被限制在"少检索一次"，
   而不是"完全没检索"——退一万步也不会比现有固定管线更差。

2. **draft 节点只组装最终提问消息，不做 token 生成**。生成统一由 runtime 负责，
   使 SSE 流式与同步两种模式共用同一份提示词与证据，避免两套逻辑漂移。

3. **act 节点自建而非用 ToolNode**。ToolNode 会执行全部 tool_calls，
   而这里需要按意图收窄工具白名单、拦截重复检索、记录每次调用的结果摘要，
   还要在**高风险写操作**执行前用 `interrupt()` 挂起等人工审批（HITL）。

4. **finalize 节点负责"定稿"**：确定最终引用、标记"知识库答不上来"的缺口、收尾轨迹。

5. **verify 节点只校验"证据能否回答问题"，绝不改写答案**。答案这一刻还没生成，
   而且"证据没覆盖问题"恰恰是回炉换词**唯一有效**的时机；答案生成后的
   事实校验与引用三态过滤放在 runtime（那一刻答案文本才存在），
   结论通过"引用三态 + 降级提示 + 覆盖率"表达，不做事后偷偷改正文。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from app.agent import verifier
from app.agent.context import current_context
from app.agent.prompts import (
    ACTION_CONSTRAINT,
    PLAN_HUMAN_TEMPLATE,
    PLAN_SYSTEM_PROMPT,
    REPAIR_HUMAN_TEMPLATE,
    SMALLTALK_SYSTEM_PROMPT,
)
from app.agent.router import (
    INTENT_ACTION,
    INTENT_SMALLTALK,
    classify_intent,
    intent_label,
    should_retrieve,
)
from app.agent.state import (
    AgentState,
    evidence_brief,
    is_timed_out,
    merge_evidence,
    to_sources,
)
from app.services import chat_service

# 单个工具结果回传给模型的最大字符数（防止一条超长结果吃光上下文）
TOOL_RESULT_MAX_CHARS = 4000
# 规划阶段的证据摘要里，单条内容的截断长度
PLAN_EVIDENCE_CHARS = 240


def _note(state: AgentState, text: str, **fields: Any) -> Dict[str, Any]:
    """追加一条人类可读轨迹，并合并其他状态字段"""
    fields["notes"] = list(state.get("notes") or []) + [text]
    return fields


def _call_signature(call: Dict[str, Any]) -> str:
    """工具调用指纹（用于拦截重复检索）

    检索类工具只看「工具名 + 查询语句」——模型很容易把预检索用过的查询原样再发一次，
    只差一个没写的 top_k 参数；其余工具按完整参数比对。
    """
    name = call.get("name") or ""
    args = call.get("args") or {}
    if name in ("knowledge_search", "document_search"):
        return f"{name}:{(args.get('query') or '').strip()}"
    return json.dumps({"name": name, "args": args}, ensure_ascii=False, sort_keys=True)


def _retrieval_signature(name: str, query: str) -> str:
    return f"{name}:{(query or '').strip()}"


def _invoke_with_timeout(llm, messages: List[BaseMessage], timeout: int):
    """调用 LLM，带软超时

    超时返回 None（调用方视为"规划失败"，带着已有证据直接起草），
    而不是让用户一直等——这是"可用性优先于完备性"的取舍。
    """
    if not timeout or timeout <= 0:
        return llm.invoke(messages)

    box: Dict[str, Any] = {}

    def _run():
        try:
            box["msg"] = llm.invoke(messages)
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    if "err" in box:
        raise box["err"]
    return box.get("msg")


def _budget_exhausted(state: AgentState, max_tool_calls: int, max_llm_calls: int) -> bool:
    if int(state.get("tool_calls") or 0) >= max_tool_calls:
        return True
    if int(state.get("llm_calls") or 0) >= max_llm_calls:
        return True
    return is_timed_out(state)


# ---------------------------------------------------------------------------
# 节点实现
# ---------------------------------------------------------------------------

def node_route(state: AgentState) -> Dict[str, Any]:
    """分诊：决定意图（不调用 LLM）"""
    intent = classify_intent(state["question"], state.get("history"))
    return _note(
        state,
        f"分诊：{intent_label(intent)}（{intent}）",
        intent=intent,
    )


def make_retrieve_node(
    tools_by_name: Dict[str, BaseTool],
    top_k: int,
    max_tool_calls: int,
    fast_path_min_hits: int = 0,
) -> Callable:
    """预检索节点：建立证据基线，并组装规划对话的首两条消息

    `fast_path_min_hits > 0` 时启用快路径：命中数达标即认为证据充足，
    直接进起草、跳过规划模型调用——省下一次模型调用（实测数秒），
    这对"回答前要等多久"的影响远大于多检索一次带来的收益。
    """

    def node_retrieve(state: AgentState) -> Dict[str, Any]:
        intent = state.get("intent") or ""
        evidence: List[Dict[str, Any]] = []
        fields: Dict[str, Any] = {}
        seen = list(state.get("seen") or [])

        if should_retrieve(intent):
            tool = tools_by_name.get("knowledge_search")
            if tool is not None:
                result = tool.invoke({"query": state["search_question"], "top_k": top_k})
                if isinstance(result, dict):
                    evidence = result.get("results") or []
                # 登记预检索指纹：避免规划模型把同一查询原样再发一次
                seen.append(_retrieval_signature("knowledge_search", state["search_question"]))
                fields["tool_calls"] = int(state.get("tool_calls") or 0) + 1
                fields = _note(state, f"预检索：命中 {len(evidence)} 条", **fields)
            else:
                fields = _note(state, "预检索：knowledge_search 不可用，跳过", **fields)
        else:
            fields = _note(state, "跳过预检索（该意图不需要证据基线）", **fields)

        fields["seen"] = seen
        fields["evidence"] = merge_evidence(list(state.get("evidence") or []), evidence)

        # 快路径：证据充足 → 不调用规划模型，直接起草
        if fast_path_min_hits > 0 and len(fields["evidence"]) >= fast_path_min_hits:
            return _note(
                state,
                f"证据充足（{len(fields['evidence'])} 条 ≥ {fast_path_min_hits}），跳过规划直接起草",
                fast_path=True,
                plan_note="fast_path",
                **fields,
            )

        # 组装规划对话：让模型明确知道"手上已有什么、还能用几轮"
        fields["messages"] = [
            SystemMessage(content=PLAN_SYSTEM_PROMPT),
            HumanMessage(
                content=PLAN_HUMAN_TEMPLATE.format(
                    question=state["search_question"],
                    evidence=evidence_brief(
                        fields["evidence"], per_item=PLAN_EVIDENCE_CHARS
                    ),
                    rounds=int(fields.get("tool_calls") or 0),
                    max_tool_calls=max_tool_calls,
                    intent=intent_label(intent),
                )
            ),
        ]
        return fields

    return node_retrieve


def make_plan_node(
    plan_llm,
    max_tool_calls: int,
    max_llm_calls: int,
    plan_timeout: int,
    budget_label: int,
) -> Callable:
    """规划节点：判断证据是否足够；不足则发起工具调用"""

    def node_plan(state: AgentState) -> Dict[str, Any]:
        if _budget_exhausted(state, max_tool_calls, max_llm_calls):
            return _note(
                state,
                "预算/超时到达上限，停止补检索，用已有证据起草",
                plan_note="budget",
            )

        try:
            message = _invoke_with_timeout(plan_llm, state["messages"], plan_timeout)
        except Exception as exc:  # noqa: BLE001 —— 规划失败不该让整轮问答失败
            return _note(
                state,
                f"规划调用失败（{type(exc).__name__}），降级为使用已有证据起草",
                plan_note="failed",
                error=str(exc),
            )

        if message is None:
            return _note(
                state,
                f"规划超时（>{plan_timeout}s），降级为使用已有证据起草",
                plan_note="timeout",
                error="plan_timeout",
            )

        calls = getattr(message, "tool_calls", None) or []
        if calls:
            names = "、".join(c.get("name", "?") for c in calls)
            note = f"规划：需补充证据 → 调用 {names}"
        else:
            note = "规划：证据充分，直接起草回答"
        return _note(
            state,
            note,
            messages=[message],
            llm_calls=int(state.get("llm_calls") or 0) + 1,
            plan_note="tool_calls" if calls else "sufficient",
        )

    return node_plan


def _approved_call_ids(
    calls: List[Dict[str, Any]], decision: Any, hitl_tools: set
) -> set:
    """把审批结果归一化为"被批准的 call_id 集合"

    支持三种人工答复：
    - `{"approved": true}`            全部批准
    - `{"approved": false}`           全部拒绝
    - `{"approved_call_ids": [...]}`  精确批准某几个（多写操作时更细粒度，默认拒绝未列出的）
    """
    risky_ids = {
        (c.get("id") or f"call_{c.get('name')}")
        for c in calls
        if (c.get("name") or "") in hitl_tools
    }
    if isinstance(decision, dict):
        if isinstance(decision.get("approved_call_ids"), (list, tuple, set)):
            return {str(i) for i in decision["approved_call_ids"]} & risky_ids
        if decision.get("approved") is True:
            return set(risky_ids)
        if decision.get("approved") is False:
            return set()
    if decision is True:
        return set(risky_ids)
    return set()


def make_act_node(
    tools_by_name: Dict[str, BaseTool],
    allowed_names: set,
    hitl_tools: Optional[set] = None,
    hitl_enabled: bool = False,
) -> Callable:
    """工具执行节点：按白名单执行 tool_calls，拦截越权与重复检索

    `hitl_enabled=True` 时，若本轮包含**高风险写操作**（如把资料入库成条目），
    会先用 `interrupt()` 挂起整轮运行，等人工审批后再决定是否执行——
    这是"模型可以起草、但不能替用户拍板写库"的落地方式。
    """
    hitl_tools = set(hitl_tools or ())

    def node_act(state: AgentState) -> Dict[str, Any]:
        messages = list(state.get("messages") or [])
        last = messages[-1] if messages else None
        calls = getattr(last, "tool_calls", None) or []
        prior = list(state.get("seen") or [])
        prior_set = set(prior)

        # ---- HITL：高风险写操作先挂起，等人工审批 ----
        # 中断点之前的逻辑必须无副作用：恢复时本节点会从头重跑，
        # 然后 interrupt() 这次直接返回人工答复（而不是再次抛出中断）。
        approved_ids: set = set()
        approval: Dict[str, Any] = {}
        risky = [c for c in calls if (c.get("name") or "") in hitl_tools]
        if hitl_enabled and risky:
            decision = interrupt(
                {
                    "type": "approval_required",
                    "reason": "模型请求执行写操作，需人工确认后才能落库",
                    "writes": [
                        {
                            "call_id": c.get("id") or f"call_{c.get('name')}",
                            "tool": c.get("name"),
                            "args": c.get("args") or {},
                        }
                        for c in risky
                    ],
                }
            )
            approved_ids = _approved_call_ids(calls, decision, hitl_tools)
            approval = {"approved_call_ids": sorted(approved_ids), "decision": decision}

        outgoing: List[BaseMessage] = []
        new_evidence: List[Dict[str, Any]] = []
        executed = 0
        notes: List[str] = []

        for call in calls:
            name = call.get("name") or ""
            args = call.get("args") or {}
            call_id = call.get("id") or f"call_{name}"

            if name not in allowed_names or name not in tools_by_name:
                outgoing.append(
                    ToolMessage(
                        content=f"工具 {name} 在当前场景不可用；请改用可用工具，或直接依据现有证据作答。",
                        tool_call_id=call_id,
                    )
                )
                notes.append(f"拦截不可用工具：{name}")
                continue

            if hitl_enabled and name in hitl_tools and call_id not in approved_ids:
                outgoing.append(
                    ToolMessage(
                        content=(
                            f"写操作 {name} 需人工审批，本次未获批准，已跳过（未产生任何数据变更）。"
                            "请告知用户：如需落库，可在对应管理页面手动完成。"
                        ),
                        tool_call_id=call_id,
                    )
                )
                notes.append(f"未获批准，跳过写操作：{name}")
                continue

            signature = _call_signature(call)
            if signature in prior_set:
                outgoing.append(
                    ToolMessage(
                        content="本轮已执行过完全相同的检索，请更换关键词，或直接依据现有证据作答。",
                        tool_call_id=call_id,
                    )
                )
                notes.append(f"跳过重复检索：{name}")
                continue

            result = tools_by_name[name].invoke(args)
            executed += 1
            prior.append(signature)
            prior_set.add(signature)
            ok = bool(result.get("ok")) if isinstance(result, dict) else True
            count = result.get("count") if isinstance(result, dict) else None
            notes.append(f"{name} → {'成功' if ok else '失败'}{f'（{count} 条）' if count is not None else ''}")
            if isinstance(result, dict) and result.get("results"):
                new_evidence.extend(result["results"])
            if hitl_enabled and name in hitl_tools:
                notes.append(f"写操作已获批准并执行：{name}")
            outgoing.append(
                ToolMessage(
                    content=json.dumps(result, ensure_ascii=False)[:TOOL_RESULT_MAX_CHARS],
                    tool_call_id=call_id,
                )
            )

        fields: Dict[str, Any] = {
            "messages": outgoing,
            "tool_calls": int(state.get("tool_calls") or 0) + executed,
            "evidence": merge_evidence(list(state.get("evidence") or []), new_evidence),
            "seen": prior,
        }
        if approval:
            fields["approval"] = approval
        if notes:
            fields["notes"] = list(state.get("notes") or []) + notes
        return fields

    return node_act


def node_draft(state: AgentState) -> Dict[str, Any]:
    """起草节点：组装最终提问消息（含证据与对话历史），并整理引用来源

    这里刻意不调用 LLM：token 生成由 runtime 负责（流式/同步共用同一份消息）。
    """
    intent = state.get("intent") or ""
    question = state.get("question") or ""
    history = list(state.get("history") or [])
    evidence = list(state.get("evidence") or [])

    if intent == INTENT_SMALLTALK:
        messages: List[BaseMessage] = [SystemMessage(content=SMALLTALK_SYSTEM_PROMPT)]
        messages += _history_messages(history)
        messages.append(HumanMessage(content=question))
        return _note(
            state,
            "起草：闲聊回应（不引用知识库）",
            answer_messages=messages,
            sources=[],
        )

    # 业务问答 / 流程导航 / 写操作：复用固定管线的系统提示词与上下文拼接，
    # 保证 shadow 模式下「固定管线 vs Agent」的差异只来自检索过程，而不是回答风格
    context = chat_service.build_context(evidence)
    # 长期记忆（改造 4）：与固定管线共用同一个组装函数，两路口径一致
    memory_block = ""
    ctx = current_context()
    if ctx is not None:
        memory_block = chat_service._memory_block(ctx.user, question, ctx.db)
    messages = chat_service._build_answer_messages(
        question, context, history, memory_block=memory_block
    )
    if intent == INTENT_ACTION:
        messages[0] = SystemMessage(content=messages[0].content + ACTION_CONSTRAINT)

    return _note(
        state,
        f"起草：基于 {len(evidence)} 条证据组装回答（意图={intent_label(intent)}）"
        + ("，已注入长期记忆" if memory_block else ""),
        answer_messages=messages,
        sources=to_sources(evidence),
    )


def make_verify_node(
    verifier_enabled: bool = True,
    max_repair_rounds: int = 2,
) -> Callable:
    """引用校验节点（改造 3）：校验「现有证据能否回答问题」，不合格则回炉补检索

    校验对象刻意是**问题 ↔ 证据**，而不是答案文本：

    - 答案在这一步还没生成（生成统一由 runtime 负责，流式/同步共用）；
    - 而且"证据没有覆盖问题"恰恰是回炉换词再检索**唯一有效**的时机——
      答案一旦生成，补检索也救不回来；
    - 问题里不含硬信息时（如"出差前要做什么"）规则会返回 uncertain → 放行，
      不会无谓地多检索一轮。

    答案生成后的事实校验与引用三态过滤由 runtime 调 `verifier.verify_answer` 完成。
    """

    def node_verify(state: AgentState) -> Dict[str, Any]:
        intent = state.get("intent") or ""
        if not verifier_enabled:
            return _note(
                state,
                "引用校验：未启用（AGENT_VERIFIER_ENABLED=false）",
                verification={"decision": "skipped", "mode": "disabled", "coverage": None},
                repair_pending=False,
            )
        if intent == INTENT_SMALLTALK:
            return _note(
                state,
                "引用校验：闲聊回答无需校验",
                verification={"decision": "skipped", "mode": "smalltalk", "coverage": None},
                repair_pending=False,
            )

        evidence = list(state.get("evidence") or [])
        question = state.get("search_question") or state.get("question") or ""
        result = verifier.verify_evidence(question, evidence)
        rounds = int(state.get("repair_rounds") or 0)

        can_repair = (
            result.get("decision") == "fail"
            and rounds < max(0, int(max_repair_rounds))
            and not is_timed_out(state)
        )
        fields: Dict[str, Any] = {"verification": result, "repair_pending": can_repair}

        if can_repair:
            fields["repair_rounds"] = rounds + 1
            # 追加一条提示，让规划模型知道"具体缺什么"，而不是盲目再搜一次
            fields["messages"] = [
                HumanMessage(
                    content=REPAIR_HUMAN_TEMPLATE.format(
                        hint=verifier.repair_hint(result),
                        round=rounds + 1,
                        max_rounds=max_repair_rounds,
                    )
                )
            ]
            return _note(state, verifier.summary_line(result) + " → 回炉补检索", **fields)

        if result.get("decision") == "fail":
            return _note(
                state,
                verifier.summary_line(result) + "（已达回炉上限，标记为知识库未收录）",
                **fields,
            )
        return _note(state, verifier.summary_line(result), **fields)

    return node_verify


def node_finalize(state: AgentState) -> Dict[str, Any]:
    """定稿节点：确定候选引用、标记缺口、收尾

    只做三件事，都是为了"把职责收敛到一个地方"，避免引用在多个节点里各拼一遍：
    1. 产出候选 `citations`（与固定管线 references 同结构，前端零改动）；
    2. 标记 `unanswered`——业务问题但一条证据都没有，就是"知识库答不上来"，
       这是巡检 Agent 做缺口聚类的原料（改造 5 会消费它）；
    3. 收尾 `finished_at`，并携带 verify 节点给出的 `verification` 结论。

    注意这里产出的引用是**候选**：答案还没生成，无法判断每条引用是否真被用到。
    生成后由 runtime 调 `verifier.filter_citations` 做三态过滤，最终引用以那份为准。
    """
    intent = state.get("intent") or ""
    evidence = list(state.get("evidence") or [])
    citations = list(state.get("citations") or []) or to_sources(evidence)
    unanswered = bool(should_retrieve(intent) and not citations)

    fields: Dict[str, Any] = {
        "citations": citations,
        "sources": citations,
        "unanswered": unanswered,
        "verification": dict(state.get("verification") or {}) or {"decision": "pending", "coverage": None},
        "finished_at": time.time(),
    }
    if unanswered:
        return _note(state, "定稿：无可用证据 → 标记为知识库缺口（供巡检采集）", **fields)
    if intent == INTENT_SMALLTALK:
        return _note(state, "定稿：闲聊回答（不含引用）", **fields)
    return _note(state, f"定稿：输出 {len(citations)} 条引用", **fields)


def _history_messages(history: List[Dict[str, str]]) -> List[BaseMessage]:
    messages: List[BaseMessage] = []
    for item in history or []:
        content = item.get("content") or ""
        if item.get("role") == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
    return messages


# ---------------------------------------------------------------------------
# 路由函数与图构建
# ---------------------------------------------------------------------------

def after_route(state: AgentState) -> str:
    """闲聊直接起草；其余先做预检索"""
    return "draft" if state.get("intent") == INTENT_SMALLTALK else "retrieve"


def after_retrieve(state: AgentState) -> str:
    """证据充足走快路径（跳过规划模型）；否则进入规划循环"""
    return "draft" if state.get("fast_path") else "plan"


def after_plan(state: AgentState) -> str:
    """模型给出了工具调用 → 执行；否则起草"""
    messages = list(state.get("messages") or [])
    last = messages[-1] if messages else None
    calls = getattr(last, "tool_calls", None) or []
    return "act" if calls else "draft"


def after_verify(state: AgentState) -> str:
    """校验结论决定：回炉补检索（回到 plan）还是直接定稿

    回炉有上限（`AGENT_VERIFIER_MAX_REPAIR_ROUNDS`），且超时/预算耗尽时不再回炉，
    因此这条回边不会形成死循环。
    """
    return "plan" if state.get("repair_pending") else "finalize"


def build_agent_graph(
    plan_llm,
    tools: List[BaseTool],
    max_tool_calls: int = 4,
    max_llm_calls: int = 6,
    plan_timeout: int = 20,
    top_k: int = 8,
    allowed_tool_names: Optional[set] = None,
    checkpointer=None,
    fast_path_min_hits: int = 0,
    hitl_enabled: bool = False,
    hitl_tools: Optional[List[str]] = None,
    verifier_enabled: bool = True,
    max_repair_rounds: int = 2,
):
    """构建并编译 Agent 图

    plan_llm 需为已绑定工具的可调用对象（`.invoke(messages)`）；
    allowed_tool_names 为 None 时放开传入的全部工具，否则按白名单收窄
    （runtime 会传入当前用户 + 当前意图允许的工具集合）；
    checkpointer 非空时启用状态持久化（人工中断审批 HITL 需要——没有检查点，
    图无法"停下来再接着跑"，`interrupt()` 也就无从恢复）。
    hitl_enabled 为 True 时，`hitl_tools` 里的写操作在执行前必须经人工批准。
    verifier_enabled 为 True 时，起草后插入引用校验节点；校验不合格且
    未超 `max_repair_rounds` 时会回炉补检索。
    """
    tools_by_name = {t.name: t for t in tools}
    allowed = set(allowed_tool_names) if allowed_tool_names is not None else set(tools_by_name)

    graph = StateGraph(AgentState)
    graph.add_node("route", node_route)
    graph.add_node(
        "retrieve",
        make_retrieve_node(tools_by_name, top_k, max_tool_calls, fast_path_min_hits),
    )
    graph.add_node(
        "plan",
        make_plan_node(plan_llm, max_tool_calls, max_llm_calls, plan_timeout, max_tool_calls),
    )
    graph.add_node(
        "act",
        make_act_node(
            tools_by_name,
            allowed,
            hitl_tools=set(hitl_tools or ()),
            hitl_enabled=hitl_enabled,
        ),
    )
    graph.add_node("draft", node_draft)
    graph.add_node("verify", make_verify_node(verifier_enabled, max_repair_rounds))
    graph.add_node("finalize", node_finalize)

    graph.set_entry_point("route")
    graph.add_conditional_edges("route", after_route, {"retrieve": "retrieve", "draft": "draft"})
    graph.add_conditional_edges("retrieve", after_retrieve, {"plan": "plan", "draft": "draft"})
    graph.add_conditional_edges("plan", after_plan, {"act": "act", "draft": "draft"})
    graph.add_edge("act", "plan")
    graph.add_edge("draft", "verify")
    # 校验不合格 → 回炉补检索（回到 plan）；通过/超上限 → 定稿
    graph.add_conditional_edges("verify", after_verify, {"plan": "plan", "finalize": "finalize"})
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer) if checkpointer is not None else graph.compile()
