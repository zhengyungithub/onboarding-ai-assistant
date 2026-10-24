"""三层记忆的数据模型（改造 4）

对应设计方案里的 L1.5 / L2 / L3 三层：

| 表 | 层级 | 解决的问题 |
|----|------|-----------|
| `conversation_summaries` | L1.5 会话摘要 | 会话超过 12 轮后，"最近 6 轮原文"装不下更早的结论 |
| `user_memory`           | L2 用户长期偏好 | 同一个用户反复问同一类业务，系统却每次都从零开始 |
| `unanswered_questions`  | L3 组织记忆 | 知识库答不上来的问题散落在日志里，没人聚类成缺口 |

L1（最近 N 轮原文）不建新表——`chat_messages` 已经是权威来源；
L1.6（检索式历史）的向量存 Qdrant 的 `chat_history` collection，
这里不重复存储（payload 里只放指向 `chat_messages.id` 的指针）。
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text

from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class ConversationSummary(Base):
    """会话滚动摘要（L1.5）

    `covered_message_count` 是这份摘要"覆盖到第几条消息"（按 `chat_messages`
    的时间正序计数）。它同时充当**增量更新的水位线**：只有当未被覆盖的消息数
    达到 `AGENT_MEMORY_SUMMARY_INTERVAL_TURNS × 2` 时才重新生成，
    因此正常会话（< 12 轮）根本不会触发摘要，零额外成本。
    """

    __tablename__ = "conversation_summaries"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)  # 多租户隔离

    summary = Column(Text, nullable=False, default="")
    covered_message_count = Column(Integer, nullable=False, default=0)
    # 生成摘要时用的模型名（便于排查"摘要质量差是不是模型换了"）
    model = Column(String(120), nullable=True)
    # 摘要生成失败时的原因（成功为空；保留失败现场便于定位）
    error = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class UserMemory(Base):
    """用户长期偏好（L2）

    `data` 是一个 JSON，形如：

    ```json
    {
      "flows":   [{"term": "校园招聘", "count": 7, "last_at": "2026-10-07T13:00:00"}],
      "systems": [{"term": "格慧招",   "count": 3, "last_at": "..."}],
      "topics":  [{"term": "出差",     "count": 5, "last_at": "..."}]
    }
    ```

    刻意**不直接塞进 system prompt 全文**（省 token 也避免污染），
    而是只把高频词作为**检索提示**参与查询扩展——用法见
    `app/agent/memory.py: preference_terms`。
    """

    __tablename__ = "user_memory"

    user_id = Column(String, primary_key=True)  # 一个用户一条
    data = Column(JSON, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class UnansweredQuestion(Base):
    """知识库答不上来的问题（L3 组织记忆）

    在 Agent 定稿节点与固定管线的兜底分支里自动写入——**零额外成本，顺手采集**。
    刻意**不做文本去重**：让它保持"事件流"形态，改造 5 的缺口聚类就能
    直接按行数得到频率（同一问题被问 3 次就是 3 行）。
    """

    __tablename__ = "unanswered_questions"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)

    question = Column(Text, nullable=False, default="")
    # 是否"完全没检索到"（true）还是"检索到了但不敢用"（false，如校验降级）
    retrieved_nothing = Column(Boolean, nullable=False, default=True)
    # 来源：agent（Agent 定稿）/ fixed（固定管线兜底）
    source = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
