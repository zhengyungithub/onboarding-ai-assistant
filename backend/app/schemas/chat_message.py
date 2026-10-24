"""对话消息相关的请求/响应模型"""
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel


class ChatMessageCreate(BaseModel):
    role: str  # user | assistant
    content: str
    references: Optional[List[dict]] = None
    feedback: Optional[str] = None  # useful | useless


class ChatMessageResponse(BaseModel):
    id: str
    role: str
    content: str
    references: Optional[List[dict]]
    feedback: Optional[str]
    feedback_reason: Optional[str] = None
    # 引用校验（改造 3）：覆盖率 + 被剔除引用数 + 关联的 Agent 轨迹 id。
    # 都可为空 —— 固定管线与历史消息不会带，前端按"未校验"呈现。
    citation_coverage: Optional[float] = None
    citation_dropped: Optional[int] = None
    agent_trace_id: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ChatMessageListResponse(BaseModel):
    total: int
    items: List[ChatMessageResponse]


class ChatFeedbackRequest(BaseModel):
    feedback: str  # useful | useless
    reason: Optional[str] = None  # 点「没用」时的原因说明（选填）


class ChatBatchDeleteRequest(BaseModel):
    ids: List[str]
