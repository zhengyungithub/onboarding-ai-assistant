"""Agent API（M1/M2/M3）：状态查询、轨迹查看、手动运行、写操作审批、引用校验

全部为新增接口，不改动任何既有接口：
- `GET  /api/agent/status`        当前模式与预算/HITL/校验配置（所有登录用户可见）
- `GET  /api/agent/tools`         当前用户可用的工具清单（调试/文档用）
- `POST /api/agent/run`           手动跑一次（管理员，同步返回完整轨迹；不写 chat_messages）
- `POST /api/agent/verify`        离线引用校验（管理员，对任意"回答+证据"校验，用于调参/度量）
- `POST /api/agent/approve/{id}`  人工审批写操作并从中断点恢复运行（管理员）
- `GET  /api/agent/runs`          运行轨迹列表（管理员）
- `GET  /api/agent/runs/{id}`     单次运行详情（管理员）
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import runtime, verifier
from app.agent.router import intent_label
from app.agent.tools import describe_tools
from app.core.deps import get_current_staff, get_current_user
from app.core.roles import ROLE_SUPER_ADMIN
from app.db.session import get_db
from app.models import AgentRun, User
from app.services import knowledge_service

router = APIRouter(prefix="/api/agent", tags=["Agent"])

# 处于这些状态的运行可以再次被审批
PENDING_STATUSES = ("awaiting_approval",)

# 离线校验时按 id 取证据的上限（防止一次请求拉太多正文）
MAX_EVIDENCE_ITEMS = 20


class ApprovalRequest(BaseModel):
    """人工审批答复

    - `approved=True`           批准全部待审写操作
    - `approved=False`          全部拒绝（默认，安全优先）
    - `approved_call_ids=[...]` 精确批准某几个写操作（多写操作时用）
    """

    approved: bool = Field(default=False, description="是否批准本次全部写操作")
    approved_call_ids: List[str] = Field(
        default_factory=list, description="精确批准某些 call_id（给出时以它为准）"
    )
    note: str = Field(default="", description="审批备注（写入轨迹，便于复盘）")


@router.get("/status")
def agent_status(current_user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """当前 Agent 开关与预算（前端可据此显示提示）"""
    return runtime.mode_info()


@router.get("/tools")
def agent_tools(current_user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """当前用户角色下可用的工具清单"""
    tools = describe_tools(current_user)
    return {"total": len(tools), "tools": tools}


class VerifyRequest(BaseModel):
    """离线引用校验请求

    证据有两种给法（可混用）：
    - 直接给 `evidence`（每项含 title / content）；
    - 给 `item_ids`，服务端按**当前用户的可见范围**取条目正文（越权拿不到就跳过）。
    """

    answer: str = Field(..., min_length=1, description="待校验的回答文本")
    question: str = Field(default="", description="原始问题（可选，仅用于回显）")
    evidence: List[Dict[str, Any]] = Field(default_factory=list, description="证据列表")
    item_ids: List[str] = Field(default_factory=list, description="知识条目 id（按可见范围取正文）")
    use_llm: Optional[bool] = Field(
        default=None, description="是否启用模型复核（默认按 AGENT_VERIFIER_USE_LLM）"
    )


@router.post("/verify")
def verify_answer_endpoint(
    payload: VerifyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
) -> Dict[str, Any]:
    """离线引用校验（管理员）：对任意「回答 + 证据」做两级校验

    用途：不打开 Agent 模式也能度量引用质量、调校验阈值、复盘某条回答。
    与线上路径共用同一份 `verifier`，因此结论口径完全一致。
    """
    evidence: List[Dict[str, Any]] = list(payload.evidence or [])
    if payload.item_ids:
        evidence += _load_evidence_by_ids(db, current_user, payload.item_ids)

    result = verifier.verify_answer(
        payload.answer, evidence, current_user, use_llm=payload.use_llm
    )
    return {
        "question": payload.question,
        "evidence_count": len(evidence),
        "summary": verifier.summary_line(result),
        "verification": result,
    }


def _load_evidence_by_ids(
    db: Session, user: User, item_ids: List[str]
) -> List[Dict[str, Any]]:
    """按 id 取条目正文作为证据（越权/不存在一律跳过，不泄露是否存在）

    条目正文不是一个字段，而是 title/steps/system_or_material/caution 的组合，
    因此复用模型自带的 `to_search_text()`（与检索侧口径一致）。
    """
    items: List[Dict[str, Any]] = []
    for item_id in list(item_ids)[:MAX_EVIDENCE_ITEMS]:
        try:
            item = knowledge_service.get_item(db, item_id, user)
        except Exception:  # noqa: BLE001
            continue
        if item is None:
            continue
        try:
            content = item.to_search_text()
        except Exception:  # noqa: BLE001
            content = ""
        items.append(
            {
                "id": item.id,
                "title": item.title or "",
                "content": content,
                "source_type": "knowledge_item",
                "score": 1.0,
            }
        )
    return items


@router.post("/run")
def agent_run_manual(
    question: str = Query(..., min_length=1, description="要跑 Agent 的问题"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
) -> Dict[str, Any]:
    """手动跑一次 Agent（管理员）——用于调试与效果对比，不落 chat_messages

    开启 HITL 时，若模型要执行写操作，这里会返回 `waiting_approval=True`
    与待审批清单；审批走 `POST /api/agent/approve/{run_id}`。
    """
    result = runtime.run_sync(current_user, question, db, generate=True, allow_hitl=True)
    return result


@router.post("/approve/{run_id}")
def approve_run(
    run_id: str,
    payload: ApprovalRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
) -> Dict[str, Any]:
    """审批挂起的写操作，并从中断点恢复该次运行

    只有"待审批"状态的运行可以被审批；恢复后原记录置为 approved/rejected，
    并额外写一条 mode=approve 的轨迹，保留完整审计链。
    """
    row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    if row.status not in PENDING_STATUSES:
        raise HTTPException(status_code=409, detail=f"该运行当前状态为 {row.status}，无需审批")
    if not row.thread_id:
        raise HTTPException(status_code=409, detail="该运行缺少检查点信息，无法恢复")

    owner = db.query(User).filter(User.id == row.user_id).first()
    if owner is None:
        raise HTTPException(status_code=404, detail="运行所属用户不存在")
    # 越权保护：只能审批自己的挂起操作（超管可代为处理）
    if current_user.id != row.user_id and current_user.role != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="无权审批他人的运行")

    decision = {
        "approved": payload.approved,
        "approved_call_ids": list(payload.approved_call_ids or []),
        "note": payload.note,
    }
    outcome = runtime.resume_run(
        owner,
        db,
        thread_id=row.thread_id,
        intent=row.intent or "",
        decision=decision,
    )

    answer = ""
    if not outcome.error and not outcome.waiting_approval:
        try:
            answer = runtime.generate_sync(owner, outcome.answer_messages)
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"{type(exc).__name__}: {exc}"

    # 原记录落定状态（避免长期停留在"待审批"）；恢复后的结果另写一条轨迹
    row.status = "approved" if (payload.approved or payload.approved_call_ids) else "rejected"
    db.add(row)
    db.commit()

    runtime.record_run(db, row.user_id, row.question, outcome, mode="approve", answer=answer)

    return {
        "run_id": row.id,
        "status": row.status,
        "answer": answer,
        "sources": outcome.sources,
        "notes": outcome.notes,
        "trace": outcome.trace,
        "intent": row.intent,
        "intent_label": intent_label(row.intent or ""),
        "waiting_approval": outcome.waiting_approval,
        "pending_writes": outcome.pending_writes,
        "error": outcome.error,
    }


@router.get("/runs")
def list_runs(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    mode: str = Query("", description="按模式过滤：off/shadow/on/sync，留空为全部"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
) -> Dict[str, Any]:
    """运行轨迹列表（管理员）"""
    query = db.query(AgentRun)
    if mode:
        query = query.filter(AgentRun.mode == mode)
    total = query.count()
    rows = (
        query.order_by(AgentRun.created_at.desc()).offset(skip).limit(limit).all()
    )
    return {
        "total": total,
        "items": [_serialize(row, with_trace=False) for row in rows],
    }


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
) -> Dict[str, Any]:
    """单次运行详情（含完整工具调用轨迹）"""
    row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return _serialize(row, with_trace=True)


def _serialize(row: AgentRun, with_trace: bool) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": row.id,
        "user_id": row.user_id,
        "question": row.question,
        "intent": row.intent,
        "intent_label": intent_label(row.intent or "") or None,
        "mode": row.mode,
        "status": row.status,
        "thread_id": row.thread_id,
        "tool_calls": row.tool_calls or 0,
        "llm_calls": row.llm_calls or 0,
        "duration_ms": row.duration_ms or 0,
        "citation_coverage": row.citation_coverage,
        "notes": row.notes or [],
        "sources": row.sources or [],
        "error": row.error or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if with_trace:
        data["trace"] = row.trace or []
        data["answer"] = row.answer or ""
    else:
        data["answer"] = (row.answer or "")[:200]
    return data
