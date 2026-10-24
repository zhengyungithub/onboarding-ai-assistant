"""流程相关的请求/响应模型"""
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field


class FlowNodeCreate(BaseModel):
    name: str = Field(..., max_length=200)
    description: Optional[str] = None
    order: int = 0
    related_item_ids: Optional[List[str]] = None
    # 工作流增强：前置节点（同一流程内，前置未完成则不可完成）
    depends_on: Optional[List[str]] = Field(None, description="前置节点 ID 列表")
    owner_id: Optional[str] = Field(None, description="节点负责人用户 ID")
    is_gate: bool = Field(False, description="门禁节点：完成时必须填写过闸说明")


class FlowNodeResponse(BaseModel):
    id: str
    flow_id: str
    name: str
    description: Optional[str]
    order: int
    related_item_ids: Optional[List[str]]
    depends_on: Optional[List[str]] = None
    owner_id: Optional[str] = None
    owner_name: str = ""
    is_gate: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FlowCreate(BaseModel):
    name: str = Field(..., max_length=200)
    description: Optional[str] = None
    nodes: Optional[List[FlowNodeCreate]] = None
    # 可见范围（仅管理员/超管生效）：private 仅自己 | all 全员共享 | custom 定向共享
    visibility: Optional[str] = None
    shared_user_ids: Optional[List[str]] = None


class FlowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    visibility: Optional[str] = None
    shared_user_ids: Optional[List[str]] = None


class FlowResponse(BaseModel):
    id: str
    user_id: str = ""
    is_shared: bool = False
    visibility: str = "private"
    shared_user_ids: Optional[List[str]] = None
    name: str
    description: Optional[str]
    nodes: List[FlowNodeResponse] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FlowListResponse(BaseModel):
    total: int
    flows: List[FlowResponse]


class NextStepRequest(BaseModel):
    """下一步提醒请求"""
    flow_id: str
    current_node_id: str


class NextStepResponse(BaseModel):
    """下一步提醒响应"""
    next_node: Optional[FlowNodeResponse]
    related_items: List[dict]  # 关联知识条目摘要
    advice: str  # LLM 生成的操作要点
