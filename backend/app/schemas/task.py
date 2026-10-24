"""任务实例 Schema"""
import json
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class RelatedItemInfo(BaseModel):
    id: str
    title: str
    steps: Optional[List[str]] = None
    system_or_material: Optional[str] = None
    caution: Optional[str] = None

    class Config:
        from_attributes = True


class TaskProgressLogOut(BaseModel):
    """节点进展流水条目"""
    id: str
    node_id: str
    kind: str = "manual"  # manual / complete / incomplete
    note: str
    user_id: str = ""
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TaskProgressLogCreate(BaseModel):
    node_id: str = Field(..., description="节点 ID（必须属于该任务）")
    note: str = Field(..., description="进展内容，如「已把名单发给张老师，等回复」")

    @field_validator("note")
    @classmethod
    def strip_note(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("进展内容不能为空")
        if len(v) > 500:
            raise ValueError("进展内容最多 500 字")
        return v


class TaskNodeCompleteRequest(BaseModel):
    """完成节点的请求体（可选）：门禁节点必须带过闸说明"""
    remark: Optional[str] = Field(None, description="完成说明；门禁节点必填，至少 5 个字")

    @field_validator("remark")
    @classmethod
    def strip_remark(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None


class TaskNodeProgressOut(BaseModel):
    id: str
    node_id: str
    status: str  # pending / in_progress / completed / skipped
    remark: Optional[str] = None
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TaskNodeProgressWithNode(TaskNodeProgressOut):
    node_name: str = ""
    node_description: Optional[str] = None
    node_order: int = 0
    flow_name: Optional[str] = None
    related_item_ids: Optional[List[str]] = None
    related_items: Optional[List[RelatedItemInfo]] = None
    recent_logs: Optional[List[TaskProgressLogOut]] = None  # 最近 3 条进展
    log_count: int = 0  # 该节点进展总数
    # 工作流增强：依赖 / 负责人 / 门禁
    depends_on: Optional[List[str]] = None
    blocked_by: Optional[List[str]] = None  # 未完成的前置节点名称（空 = 未被阻塞）
    is_gate: bool = False
    owner_id: Optional[str] = None
    owner_name: str = ""
    can_complete: bool = True  # 当前用户此刻能否完成该节点


class TaskCreate(BaseModel):
    flow_id: Optional[str] = Field(None, description="主流程模板 ID（兼容旧版，单流程时用）")
    flow_ids: Optional[List[str]] = Field(None, description="关联的多个流程模板 ID 列表")
    name: str = Field(..., description="任务名称")
    due_date: Optional[datetime] = Field(None, description="交付截止时间（可选）")


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None  # in_progress / completed / paused
    due_date: Optional[datetime] = Field(None, description="交付截止时间；显式传 null 表示清空")


class TaskOut(BaseModel):
    id: str
    flow_id: Optional[str] = None
    flow_ids: Optional[List[str]] = None
    name: str
    current_node_id: Optional[str] = None
    status: str
    due_date: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    flow_name: str = ""
    flow_names: Optional[List[str]] = None

    @field_validator("flow_ids", mode="before")
    @classmethod
    def parse_flow_ids(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return [v]
        return v

    class Config:
        from_attributes = True


class TaskDetailOut(TaskOut):
    progress: List[TaskNodeProgressWithNode] = []
    flow_description: Optional[str] = None
