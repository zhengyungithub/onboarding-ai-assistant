"""备忘录（待办事项）相关的请求/响应模型"""
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field


class MemoCreate(BaseModel):
    title: str = Field(..., max_length=200)
    content: Optional[str] = None
    priority: Optional[str] = "medium"  # low | medium | high
    due_date: Optional[datetime] = None
    folder_id: Optional[str] = None  # 所属文件夹，NULL=未分组


class MemoUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    is_completed: Optional[bool] = None
    priority: Optional[str] = None
    due_date: Optional[datetime] = None
    folder_id: Optional[str] = None


class MemoResponse(BaseModel):
    id: str
    title: str
    content: Optional[str]
    is_completed: bool
    priority: str
    due_date: Optional[datetime]
    folder_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MemoListResponse(BaseModel):
    total: int
    items: List[MemoResponse]


# ---------- 文件夹 ----------

class MemoFolderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    parent_id: Optional[str] = None  # 父文件夹，NULL=顶层


class MemoFolderUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    parent_id: Optional[str] = None  # 移动到指定父文件夹（可与 name 一起传）


class MemoFolderResponse(BaseModel):
    id: str
    name: str
    parent_id: Optional[str] = None
    memo_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class MemoFolderListResponse(BaseModel):
    total: int
    items: List[MemoFolderResponse]
