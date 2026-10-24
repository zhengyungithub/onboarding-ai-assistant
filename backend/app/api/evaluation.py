"""评测 API：测试用例 CRUD + 评测 + 统计 + 反馈"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models import User
from app.core.deps import get_current_user, get_current_staff
from app.core.roles import ROLE_SUPER_ADMIN
from app.services import evaluation_service
from app.services.evaluation_service import FeedbackToCaseError
from app.schemas.evaluation import (
    TestCaseCreate, TestCaseResponse, TestCaseListResponse,
    EvaluateResponse, StatsResponse, FeedbackCreate,
    PendingFeedbackListResponse, PendingFeedbackItem, FeedbackToCaseRequest,
)

router = APIRouter(prefix="/api/evaluation", tags=["评测"])


def _case_payload(case) -> TestCaseResponse:
    """统一补齐多期望条目字段（老数据自动按单条目回落）"""
    ids, titles = evaluation_service.case_expected(case)
    return TestCaseResponse(
        id=case.id,
        question=case.question,
        expected_item_id=case.expected_item_id,
        expected_item_title=case.expected_item_title,
        expected_item_ids=ids,
        expected_item_titles=titles,
        enabled=case.enabled,
        source_message_id=case.source_message_id,
        created_at=case.created_at,
    )


@router.get("/cases", response_model=TestCaseListResponse)
def list_cases(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    total, cases = evaluation_service.get_test_cases(db, current_user.id, skip, limit)
    return TestCaseListResponse(
        total=total,
        cases=[_case_payload(c) for c in cases],
    )


@router.post("/cases", response_model=TestCaseResponse, status_code=201)
def create_case(
    data: TestCaseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    try:
        case = evaluation_service.create_test_case(db, current_user.id, data)
    except FeedbackToCaseError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return _case_payload(case)


@router.delete("/cases/{case_id}", status_code=204)
def delete_case(
    case_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    if not evaluation_service.delete_test_case(db, case_id, current_user.id):
        raise HTTPException(status_code=404, detail="测试用例不存在")


@router.post("/run", response_model=EvaluateResponse)
def run_evaluation(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    """一键评测：批量跑测试集"""
    result = evaluation_service.run_evaluation(db, current_user.id, current_user)
    return EvaluateResponse(**result)


@router.get("/stats", response_model=StatsResponse)
def get_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    return StatsResponse(**evaluation_service.get_stats(db, current_user.id))


@router.get("/citation-stats")
def citation_stats(
    limit: int = Query(200, ge=10, le=2000),
    scope: str = Query("me", description="me=只看自己；all=全站（仅超管）"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    """引用正确率统计（改造 3）

    返回最近若干条 AI 回答的引用校验覆盖率均值、达标率、被剔除引用数等，
    是"引用不相关"这个痛点的可度量指标——也是改造效果的硬证据。
    """
    if scope == "all":
        if current_user.role != ROLE_SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="仅超级管理员可查看全站引用正确率")
        target: Optional[str] = None
    else:
        target = current_user.id
    return evaluation_service.citation_stats(db, target, limit)


@router.post("/feedback", status_code=201)
def create_feedback(
    data: FeedbackCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    """问答反馈：有用/没用"""
    evaluation_service.create_feedback(db, current_user.id, data.question, data.answer or "", data.is_helpful)
    return {"status": "ok"}


# ===== 负反馈转测试用例（闭环） =====

@router.get("/pending-feedback", response_model=PendingFeedbackListResponse)
def list_pending_feedback(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    """待处理负反馈：被标记「没用」且尚未转成测试用例的 AI 回答"""
    items = evaluation_service.get_pending_feedback(db)
    return PendingFeedbackListResponse(
        total=len(items),
        items=[PendingFeedbackItem(**it) for it in items],
    )


@router.post("/feedback-to-case", response_model=TestCaseResponse, status_code=201)
def feedback_to_case(
    data: FeedbackToCaseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    """将一条负反馈人工确认后转为测试用例"""
    try:
        case = evaluation_service.create_case_from_feedback(
            db, current_user.id, data.message_id, data.question,
            data.expected_item_id, data.expected_item_ids,
        )
    except FeedbackToCaseError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return _case_payload(case)
