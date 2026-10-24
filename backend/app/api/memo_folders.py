"""备忘录文件夹 API"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models import User
from app.schemas.memo import (
    MemoFolderCreate, MemoFolderUpdate,
    MemoFolderResponse, MemoFolderListResponse,
)
from app.services import memo_service
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/memo-folders", tags=["备忘录文件夹"])


@router.get("", response_model=MemoFolderListResponse)
def list_folders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    folders = memo_service.list_folders(db, current_user.id)
    return MemoFolderListResponse(
        total=len(folders),
        items=[MemoFolderResponse.model_validate(f) for f in folders],
    )


@router.post("", response_model=MemoFolderResponse, status_code=201)
def create_folder(
    data: MemoFolderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    folder = memo_service.create_folder(db, current_user.id, data)
    if not folder:
        raise HTTPException(
            status_code=400,
            detail="创建失败：同级已存在同名文件夹、父文件夹无效或超过最大层级（3 层）",
        )
    return MemoFolderResponse.model_validate({**folder.__dict__, "memo_count": 0})


@router.put("/{folder_id}", response_model=MemoFolderResponse)
def rename_folder(
    folder_id: str,
    data: MemoFolderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重命名文件夹 和/或 移动到新的父文件夹"""
    folder = memo_service.update_folder(db, folder_id, current_user.id, data)
    if not folder:
        raise HTTPException(
            status_code=400,
            detail="操作失败：文件夹不存在、同级重名、目标父文件夹无效或会造成循环嵌套",
        )
    return MemoFolderResponse.model_validate(folder)


@router.delete("/{folder_id}", status_code=204)
def delete_folder(
    folder_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ok = memo_service.delete_folder(db, folder_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="文件夹不存在")
