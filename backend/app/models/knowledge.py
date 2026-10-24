"""知识条目数据模型"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, Integer, JSON, Boolean
from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)  # 多租户隔离

    title = Column(String(200), nullable=False)
    steps = Column(JSON, nullable=True)  # 操作步骤数组
    system_or_material = Column(String(500), nullable=True)  # 涉及的系统或物料
    caution = Column(Text, nullable=True)  # 注意事项
    related_flow = Column(JSON, nullable=True)  # 关联流程ID数组

    status = Column(String(20), nullable=False, default="draft")  # draft | published

    # 可见范围（三态）：private 仅自己 | all 全员共享 | custom 定向共享
    # is_shared 为派生兼容值（= visibility != 'private'），由 apply_visibility 统一维护
    is_shared = Column(Boolean, nullable=False, default=False, index=True)
    visibility = Column(String(20), nullable=False, default="private", server_default="private")
    shared_user_ids = Column(JSON, nullable=True)  # 定向共享的目标用户 ID 列表

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def to_search_text(self) -> str:
        """将条目拼接为检索用文本"""
        parts = [f"标题：{self.title}"]
        if self.system_or_material:
            parts.append(f"系统/物料：{self.system_or_material}")
        if self.steps:
            steps_text = "；".join([f"{i+1}.{s}" for i, s in enumerate(self.steps)])
            parts.append(f"步骤：{steps_text}")
        if self.caution:
            parts.append(f"注意事项：{self.caution}")
        if self.related_flow:
            flow_names = "、".join(self.related_flow) if isinstance(self.related_flow, list) else str(self.related_flow)
            parts.append(f"关联流程：{flow_names}")
        return "\n".join(parts)
