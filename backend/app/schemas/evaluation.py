"""评测相关的请求/响应模型"""
from typing import Optional, List, Any
from datetime import datetime
from pydantic import BaseModel, Field


class TestCaseCreate(BaseModel):
    question: str = Field(..., min_length=1)
    # 兼容旧客户端：只传单条目；新客户端传 expected_item_ids（可多个，全部命中才算通过）
    expected_item_id: Optional[str] = None
    expected_item_ids: Optional[List[str]] = None
    expected_item_title: Optional[str] = None


class TestCaseResponse(BaseModel):
    id: str
    question: str
    expected_item_id: str
    expected_item_title: Optional[str]
    # 多期望条目（含单条目场景，统一以列表返回，前端只认这个）
    expected_item_ids: List[str] = []
    expected_item_titles: List[str] = []
    enabled: bool
    source_message_id: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TestCaseListResponse(BaseModel):
    total: int
    cases: List[TestCaseResponse]


class EvaluateResponse(BaseModel):
    """评测结果"""
    total_cases: int
    hit_count: int
    hit_rate: float
    details: List[dict]  # [{question, expected_item_id, actual_items, hit: bool}]


class StatsResponse(BaseModel):
    """统计数据"""
    total_items: int
    published_items: int
    total_cases: int
    latest_hit_rate: Optional[float]
    feedback_helpful: int
    feedback_unhelpful: int


class FeedbackCreate(BaseModel):
    question: str
    answer: Optional[str] = None
    is_helpful: bool


class PendingFeedbackItem(BaseModel):
    """待处理的「没用」反馈（含配对提问与当时引用来源）"""
    message_id: str
    user_id: str
    username: str
    question: str
    answer: str
    reason: str = ""
    references: List[dict] = []
    created_at: datetime


class PendingFeedbackListResponse(BaseModel):
    total: int
    items: List[PendingFeedbackItem]


class FeedbackToCaseRequest(BaseModel):
    """把一条「没用」反馈转成测试用例"""
    message_id: str
    question: str = Field(..., min_length=1)
    expected_item_id: Optional[str] = None
    # 可多选：一道题要覆盖多个知识点时，全部命中才算通过
    expected_item_ids: Optional[List[str]] = None
