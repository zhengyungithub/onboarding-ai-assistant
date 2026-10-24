"""用户数据模型"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text
from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=generate_uuid)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="user")  # user | admin | super_admin

    # 用户自己的 LLM/Embedding 配置（加密存储）
    llm_api_key = Column(Text, nullable=True)
    llm_base_url = Column(String(500), nullable=True)
    llm_model = Column(String(100), nullable=True)
    embedding_api_key = Column(Text, nullable=True)
    embedding_model = Column(String(100), nullable=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
