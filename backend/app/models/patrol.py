"""主动巡检报告数据模型（改造 5）

巡检（Patrol）是系统的"主动侧"：不再等用户提问，而是定时/手动扫一遍知识库，
把**缺口、过期、质量异常、一致性冲突**整理成报告 + 草稿。

| 字段 | 存什么 |
|------|--------|
| `stale_items`        | 过期条目（published 且长期未更新） |
| `gap_clusters`       | 缺口聚类（每簇含成员问题 + 生成的草稿条目 id） |
| `quality_issues`     | 质量异常（被引用多但有用率低的条目） |
| `consistency_issues` | 一致性冲突（流程节点描述与关联条目矛盾，需人工裁决） |

**关键设计：巡检只产出 `status=draft` 草稿 + 报告，绝不自动发布。**
复用现有「草稿 → 人工复核 → 发布」链路，守住"不引入错误知识"的底线。
"""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class PatrolReport(Base):
    """一次巡检的完整产出（分区块存储，便于前端分区展示与按块取用）"""

    __tablename__ = "patrol_reports"

    id = Column(String, primary_key=True, default=generate_uuid)
    # 触发这次巡检的人（不是"被巡检数据的归属者"）
    user_id = Column(String, nullable=False, index=True)
    # 巡检范围：me（只看自己的）/ all（全局，仅超管）
    scope = Column(String(10), nullable=False, default="me")
    run_at = Column(DateTime, default=datetime.now, index=True)

    # ok（全部检查器跑完）/ partial（部分检查器失败）/ error（整体失败）
    status = Column(String(20), nullable=False, default="ok")

    stale_items = Column(JSON, nullable=True)
    gap_clusters = Column(JSON, nullable=True)
    quality_issues = Column(JSON, nullable=True)
    consistency_issues = Column(JSON, nullable=True)

    # LLM（或规则）生成的一句话总览
    summary = Column(Text, nullable=True)
    # 计数与耗时等元信息：{stale: n, gaps: n, drafts: n, quality: n, duration_ms: n}
    counts = Column(JSON, nullable=True)

    duration_ms = Column(Integer, default=0)
    # 分段失败原因（JSON：{"stale": "...", "gaps": "..."}），成功为空
    error = Column(JSON, nullable=True)
