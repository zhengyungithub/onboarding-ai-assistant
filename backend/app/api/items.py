"""知识条目 CRUD API"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models import User
from app.schemas.knowledge import (
    KnowledgeItemCreate, KnowledgeItemUpdate,
    KnowledgeItemResponse, KnowledgeItemListResponse,
)
from app.services import knowledge_service
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/items", tags=["知识条目"])


@router.get("", response_model=KnowledgeItemListResponse)
def list_items(
    status: str = Query(None),
    keyword: str = Query(None),
    related_flow: str = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    total, items = knowledge_service.get_items(
        db, current_user, status=status, keyword=keyword,
        related_flow=related_flow, skip=skip, limit=limit
    )
    return KnowledgeItemListResponse(
        total=total,
        items=[KnowledgeItemResponse.model_validate(i) for i in items],
    )


@router.post("", response_model=KnowledgeItemResponse, status_code=201)
def create_item(
    data: KnowledgeItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = knowledge_service.create_item(db, current_user, data)
    return KnowledgeItemResponse.model_validate(item)


@router.get("/{item_id}", response_model=KnowledgeItemResponse)
def get_item(
    item_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = knowledge_service.get_item(db, item_id, current_user)
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    return KnowledgeItemResponse.model_validate(item)


@router.put("/{item_id}", response_model=KnowledgeItemResponse)
def update_item(
    item_id: str,
    data: KnowledgeItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = knowledge_service.update_item(db, item_id, current_user, data)
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    return KnowledgeItemResponse.model_validate(item)


@router.delete("/{item_id}", status_code=204)
def delete_item(
    item_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ok = knowledge_service.delete_item(db, item_id, current_user)
    if not ok:
        raise HTTPException(status_code=404, detail="条目不存在")


@router.post("/{item_id}/publish", response_model=KnowledgeItemResponse)
def publish_item(
    item_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = knowledge_service.publish_item(db, item_id, current_user)
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    return KnowledgeItemResponse.model_validate(item)
