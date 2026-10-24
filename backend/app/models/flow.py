"""流程数据模型：Flow + FlowNode"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, Integer, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship, foreign
from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class Flow(Base):
    """业务流程：如校招流程、入职流程"""
    __tablename__ = "flows"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)  # 多租户隔离
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)

    # 可见范围（三态）：private 仅自己 | all 全员共享 | custom 定向共享
    # is_shared 为派生兼容值（= visibility != 'private'），由 apply_visibility 统一维护
    is_shared = Column(Boolean, nullable=False, default=False, index=True)
    visibility = Column(String(20), nullable=False, default="private", server_default="private")
    shared_user_ids = Column(JSON, nullable=True)  # 定向共享的目标用户 ID 列表

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    nodes = relationship("FlowNode", back_populates="flow", cascade="all, delete-orphan", order_by="FlowNode.order")


class FlowNode(Base):
    """流程节点：有序步骤，关联知识条目

    工作流增强（增量）：
    - depends_on：前置节点 ID 列表（限定同一流程内），前置未完成时节点不可完成；
    - owner_id：节点负责人，非负责人（且非管理员）不能完成该节点；
    - is_gate：门禁节点，完成时必须填写过闸说明（remark），留下人工判断痕迹。
    """
    __tablename__ = "flow_nodes"

    id = Column(String, primary_key=True, default=generate_uuid)
    flow_id = Column(String, ForeignKey("flows.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    order = Column(Integer, nullable=False, default=0)  # 节点顺序
    related_item_ids = Column(JSON, nullable=True)  # 关联知识条目 ID 列表

    # --- 工作流增强字段（老库通过 init_migrate 幂等加列） ---
    depends_on = Column(JSON, nullable=True)  # 前置节点 ID 列表
    owner_id = Column(String, nullable=True)  # 负责人用户 ID（无外键，避免老库 ALTER 限制）
    is_gate = Column(Boolean, nullable=False, default=False, server_default="0")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    flow = relationship("Flow", back_populates="nodes")
    # 负责人姓名用于展示；owner_id 无外键，用 foreign() 显式声明连接条件（只读）
    owner = relationship(
        "User",
        primaryjoin="foreign(FlowNode.owner_id) == User.id",
        viewonly=True,
        uselist=False,
    )

    @property
    def owner_name(self) -> str:
        return self.owner.username if self.owner else ""
