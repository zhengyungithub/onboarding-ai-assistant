"""评测数据模型：TestCase + EvaluationResult"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, Integer, Boolean, JSON, Float
from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class TestCase(Base):
    """测试用例：问题 + 期望命中的条目 ID"""
    __tablename__ = "test_cases"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)
    question = Column(Text, nullable=False)
    expected_item_id = Column(String, nullable=False)  # 期望命中的知识条目 ID（第一个，兼容旧数据）
    expected_item_title = Column(String(200), nullable=True)  # 冗余存储标题便于展示
    # 多期望条目：一道题需要覆盖多个知识点时使用；**全部命中才算通过**
    # 旧数据为空 → 视为 [expected_item_id]（service 层兜底，无需回填）
    expected_item_ids = Column(JSON, nullable=True)
    expected_item_titles = Column(JSON, nullable=True)
    enabled = Column(Boolean, default=True)
    # 来源：由哪条「没用」反馈消息转换而来（用于去重，手工创建的用例为 None）
    source_message_id = Column(String, nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.now)


class EvaluationResult(Base):
    """评测结果：一次评测的记录"""
    __tablename__ = "evaluation_results"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)

    total_cases = Column(Integer, nullable=False, default=0)
    hit_count = Column(Integer, nullable=False, default=0)  # 命中数
    hit_rate = Column(Float, nullable=False, default=0.0)  # 命中率

    details = Column(JSON, nullable=True)  # 每条用例的命中详情

    created_at = Column(DateTime, default=datetime.now)


class ChatFeedback(Base):
    """问答反馈：有用/没用"""
    __tablename__ = "chat_feedbacks"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    is_helpful = Column(Boolean, nullable=True)  # True=有用, False=没用, None=未评价

    created_at = Column(DateTime, default=datetime.now)
