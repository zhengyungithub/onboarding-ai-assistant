"""对话消息 CRUD API（持久化、批量删除、反馈）"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models import User
from app.schemas.chat_message import (
    ChatMessageResponse, ChatMessageListResponse,
    ChatFeedbackRequest, ChatBatchDeleteRequest,
)
from app.services import chat_message_service
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/chat/messages", tags=["对话消息"])


@router.get("", response_model=ChatMessageListResponse)
def list_messages(
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取当前用户的对话历史"""
    total, items = chat_message_service.get_messages(db, current_user.id, limit=limit)
    return ChatMessageListResponse(
        total=total,
        items=[ChatMessageResponse.model_validate(i) for i in items],
    )


@router.delete("", status_code=200)
def batch_delete_messages(
    req: ChatBatchDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量删除对话消息"""
    if not req.ids:
        raise HTTPException(status_code=400, detail="请选择要删除的消息")
    deleted = chat_message_service.batch_delete_messages(db, req.ids, current_user.id)
    return {"deleted": deleted}


@router.delete("/all", status_code=200)
def clear_all_messages(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """清空当前用户所有对话"""
    deleted = chat_message_service.clear_messages(db, current_user.id)
    return {"deleted": deleted}


@router.patch("/{message_id}/feedback", response_model=ChatMessageResponse)
def set_feedback(
    message_id: str,
    req: ChatFeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """对 AI 回答进行反馈（有用/没用）"""
    if req.feedback not in ("useful", "useless"):
        raise HTTPException(status_code=400, detail="反馈类型无效")
    msg = chat_message_service.set_feedback(db, message_id, current_user.id, req.feedback, req.reason)
    if not msg:
        raise HTTPException(status_code=404, detail="消息不存在")
    return ChatMessageResponse.model_validate(msg)
