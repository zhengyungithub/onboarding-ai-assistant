"""Agent 运行时：模式分发（off / shadow / on）、预算与降级、轨迹落库

这一层负责三件事，都是"工程化"而非"模型能力"的部分：

1. **灰度分发**：`AGENT_MODE` 控制走原管线还是 Agent 管线。
   - `off`    行为与改造前完全一致（默认值）
   - `shadow` 用户仍看原管线结果，Agent 在后台线程只读跑一遍并记录轨迹（可配采样率控制成本）
   - `on`     用户走 Agent 管线；**任何异常都回退到原管线**，绝不让用户看到报错

2. **降级兜底**：Agent 失败 → 回落固定管线；预检索无命中 → 直接返回与固定管线一致的兜底话术。

3. **可观测**：每次运行的状态、决策轨迹、工具调用与耗时写入 `agent_runs`，
   shadow 模式据此与固定管线做质量对比（而不是靠体感判断"改好没有"）。
"""
from __future__ import annotations

import json
import random
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from sqlalchemy.orm import Session

from app.agent import verifier
from app.agent.context import AgentContext, agent_context
from app.agent.graph import build_agent_graph
from app.agent.router import (
    INTENT_SMALLTALK,
    classify_intent,
    intent_label,
    should_retrieve,
    tools_for_intent,
)
from app.agent.state import AgentState, new_state, to_sources
from app.core.config import settings
from app.models import AgentRun, User
from app.services import chat_service, llm_service

# 与固定管线完全一致的兜底话术（保证预览与线上行为一字不差）
NO_RESULT_TEXT = "抱歉，知识库中暂无相关内容。"
# 轨迹落库时 answer 的最大长度
ANSWER_MAX_CHARS = 4000
VALID_MODES = ("off", "shadow", "on")


@dataclass
class AgentOutcome:
    """一次 Agent 运行的结果（不含最终回答文本）"""

    intent: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)
    answer_messages: List[BaseMessage] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    llm_calls: int = 0
    duration_ms: int = 0
    error: str = ""
    short_circuit: str = ""  # 非空 = 不生成回答，直接返回该文本
    thread_id: str = ""      # 检查点线程 id（恢复中断时用）
    waiting_approval: bool = False                        # 图已挂起，等人工审批
    pending_writes: List[Dict[str, Any]] = field(default_factory=list)  # 待审批的写操作
    unanswered: bool = False  # 知识库答不上来（缺口采集用）
    citations: List[Dict[str, Any]] = field(default_factory=list)
    # ---- 引用校验（改造 3）----
    evidence: List[Dict[str, Any]] = field(default_factory=list)   # 生成前累积的证据（用于答案侧校验）
    verification: Dict[str, Any] = field(default_factory=dict)     # {evidence:{...}, answer:{...}}
    citation_coverage: Optional[float] = None                      # 答案硬信息支撑率（可为 None）
    citation_dropped: int = 0                                     # 被剔除的"回答没用到"的引用数

    @property
    def ok(self) -> bool:
        return not self.error and not self.short_circuit and not self.waiting_approval


def current_mode() -> str:
    """当前 Agent 模式（非法值一律视为 off，安全优先）"""
    mode = (settings.AGENT_MODE or "off").strip().lower()
    return mode if mode in VALID_MODES else "off"


# 检查点存储器（进程内单例；构造失败不影响主流程）
_checkpointer = None
_checkpointer_prepared = False


def get_checkpointer():
    """检查点存储器：未启用时返回 None

    `AGENT_USE_CHECKPOINT` 或 `AGENT_HITL_ENABLED` 任一为真即启用——
    HITL 依赖"图能停下来再接着跑"，没有检查点就无从恢复。
    启用后可用 SQLite 持久化图状态；SQLite 连接需 `check_same_thread=False`，
    因为 shadow 模式会在后台线程里跑图。构造失败时退回内存存储，不影响问答。
    """
    global _checkpointer, _checkpointer_prepared
    if _checkpointer_prepared:
        return _checkpointer
    _checkpointer_prepared = True

    if not (settings.AGENT_USE_CHECKPOINT or settings.AGENT_HITL_ENABLED):
        _checkpointer = None
        return None

    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        path = settings.AGENT_CHECKPOINT_PATH.strip()
        if not path:
            base = Path(__file__).resolve().parents[2]
            path = str(base / "data" / "agent_checkpoints.db")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        _checkpointer = saver
        print(f"[初始化] Agent 检查点已启用：{path}")
    except Exception as exc:  # noqa: BLE001
        from langgraph.checkpoint.memory import MemorySaver

        _checkpointer = MemorySaver()
        print(f"[警告] 检查点存储初始化失败，已退回内存存储: {exc}")
    return _checkpointer


def mode_info() -> Dict[str, Any]:
    """当前 Agent 配置（供前端与运维查看）"""
    return {
        "mode": current_mode(),
        "max_tool_calls": settings.AGENT_MAX_TOOL_CALLS,
        "max_llm_calls": settings.AGENT_MAX_LLM_CALLS,
        "timeout_seconds": settings.AGENT_TIMEOUT_SECONDS,
        "plan_timeout_seconds": settings.AGENT_PLAN_TIMEOUT_SECONDS,
        "shadow_rate": settings.AGENT_SHADOW_RATE,
        "fast_path_min_hits": settings.AGENT_FAST_PATH_MIN_HITS,
        "use_checkpoint": settings.AGENT_USE_CHECKPOINT,
        "hitl_enabled": settings.AGENT_HITL_ENABLED,
        "hitl_tools": hitl_tools(),
        # 引用校验（改造 3）
        "verifier_enabled": bool(settings.AGENT_VERIFIER_ENABLED),
        "verifier_use_llm": bool(settings.AGENT_VERIFIER_USE_LLM),
        "verifier_pass_threshold": settings.AGENT_VERIFIER_PASS_THRESHOLD,
        "verifier_repair_threshold": settings.AGENT_VERIFIER_REPAIR_THRESHOLD,
        "verifier_max_repair_rounds": settings.AGENT_VERIFIER_MAX_REPAIR_ROUNDS,
        # 三层记忆（改造 4）：关闭时短期轮数回到 4 轮，全链路归零
        "memory": _memory_info(),
        # 主动巡检（改造 5）：离线批处理，配置只供运维确认
        "patrol": _patrol_info(),
        "ready": get_checkpointer() is not None or not settings.AGENT_HITL_ENABLED,
    }


def _memory_info() -> Dict[str, Any]:
    """记忆配置摘要（内存模块不可用时返回空 dict，不影响状态接口）"""
    try:
        from app.agent import memory

        return memory.mode_info()
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 读取记忆配置失败: {exc}")
        return {}


def _patrol_info() -> Dict[str, Any]:
    """巡检配置摘要（巡检模块不可用时返回空 dict，不影响状态接口）"""
    try:
        from app.agent import patrol

        return patrol.mode_info()
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 读取巡检配置失败: {exc}")
        return {}


def hitl_tools() -> List[str]:
    """解析需要人工审批的工具名单（逗号分隔，容忍空项与空格）"""
    raw = settings.AGENT_HITL_TOOLS or ""
    return [n.strip() for n in raw.split(",") if n.strip()]


# ---------------------------------------------------------------------------
# 核心：跑图（不生成回答文本）
# ---------------------------------------------------------------------------

def _resolve_hitl(allow_hitl: bool, checkpointer) -> bool:
    """HITL 三重条件：调用方允许 + 配置开启 + 检查点可用（缺一不可）"""
    return bool(allow_hitl and settings.AGENT_HITL_ENABLED and checkpointer is not None)


def _build_runtime_graph(plan_llm, tools: List[BaseTool], checkpointer, hitl_enabled: bool):
    """按当前配置装配图（跑图与恢复中断必须用同一套参数，否则恢复会失败）"""
    return build_agent_graph(
        plan_llm,
        tools,
        max_tool_calls=settings.AGENT_MAX_TOOL_CALLS,
        max_llm_calls=settings.AGENT_MAX_LLM_CALLS,
        plan_timeout=settings.AGENT_PLAN_TIMEOUT_SECONDS,
        top_k=settings.AGENT_SEARCH_TOP_K,
        allowed_tool_names={t.name for t in tools},
        checkpointer=checkpointer,
        fast_path_min_hits=settings.AGENT_FAST_PATH_MIN_HITS,
        hitl_enabled=hitl_enabled,
        hitl_tools=hitl_tools(),
        verifier_enabled=bool(settings.AGENT_VERIFIER_ENABLED and settings.AGENT_VERIFIER_REPAIR_ENABLED),
        max_repair_rounds=int(settings.AGENT_VERIFIER_MAX_REPAIR_ROUNDS),
    )


def _pending_writes(interrupts) -> List[Dict[str, Any]]:
    """从 LangGraph 的中断对象里取出待审批写操作"""
    writes: List[Dict[str, Any]] = []
    for item in interrupts or ():
        value = getattr(item, "value", None)
        if isinstance(value, dict):
            writes.extend(value.get("writes") or [])
    return writes


def _fill_outcome(outcome: AgentOutcome, final: AgentState, bind_error: str = "") -> AgentOutcome:
    """把跑图结果回填到 outcome（跑图与恢复共用，保证两条路径口径一致）"""
    outcome.sources = list(final.get("citations") or final.get("sources") or [])
    outcome.citations = outcome.sources
    outcome.answer_messages = list(final.get("answer_messages") or [])
    outcome.notes = list(final.get("notes") or [])
    outcome.unanswered = bool(final.get("unanswered"))
    outcome.tool_calls = int(final.get("tool_calls") or 0)
    outcome.llm_calls = int(final.get("llm_calls") or 0)
    outcome.evidence = list(final.get("evidence") or [])
    outcome.verification = dict(final.get("verification") or {})
    if bind_error:
        outcome.notes.append(f"模型不支持工具调用，已降级为「仅预检索」模式（{bind_error}）")
    return outcome


def prepare(
    user: User,
    question: str,
    db: Session,
    plan_llm=None,
    tools: Optional[List[BaseTool]] = None,
    source: str = "agent",
    history: Optional[List[Dict[str, str]]] = None,
    search_question: Optional[str] = None,
    thread_id: Optional[str] = None,
    allow_hitl: bool = False,
) -> AgentOutcome:
    """跑一遍 Agent 图，返回「引用 + 提问消息 + 轨迹」

    不生成回答的原因：流式与同步两种生成方式共用同一份消息，避免逻辑漂移；
    同时让"跑图"这一步可以脱离 LLM 生成单独测试。

    `allow_hitl=True` 且配置开启 HITL 时，图可能停在"等人工审批"上——
    此时 `outcome.waiting_approval` 为真、`pending_writes` 给出待批写操作，
    由调用方（`/api/agent/approve/{run_id}`）带着 `thread_id` 恢复。
    """
    started = time.perf_counter()
    intent = classify_intent(question, history)
    outcome = AgentOutcome(intent=intent)

    # 整条链路（工具绑定 → 建图 → 跑图）都在保护范围内：
    # 任何一步失败都只是"这次降级到固定管线"，绝不让用户看到报错
    try:
        checkpointer = get_checkpointer()
        hitl_on = _resolve_hitl(allow_hitl, checkpointer)

        if tools is None:
            tools = tools_for_intent(intent, user, allow_hitl_writes=hitl_on)

        # 规划用的模型：需要绑定工具；模型不支持 tool calling 时降级为普通模型
        if plan_llm is None:
            plan_llm, bind_error = _bind_tools_safely(user, tools)
        else:
            bind_error = ""

        graph = _build_runtime_graph(plan_llm, tools, checkpointer, hitl_on)

        if history is None:
            history, search_question = _load_context(user, question, db, search_question)

        state = new_state(
            question=question,
            search_question=search_question or question,
            history=history,
            timeout_seconds=settings.AGENT_TIMEOUT_SECONDS,
        )

        run_config = None
        if checkpointer is not None:
            import uuid as _uuid

            outcome.thread_id = thread_id or f"{source}:{user.id}:{_uuid.uuid4()}"
            run_config = {"configurable": {"thread_id": outcome.thread_id}}

        with agent_context(user, db, source=source) as ctx:
            final: AgentState = (
                graph.invoke(state, config=run_config) if run_config else graph.invoke(state)
            )
            outcome.trace = list(ctx.trace)
    except Exception as exc:  # noqa: BLE001 —— 任何失败一律降级，不让用户看到报错
        traceback.print_exc()  # 只降级不静默：堆栈进日志，便于定位
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.notes = ["运行失败，已降级为原有问答管线"]
        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    # 图挂在"等人工审批"上：还没有最终回答，交给调用方去审批后再恢复
    interrupts = final.get("__interrupt__") or ()
    if interrupts:
        outcome.waiting_approval = True
        outcome.pending_writes = _pending_writes(interrupts)
        outcome.notes = list(final.get("notes") or []) + ["暂停：写操作待人工审批，尚未执行"]
        outcome.tool_calls = int(final.get("tool_calls") or 0)
        outcome.llm_calls = int(final.get("llm_calls") or 0)
        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    _fill_outcome(outcome, final, bind_error)

    # 业务问题但没有任何证据 → 直接返回与固定管线一致的兜底话术，不必再调 LLM
    if should_retrieve(intent) and not outcome.sources:
        outcome.short_circuit = NO_RESULT_TEXT
        outcome.notes.append("无检索结果，返回与原有管线一致的兜底话术")

    outcome.duration_ms = int((time.perf_counter() - started) * 1000)
    return outcome


def resume_run(
    user: User,
    db: Session,
    *,
    thread_id: str,
    intent: str,
    decision: Any,
    source: str = "approve",
    plan_llm=None,
) -> AgentOutcome:
    """从「等人工审批」的中断点继续跑图

    `decision` 为人工答复：`{"approved": true|false}` 或
    `{"approved_call_ids": ["call_x"]}`（精确批准某几个写操作）。
    恢复必须复用与原始运行**完全一致**的图参数（工具集/预算/HITL 配置），
    否则 LangGraph 会因为图结构不匹配而拒绝恢复。
    """
    started = time.perf_counter()
    outcome = AgentOutcome(intent=intent, thread_id=thread_id)

    try:
        checkpointer = get_checkpointer()
        if checkpointer is None:
            raise RuntimeError("未启用检查点，无法恢复中断（请开启 AGENT_USE_CHECKPOINT 或 AGENT_HITL_ENABLED）")

        hitl_on = _resolve_hitl(True, checkpointer)
        tools = tools_for_intent(intent, user, allow_hitl_writes=hitl_on)
        if plan_llm is None:
            plan_llm, bind_error = _bind_tools_safely(user, tools)
        else:
            bind_error = ""

        graph = _build_runtime_graph(plan_llm, tools, checkpointer, hitl_on)
        config = {"configurable": {"thread_id": thread_id}}

        with agent_context(user, db, source=source) as ctx:
            final: AgentState = graph.invoke(Command(resume=decision), config=config)
            outcome.trace = list(ctx.trace)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.notes = ["审批恢复失败"]
        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    interrupts = final.get("__interrupt__") or ()
    if interrupts:
        # 又挂起了（还有未审批的写操作）
        outcome.waiting_approval = True
        outcome.pending_writes = _pending_writes(interrupts)
        outcome.notes = list(final.get("notes") or []) + ["暂停：仍有写操作待人工审批"]
        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    _fill_outcome(outcome, final, bind_error)
    if should_retrieve(intent) and not outcome.sources:
        outcome.short_circuit = NO_RESULT_TEXT
    outcome.duration_ms = int((time.perf_counter() - started) * 1000)
    return outcome


def _bind_tools_safely(user: User, tools: List[BaseTool]):
    """绑定工具；模型不支持 tool calling 时不报错，返回未绑定的模型

    这是很重要的降级：即使模型不支持 function calling，
    预检索（由代码保证）依然会执行，用户仍能拿到带引用的答案——
    只是失去了"模型自主补检索"的能力，而不是整个功能不可用。
    """
    llm = llm_service.get_llm_for_user(user)
    if not tools:
        return llm, ""
    try:
        return llm.bind_tools(tools), ""
    except Exception as exc:  # noqa: BLE001
        return llm, f"{type(exc).__name__}: {exc}"


def _load_context(
    user: User, question: str, db: Session, search_question: Optional[str]
) -> tuple[List[Dict[str, str]], str]:
    """复用固定管线的上下文准备逻辑（历史 + 追问改写），保证两者口径一致

    返回顺序固定为 (对话历史, 检索问题)——注意 `chat_service._prepare_question`
    返回的是反过来的 (检索问题, 历史)，这里统一顺序，避免调用方拿错。
    """
    if search_question:
        history = chat_service.get_recent_history(db, user.id) if db is not None else []
        return history, search_question
    if db is None:
        return [], question
    prepared_question, history = chat_service._prepare_question(user, question, db)
    return history, prepared_question


# ---------------------------------------------------------------------------
# 回答生成
# ---------------------------------------------------------------------------

def generate_sync(user: User, messages: List[BaseMessage], llm=None) -> str:
    """同步生成回答（shadow 对比、评测、管理接口用）"""
    model = llm or llm_service.get_llm_for_user(user)
    response = model.invoke(messages)
    return getattr(response, "content", "") or ""


async def generate_stream(user: User, messages: List[BaseMessage], llm=None) -> AsyncGenerator[str, None]:
    """流式生成回答（逐段 yield 文本）"""
    model = llm or llm_service.get_llm_for_user(user)
    async for chunk in model.astream(messages):
        content = getattr(chunk, "content", "")
        if content:
            yield content


# ---------------------------------------------------------------------------
# 生成后引用校验（改造 3 的收口处）
# ---------------------------------------------------------------------------

def finalize_citations(outcome: AgentOutcome, answer: str, user: User | None = None) -> AgentOutcome:
    """对已生成的回答做事实校验 + 引用三态过滤

    这是"引用不相关"痛点的收口：**只有真被回答用到的证据才留在引用列表里**，
    没被用到的直接剔除（至少保留 `AGENT_VERIFIER_MIN_CITATIONS` 条作为保底）。

    刻意不改写答案正文——流式已经发给用户了，事后偷偷改文本不诚实；
    校验结论通过三个可观测出口表达：引用三态、覆盖率、降级提示。

    只读、永不抛异常：任何异常都退化为"沿用候选引用 + 未校验"。
    """
    if not settings.AGENT_VERIFIER_ENABLED:
        return outcome
    if outcome.intent == INTENT_SMALLTALK or not outcome.sources or not answer:
        return outcome

    try:
        result = verifier.verify_answer(answer, outcome.evidence or [], user)
    except Exception as exc:  # noqa: BLE001 —— 校验失败不能反过来弄挂问答
        traceback.print_exc()
        print(f"[警告] 引用校验失败（沿用候选引用）: {exc}")
        return outcome

    citations = result.get("citations") or outcome.sources
    outcome.sources = citations
    outcome.citations = citations
    outcome.verification = {**outcome.verification, "answer": result}
    outcome.citation_coverage = result.get("coverage")
    outcome.citation_dropped = len(result.get("dropped") or [])
    outcome.notes = list(outcome.notes) + [verifier.summary_line(result)]
    return outcome


def verification_payload(outcome: AgentOutcome) -> Dict[str, Any]:
    """给前端/落库用的校验摘要（None 字段会被剔除，避免噪音）

    顶层是"证据校验"（生成前）的结论，`answer` 子对象是"答案校验"（生成后）的结论。
    """
    evidence_result = dict(outcome.verification or {})
    answer_result = evidence_result.pop("answer", None) or {}
    payload: Dict[str, Any] = {
        "coverage": outcome.citation_coverage,
        "decision": answer_result.get("decision") or evidence_result.get("decision") or "skipped",
        "unsupported": list(answer_result.get("unsupported") or [])[:8],
        "dropped": int(answer_result.get("dropped") and len(answer_result["dropped"]) or outcome.citation_dropped or 0),
        "kept_ratio": answer_result.get("kept_ratio"),
    }
    if evidence_result.get("stage") == "evidence":
        payload["evidence_coverage"] = evidence_result.get("coverage")
    return {k: v for k, v in payload.items() if v is not None}


def run_sync(
    user: User,
    question: str,
    db: Session,
    generate: bool = True,
    history: Optional[List[Dict[str, str]]] = None,
    llm=None,
    allow_hitl: bool = False,
) -> Dict[str, Any]:
    """完整跑一遍（跑图 + 生成），返回结构化结果（不落 chat_messages）"""
    outcome = prepare(
        user, question, db, source="sync", history=history, allow_hitl=allow_hitl
    )

    answer = outcome.short_circuit
    if outcome.waiting_approval:
        answer = ""  # 图已挂起：先让用户审批，审批后恢复才生成回答
    elif not answer and not outcome.error:
        try:
            answer = generate_sync(user, outcome.answer_messages, llm=llm) if generate else ""
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"{type(exc).__name__}: {exc}"
            answer = ""

    # 生成后引用校验（改造 3）：过滤引用 + 标注 verified 三态 + 计算覆盖率
    if generate and answer:
        finalize_citations(outcome, answer, user)

    return {
        "answer": answer,
        "sources": outcome.sources,
        "intent": outcome.intent,
        "intent_label": intent_label(outcome.intent),
        "notes": outcome.notes,
        "trace": outcome.trace,
        "tool_calls": outcome.tool_calls,
        "llm_calls": outcome.llm_calls,
        "duration_ms": outcome.duration_ms,
        "error": outcome.error,
        # HITL：图挂在审批上时返回待批写操作与恢复所需的 thread_id
        "waiting_approval": outcome.waiting_approval,
        "pending_writes": outcome.pending_writes,
        "thread_id": outcome.thread_id,
        "unanswered": outcome.unanswered,
        # 引用校验（改造 3）
        "verification": verification_payload(outcome),
        "citation_coverage": outcome.citation_coverage,
    }


# ---------------------------------------------------------------------------
# 轨迹落库
# ---------------------------------------------------------------------------

def record_run(
    db: Session,
    user_id: str,
    question: str,
    outcome: AgentOutcome,
    mode: str,
    answer: str = "",
    status: str = "",
) -> Optional[AgentRun]:
    """写入一条运行轨迹（失败不影响主流程）"""
    try:
        if not status:
            if outcome.waiting_approval:
                status = "awaiting_approval"
            elif outcome.error or outcome.short_circuit:
                status = "fallback"
            else:
                status = "ok"
        run = AgentRun(
            user_id=user_id,
            question=question[:2000],
            intent=outcome.intent,
            mode=mode,
            status=status,
            thread_id=outcome.thread_id or None,
            answer=(answer or "")[:ANSWER_MAX_CHARS],
            sources=outcome.sources or [],
            trace=outcome.trace or [],
            notes=outcome.notes or [],
            tool_calls=outcome.tool_calls,
            llm_calls=outcome.llm_calls,
            duration_ms=outcome.duration_ms,
            error=(outcome.error or "")[:1000],
            citation_coverage=outcome.citation_coverage,
            citation_dropped=outcome.citation_dropped,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        _trim_runs(db, user_id)
        return run
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[警告] Agent 轨迹写入失败（不影响问答）: {exc}")
        return None


def _trim_runs(db: Session, user_id: str) -> None:
    """每个用户只保留最近 N 条轨迹，避免 shadow 模式把库撑大"""
    keep = max(50, int(settings.AGENT_RUN_KEEP))
    try:
        total = db.query(AgentRun).filter(AgentRun.user_id == user_id).count()
        if total <= keep:
            return
        old = (
            db.query(AgentRun)
            .filter(AgentRun.user_id == user_id)
            .order_by(AgentRun.created_at.desc())
            .offset(keep)
            .all()
        )
        for row in old:
            db.delete(row)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[警告] 轨迹清理失败: {exc}")


# ---------------------------------------------------------------------------
# shadow 模式：后台只读跑一遍，用于与固定管线对比
# ---------------------------------------------------------------------------

def maybe_run_shadow(user_id: str, question: str) -> bool:
    """采样后异步跑一次只读 Agent，结果落轨迹表（不返回给用户、不影响响应）

    只读的关键：shadow 模式不绑定写工具，避免"用户没看到的过程"产生副作用。
    """
    if current_mode() != "shadow":
        return False
    rate = max(0.0, min(1.0, float(settings.AGENT_SHADOW_RATE)))
    if rate <= 0 or random.random() > rate:
        return False
    # 闲聊没有对比价值：连线程都不必起（省一次模型调用与一次检索）
    if classify_intent(question) == INTENT_SMALLTALK:
        return False

    thread = threading.Thread(
        target=_shadow_worker, args=(user_id, question), daemon=True, name="agent-shadow"
    )
    thread.start()
    return True


def _shadow_worker(user_id: str, question: str) -> None:
    """后台线程：独立数据库会话跑只读 Agent 并落轨迹"""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            return
        intent = classify_intent(question)
        if intent == INTENT_SMALLTALK:
            return  # 闲聊没有对比价值，省一次模型调用
        tools = [t for t in tools_for_intent(intent, user) if t.name != "memo_create"]
        outcome = prepare(user, question, db, tools=tools, source="shadow")
        answer = ""
        if not outcome.error and not outcome.short_circuit:
            try:
                answer = generate_sync(user, outcome.answer_messages)
            except Exception as exc:  # noqa: BLE001
                outcome.error = f"{type(exc).__name__}: {exc}"
        # shadow 也要走引用校验，否则"Agent vs 固定管线"的引用质量没法对比
        if answer:
            finalize_citations(outcome, answer, user)
        record_run(db, user_id, question, outcome, mode="shadow", answer=answer)
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] shadow 运行失败（不影响用户）: {exc}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 事件流（富事件，供前端「过程」面板使用）
# ---------------------------------------------------------------------------

def _sse(payload: Any) -> str:
    import json

    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_agent_events(
    user: User, question: str, db: Session, sink: Optional[Dict[str, Any]] = None
) -> AsyncGenerator[str, None]:
    """Agent 流式事件（/api/chat/agent/stream）

    事件类型：start → intent → step* → tool* → references → 文本 → citation_check → references → done
    注意：过程事件在生成开始前一次性下发（跑图阶段已全部确定）；
    逐步实时推送需要改用 astream_events 的逐步回调，属后续里程碑。

    末尾会再发一次 `references`（引用校验过滤后的版本）与 `citation_check`
    （覆盖率/剔除条数）——这两条只在富事件协议里出现，消费方按 type 取用即可。
    """
    outcome = prepare(user, question, db, source="agent-stream")

    if outcome.error or outcome.short_circuit:
        # 降级：把固定管线的流式结果透传出去（事件协议保持一致）
        yield _sse({"type": "fallback", "reason": outcome.error or "no_result"})
        async for chunk in chat_service.get_answer_stream(user, question, db):
            yield chunk
        return

    yield _sse({"type": "start", "mode": "on"})
    yield _sse(
        {"type": "intent", "intent": outcome.intent, "label": intent_label(outcome.intent)}
    )
    for note in outcome.notes:
        yield _sse({"type": "step", "text": note})
    for step in outcome.trace:
        yield _sse(
            {
                "type": "tool",
                "tool": step.get("tool"),
                "ok": step.get("ok"),
                "ms": step.get("ms"),
                "summary": step.get("summary"),
            }
        )
    yield _sse({"type": "references", "references": outcome.sources})

    chunks: List[str] = []
    try:
        async for text in generate_stream(user, outcome.answer_messages):
            chunks.append(text)
            yield _sse(text)
    except Exception as exc:  # noqa: BLE001
        yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    # ---- 生成后引用校验：只把真被回答用到的证据留在引用列表里 ----
    finalize_citations(outcome, "".join(chunks), user)
    payload = verification_payload(outcome)
    if sink is not None:
        sink["references"] = outcome.sources
        sink["verification"] = payload
        sink["citation_coverage"] = outcome.citation_coverage
        sink["citation_dropped"] = outcome.citation_dropped
        sink["outcome"] = outcome
    yield _sse(
        {
            "type": "citation_check",
            "coverage": payload.get("coverage"),
            "decision": payload.get("decision"),
            "dropped": payload.get("dropped", 0),
            "unsupported": payload.get("unsupported", []),
        }
    )
    yield _sse({"type": "references", "references": outcome.sources, "verified": True})

    yield _sse({"type": "done", "tool_calls": outcome.tool_calls, "duration_ms": outcome.duration_ms})
    yield _sse("[DONE]")


# ---------------------------------------------------------------------------
# 兼容旧前端的流式输出（references JSON + 纯文本，协议与固定管线一致）
# ---------------------------------------------------------------------------

async def stream_agent_legacy(
    user: User, question: str, db: Session, sink: Optional[Dict[str, Any]] = None
) -> AsyncGenerator[str, None]:
    """Agent 模式的流式输出，但**保持固定管线的 SSE 协议**

    这样 AGENT_MODE=on 时前端无需任何改动即可切换；
    富事件协议走 /api/chat/agent/stream，供后续的「过程」面板使用。

    引用校验的落地方式（为什么不能直接改流里的 references）：
    前端只在**第一个** data 里解析 references，之后再发 JSON 会被当成正文。
    所以首包仍发候选引用（用户立刻能看到来源），生成结束后把**过滤后的引用**
    与覆盖率写入 `sink`，由调用方（`_agent_stream_with_persistence`）用于持久化——
    前端流结束后会重新拉取消息，那时展示的就是校验后的引用。
    """
    import json

    outcome = prepare(user, question, db, source="chat")

    if outcome.error or outcome.short_circuit:
        if outcome.short_circuit and not outcome.error:
            yield f"data: {outcome.short_circuit}\n\n"
            yield "data: [DONE]\n\n"
            return
        # 真正的失败：回落固定管线（用户视角无差别）
        async for chunk in chat_service.get_answer_stream(user, question, db):
            yield chunk
        return

    if outcome.sources:
        yield f"data: {json.dumps({'references': outcome.sources}, ensure_ascii=False)}\n\n"

    chunks: List[str] = []
    async for text in generate_stream(user, outcome.answer_messages):
        chunks.append(text)
        yield f"data: {text}\n\n"

    answer = "".join(chunks)
    finalize_citations(outcome, answer, user)

    if sink is not None:
        sink["references"] = outcome.sources
        sink["verification"] = verification_payload(outcome)
        sink["citation_coverage"] = outcome.citation_coverage
        sink["citation_dropped"] = outcome.citation_dropped
        sink["outcome"] = outcome

    yield "data: [DONE]\n\n"


def run_in_thread(func, *args, **kwargs) -> None:
    """在守护线程里执行（供需要"不阻塞响应"的场景复用）"""
    threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()
