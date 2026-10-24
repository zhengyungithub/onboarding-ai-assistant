"""Agent 状态定义（LangGraph 图的状态结构）

设计约束（很重要）：
- **状态里不放 ORM Session / 用户对象**。它们无法被检查点序列化，也是并发隐患；
  用户与数据库会话统一走 `app.agent.context` 的 ContextVar 注入。
- 状态只放「可序列化的数据 + 消息列表」，这样后续打开检查点、做中断恢复、
  甚至把状态落库做回放都不需要改结构。
"""
from __future__ import annotations

import time
from typing import Annotated, Any, Dict, List, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """一次 Agent 运行的状态"""

    # ---- 对话与消息 ----
    # 规划循环的消息序列（SystemMessage(规划提示) + HumanMessage(问题+证据) + AI tool_calls + ToolMessage）
    messages: Annotated[List[BaseMessage], add_messages]
    question: str                 # 用户原始问题
    search_question: str          # 用于检索的问题（追问时已改写为自包含形式）
    history: List[Dict[str, str]]  # 最近几轮对话（与固定管线口径一致）

    # ---- 决策 ----
    intent: str                   # smalltalk | kb_qa | flow_nav | action
    plan_note: str                # 规划节点的自述（为什么继续检索 / 为什么停止）
    fast_path: bool               # 证据充足，跳过规划模型调用直接起草

    # ---- 证据 ----
    evidence: List[Dict[str, Any]]  # 累积的检索结果（按 id 去重）
    sources: List[Dict[str, Any]]   # 最终引用（与固定管线同结构，供前端跳转）
    seen: List[str]                 # 已执行过的工具调用指纹（防重复检索，含预检索那一次）

    # ---- 定稿（finalize 节点产出）----
    citations: List[Dict[str, Any]]  # 定稿后的引用（带 verified 三态：true / "partial" / 被剔除）
    verification: Dict[str, Any]     # 引用校验结果（{decision, coverage, missing, mode}）
    unanswered: bool                 # 本次是否属于"知识库答不上来"（供巡检采集缺口）
    # ---- 引用校验（Verifier / 改造 3）----
    repair_rounds: int               # 已回炉补检索的轮次（上限 AGENT_VERIFIER_MAX_REPAIR_ROUNDS）
    repair_pending: bool             # 本轮校验判定"需回炉"（after_verify 据此走回边）

    # ---- 写操作人工审批（HITL）----
    pending_writes: List[Dict[str, Any]]  # 因等待审批而未执行的写操作
    approval: Dict[str, Any]              # 审批结果（从中断点恢复时写入）

    # ---- 产出 ----
    # draft 节点组装好的最终提问消息；token 生成由 runtime 统一流式输出
    answer_messages: List[BaseMessage]

    # ---- 预算与可观测 ----
    tool_calls: int               # 已发生的工具调用次数（含确定性预检索）
    llm_calls: int                # 已发生的 LLM 调用次数
    started_at: float             # 起始时间戳（秒）
    finished_at: float            # 定稿时间戳（秒）
    timeout_seconds: int          # 硬超时
    notes: List[str]              # 人类可读的决策轨迹（前端「过程」面板直接展示）
    error: str                    # 异常信息（非空表示该次运行降级）


def new_state(
    question: str,
    search_question: str,
    history: List[Dict[str, str]] | None = None,
    timeout_seconds: int = 45,
) -> AgentState:
    """构造初始状态"""
    return AgentState(
        messages=[],
        question=question,
        search_question=search_question,
        history=list(history or []),
        intent="",
        plan_note="",
        fast_path=False,
        evidence=[],
        sources=[],
        seen=[],
        citations=[],
        verification={},
        unanswered=False,
        repair_rounds=0,
        repair_pending=False,
        pending_writes=[],
        approval={},
        answer_messages=[],
        tool_calls=0,
        llm_calls=0,
        started_at=time.time(),
        finished_at=0.0,
        timeout_seconds=timeout_seconds,
        notes=[],
        error="",
    )


def elapsed_seconds(state: AgentState) -> float:
    return time.time() - float(state.get("started_at") or time.time())


def is_timed_out(state: AgentState) -> bool:
    return elapsed_seconds(state) > float(state.get("timeout_seconds") or 45)


def merge_evidence(
    existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """合并证据并按 id 去重（保留先到的，即分数更高的那批顺序）"""
    merged: List[Dict[str, Any]] = list(existing or [])
    seen = {str(e.get("id")) for e in merged}
    for ref in incoming or []:
        rid = str(ref.get("id"))
        if rid in seen:
            continue
        seen.add(rid)
        merged.append(ref)
    return merged


def evidence_brief(
    evidence: List[Dict[str, Any]], per_item: int = 240, max_items: int = 8
) -> str:
    """把证据压成给「规划模型」看的摘要

    规划只需要知道"目前手上有什么、够不够"，不需要全文；给全文会白白烧 token。
    """
    if not evidence:
        return "（暂无）"
    lines = []
    for i, ref in enumerate(evidence[:max_items], 1):
        content = (ref.get("content") or "").replace("\n", " ").strip()
        if len(content) > per_item:
            content = content[:per_item] + "…"
        lines.append(
            f"{i}. [{ref.get('source_type', 'knowledge_item')}] {ref.get('title', '')}"
            f"（score={round(float(ref.get('score') or 0), 3)}）：{content}"
        )
    if len(evidence) > max_items:
        lines.append(f"…另有 {len(evidence) - max_items} 条未展示")
    return "\n".join(lines)


def to_sources(evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """转成前端引用结构（与固定管线 references 字段完全一致，前端无需改动）"""
    return [
        {
            "item_id": ref.get("id"),
            "title": ref.get("title", ""),
            "score": round(float(ref.get("score") or 0), 4),
            "source_type": ref.get("source_type", "knowledge_item"),
        }
        for ref in evidence or []
    ]
