"""知识条目相关的请求/响应模型"""
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field


class KnowledgeItemCreate(BaseModel):
    title: str = Field(..., max_length=200)
    steps: Optional[List[str]] = None
    system_or_material: Optional[str] = None
    caution: Optional[str] = None
    related_flow: Optional[List[str]] = None
    status: Optional[str] = "draft"  # draft | published
    # 可见范围（仅管理员/超管生效）：private 仅自己 | all 全员共享 | custom 定向共享
    visibility: Optional[str] = None
    shared_user_ids: Optional[List[str]] = None


class KnowledgeItemUpdate(BaseModel):
    title: Optional[str] = None
    steps: Optional[List[str]] = None
    system_or_material: Optional[str] = None
    caution: Optional[str] = None
    related_flow: Optional[List[str]] = None
    status: Optional[str] = None
    visibility: Optional[str] = None
    shared_user_ids: Optional[List[str]] = None


class KnowledgeItemResponse(BaseModel):
    id: str
    user_id: str = ""
    is_shared: bool = False
    visibility: str = "private"
    shared_user_ids: Optional[List[str]] = None
    title: str
    steps: Optional[List[str]]
    system_or_material: Optional[str]
    caution: Optional[str]
    related_flow: Optional[List[str]]
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class KnowledgeItemListResponse(BaseModel):
    total: int
    items: List[KnowledgeItemResponse]


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1)


class ChatReference(BaseModel):
    item_id: str
    title: str
    score: float


class ChatResponse(BaseModel):
    answer: str
    references: List[ChatReference]
