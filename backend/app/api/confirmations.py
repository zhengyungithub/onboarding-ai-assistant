"""文档确认清单 API

三组路由：
- /api/source-documents/{doc_id}/confirmations  文档维度：发起确认、查看进度
- /api/confirmations/...                        确认人维度：待我确认、详情、提交、撤销、取链接
- /api/public/confirmations/{token}...          免登录通道（默认关闭，关闭时一律 404）
"""
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user
from app.db.session import get_db
from app.models import User
from app.services import confirmation_service
from app.services.confirmation_service import ConfirmationError

doc_router = APIRouter(prefix="/api/source-documents", tags=["文档确认"])
router = APIRouter(prefix="/api/confirmations", tags=["待我确认"])
public_router = APIRouter(prefix="/api/public/confirmations", tags=["免登录确认"])


def _http(e: ConfirmationError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.message)


class RequestConfirmationsIn(BaseModel):
    user_ids: List[str] = []
    message: Optional[str] = None


class RespondIn(BaseModel):
    action: str            # confirmed | rejected
    comment: Optional[str] = None


# --------------------------------------------------------------------------
# 文档维度
# --------------------------------------------------------------------------

@doc_router.post("/{doc_id}/confirmations")
def request_confirmations(
    doc_id: str,
    data: RequestConfirmationsIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """对文档发起确认（仅文档本人或管理员）"""
    try:
        return confirmation_service.request_confirmations(
            db, doc_id, current_user, data.user_ids, data.message
        )
    except ConfirmationError as e:
        raise _http(e)


@doc_router.get("/{doc_id}/confirmations")
def list_document_confirmations(
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查看文档的确认进度（文档可读即可查看，不含 token）"""
    try:
        return confirmation_service.list_for_document(db, doc_id, current_user)
    except ConfirmationError as e:
        raise _http(e)


# --------------------------------------------------------------------------
# 确认人维度
# --------------------------------------------------------------------------

@router.get("/mine")
def list_my_confirmations(
    status: str = Query("pending", description="pending / confirmed / rejected / all"),
    limit: int = Query(100, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """待我确认（默认只看未处理）"""
    try:
        return confirmation_service.list_mine(db, current_user, status=status, limit=limit)
    except ConfirmationError as e:
        raise _http(e)


# 注意：必须注册在 /{conf_id} 之前，否则 "pending-count" 会被当成 conf_id
@router.get("/pending-count")
def get_pending_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """待我确认的条数（侧边栏徽标用）"""
    return {"count": confirmation_service.pending_count(db, current_user.id)}


@router.get("/contacts")
def list_contacts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """可邀请确认的人（排除自己；只含 id + 用户名）"""
    return {"items": confirmation_service.list_contacts(db, current_user)}


@router.get("/{conf_id}")
def get_confirmation(
    conf_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认记录详情（含文档正文预览）"""
    try:
        return confirmation_service.get_detail(db, conf_id, current_user)
    except ConfirmationError as e:
        raise _http(e)


@router.get("/{conf_id}/download")
def download_confirmation_document(
    conf_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """下载确认对应的原始文件（前端按原版式预览也走这里）"""
    try:
        doc = confirmation_service.download_document(db, conf_id, current_user)
    except ConfirmationError as e:
        raise _http(e)
    if not doc.file_path or not os.path.exists(doc.file_path):
        raise HTTPException(status_code=404, detail="原始文件不存在")
    return FileResponse(
        path=doc.file_path,
        filename=doc.filename,
        media_type="application/octet-stream",
    )


@router.post("/{conf_id}/respond")
def respond_confirmation(
    conf_id: str,
    data: RespondIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """提交确认结果：confirmed（无误）/ rejected（有问题，需填意见）"""
    try:
        return confirmation_service.respond(
            db, conf_id, current_user, data.action, data.comment
        )
    except ConfirmationError as e:
        raise _http(e)


@router.post("/{conf_id}/revoke")
def revoke_confirmation(
    conf_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """撤销一条待确认（仅发起人或管理员）"""
    try:
        return confirmation_service.revoke(db, conf_id, current_user)
    except ConfirmationError as e:
        raise _http(e)


@router.post("/{conf_id}/link")
def issue_public_link(
    conf_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取回/补发免登录确认链接（仅发起人或管理员）"""
    try:
        return confirmation_service.issue_link(db, conf_id, current_user)
    except ConfirmationError as e:
        raise _http(e)


# --------------------------------------------------------------------------
# 免登录通道（默认关闭）
# --------------------------------------------------------------------------

@public_router.get("/{token}")
def public_get_confirmation(token: str, db: Session = Depends(get_db)):
    """免登录查看待确认文档（只暴露这一份文档的必要信息）"""
    try:
        return confirmation_service.public_view(db, token)
    except ConfirmationError as e:
        raise _http(e)


@public_router.post("/{token}/respond")
def public_respond(token: str, data: RespondIn, db: Session = Depends(get_db)):
    """免登录提交确认结果"""
    try:
        return confirmation_service.public_respond(db, token, data.action, data.comment)
    except ConfirmationError as e:
        raise _http(e)


@public_router.get("/{token}/download")
def public_download(token: str, db: Session = Depends(get_db)):
    """免登录下载原始文件（确认人要看的就是那张表）"""
    if not settings.DOC_CONFIRM_PUBLIC_ENABLED:
        raise HTTPException(status_code=404, detail="链接无效或已失效")
    try:
        doc = confirmation_service.public_download(db, token)
    except ConfirmationError as e:
        raise _http(e)
    if not doc.file_path or not os.path.exists(doc.file_path):
        raise HTTPException(status_code=404, detail="原始文件不存在")
    return FileResponse(
        path=doc.file_path,
        filename=doc.filename,
        media_type="application/octet-stream",
    )
