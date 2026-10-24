"""备忘录（待办事项）CRUD API"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models import User
from app.schemas.memo import (
    MemoCreate, MemoUpdate,
    MemoResponse, MemoListResponse,
)
from app.services import memo_service
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/memos", tags=["备忘录"])


@router.get("", response_model=MemoListResponse)
def list_memos(
    is_completed: bool = Query(None),
    priority: str = Query(None),
    keyword: str = Query(None),
    folder_id: str = Query(None),
    ungrouped: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    total, items = memo_service.get_memos(
        db, current_user.id,
        is_completed=is_completed, priority=priority,
        keyword=keyword, folder_id=folder_id, ungrouped=ungrouped,
        skip=skip, limit=limit,
    )
    return MemoListResponse(
        total=total,
        items=[MemoResponse.model_validate(i) for i in items],
    )


@router.post("", response_model=MemoResponse, status_code=201)
def create_memo(
    data: MemoCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    memo = memo_service.create_memo(db, current_user, data)
    return MemoResponse.model_validate(memo)


@router.get("/{memo_id}", response_model=MemoResponse)
def get_memo(
    memo_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    memo = memo_service.get_memo(db, memo_id, current_user.id)
    if not memo:
        raise HTTPException(status_code=404, detail="备忘录不存在")
    return MemoResponse.model_validate(memo)


@router.put("/{memo_id}", response_model=MemoResponse)
def update_memo(
    memo_id: str,
    data: MemoUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    memo = memo_service.update_memo(db, memo_id, current_user.id, data)
    if not memo:
        raise HTTPException(status_code=404, detail="备忘录不存在")
    return MemoResponse.model_validate(memo)


@router.patch("/{memo_id}/toggle", response_model=MemoResponse)
def toggle_memo(
    memo_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    memo = memo_service.toggle_memo(db, memo_id, current_user.id)
    if not memo:
        raise HTTPException(status_code=404, detail="备忘录不存在")
    return MemoResponse.model_validate(memo)


@router.delete("/{memo_id}", status_code=204)
def delete_memo(
    memo_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ok = memo_service.delete_memo(db, memo_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="备忘录不存在")
