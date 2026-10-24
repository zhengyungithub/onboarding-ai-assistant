"""Agent 运行轨迹数据模型

存在的意义：把"Agent 到底做了什么决策"变成可查询、可统计、可对比的数据。
没有这张表，Agent 改造就只能靠体感判断好坏；有了它才能回答：
- 平均检索几轮？有多少次规划失败降级？
- shadow 模式下 Agent 与固定管线的引用差异是什么？
- 哪类问题最容易被误分诊到闲聊？
"""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, JSON, String, Text

from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)  # 多租户隔离

    question = Column(Text, nullable=False, default="")
    intent = Column(String(20), nullable=True)  # smalltalk | kb_qa | flow_nav | action
    mode = Column(String(10), nullable=True)    # off | shadow | on | sync
    status = Column(String(20), nullable=True)  # ok | fallback | awaiting_approval
    # 检查点线程 id：HITL 审批后要靠它从中断点恢复同一次运行
    thread_id = Column(String(120), nullable=True, index=True)

    answer = Column(Text, nullable=True)
    sources = Column(JSON, nullable=True)  # 引用来源（与前端 references 同结构）
    trace = Column(JSON, nullable=True)    # 每次工具调用：工具名/参数/成败/摘要/耗时
    notes = Column(JSON, nullable=True)    # 人类可读的决策轨迹

    tool_calls = Column(Integer, default=0)
    llm_calls = Column(Integer, default=0)
    duration_ms = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    # 引用校验覆盖率（改造 3）：答案硬信息被引用原文支撑的比例，
    # 这是"引用不相关"痛点的可度量指标（评测页的"引用正确率"就取它的均值）
    citation_coverage = Column(Float, nullable=True)
    # 被剔除的引用数：回答实际没用到的来源（引用支撑率 = 保留 / (保留 + 剔除)）
    citation_dropped = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.now, index=True)
