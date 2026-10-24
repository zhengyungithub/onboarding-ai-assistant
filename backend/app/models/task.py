"""任务实例数据模型：TaskInstance + TaskNodeProgress + TaskProgressLog"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.db.session import Base
import json


def generate_uuid() -> str:
    return str(uuid.uuid4())


class TaskInstance(Base):
    """任务实例：基于流程模板创建的具体工作任务"""
    __tablename__ = "task_instances"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)  # 多租户隔离
    flow_id = Column(String, ForeignKey("flows.id", ondelete="CASCADE"), nullable=True)  # 主流程（兼容旧数据）
    flow_ids = Column(Text, nullable=True)  # 关联的多个流程 ID 列表（JSON 字符串），如 ["id1","id2"]
    name = Column(String(200), nullable=False)  # 任务名称，如"2026届校招"
    current_node_id = Column(String, nullable=True)  # 当前所在节点
    status = Column(String(20), nullable=False, default="in_progress")  # in_progress / completed / paused
    due_date = Column(DateTime, nullable=True)  # 交付截止时间（可选，为空表示不跟踪到期）
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    flow = relationship("Flow")
    progress = relationship("TaskNodeProgress", back_populates="task", cascade="all, delete-orphan")
    logs = relationship("TaskProgressLog", back_populates="task", cascade="all, delete-orphan")

    @property
    def flow_id_list(self) -> list:
        """解析 flow_ids 为列表，兼容旧数据（只有 flow_id）"""
        if self.flow_ids:
            try:
                return json.loads(self.flow_ids)
            except (json.JSONDecodeError, TypeError):
                return [self.flow_id] if self.flow_id else []
        return [self.flow_id] if self.flow_id else []


class TaskNodeProgress(Base):
    """任务节点完成记录"""
    __tablename__ = "task_node_progress"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("task_instances.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id = Column(String, ForeignKey("flow_nodes.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(20), nullable=False, default="pending")  # pending / in_progress / completed / skipped
    remark = Column(String(500), nullable=True)  # 备注
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    task = relationship("TaskInstance", back_populates="progress")
    node = relationship("FlowNode")


class TaskProgressLog(Base):
    """任务进展流水：节点上的事件流（「记一笔」+ 状态变更）

    为什么不用 TaskNodeProgress.remark：
    remark 是覆盖式单条备注，只留得下"最后一句话"，看不出一件事是怎么推进的。
    被打断后回来，需要的不是结论而是过程——「昨天做到哪、卡在谁那里、约了什么时间」。
    因此这里改成只追加的事件流，永不覆盖。

    kind 区分来源：
    - manual      用户手动「记一笔」
    - complete    标记节点完成（自动生成，正文是节点名）
    - incomplete  回退节点为未完成（自动生成）
    """
    __tablename__ = "task_progress_logs"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("task_instances.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id = Column(String, ForeignKey("flow_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)  # 预留多人协作；当前任务按用户隔离
    kind = Column(String(20), nullable=False, default="manual")  # manual / complete / incomplete
    note = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.now, index=True)

    task = relationship("TaskInstance", back_populates="logs")
