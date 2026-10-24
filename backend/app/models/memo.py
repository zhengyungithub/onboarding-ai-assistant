"""备忘录（待办事项）数据模型"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, Boolean
from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class Memo(Base):
    __tablename__ = "memos"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)  # 多租户隔离

    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=True)
    is_completed = Column(Boolean, nullable=False, default=False)
    priority = Column(String(10), nullable=False, default="medium")  # low | medium | high
    due_date = Column(DateTime, nullable=True)
    folder_id = Column(String, nullable=True, index=True)  # 所属文件夹，NULL=未分组

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MemoFolder(Base):
    __tablename__ = "memo_folders"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)  # 多租户隔离

    name = Column(String(50), nullable=False)
    parent_id = Column(String, nullable=True, index=True)  # 父文件夹 id，NULL=顶层文件夹

    created_at = Column(DateTime, default=datetime.now)
