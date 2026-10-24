"""对话消息数据模型（持久化到数据库，按用户隔离）"""
import uuid
from datetime import datetime
from sqlalchemy import Column, DateTime, Float, Integer, String, Text, JSON
from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)  # 多租户隔离

    role = Column(String(20), nullable=False)  # user | assistant
    content = Column(Text, nullable=False, default="")
    references = Column(JSON, nullable=True)  # 引用来源列表（含 verified 三态标记）
    feedback = Column(String(20), nullable=True)  # useful | useless | None
    feedback_reason = Column(Text, nullable=True)  # 点「没用」时用户填写的原因
    # 引用校验覆盖率（改造 3）：该条回答的硬信息被引用原文支撑的比例
    citation_coverage = Column(Float, nullable=True)
    # 被剔除的引用数（回答没用到的来源）；引用支撑率 = 保留 ÷ (保留 + 剔除)
    citation_dropped = Column(Integer, nullable=True)
    # 关联的 agent_runs.id（管理员可从消息反查 Agent 决策轨迹）
    agent_trace_id = Column(String, nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.now)
