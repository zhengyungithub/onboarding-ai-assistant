"""LLM 服务：按用户配置动态创建 ChatOpenAI 实例"""
from typing import Optional
from langchain_openai import ChatOpenAI
from app.models import User
from app.core.config import settings
from app.core.security import decrypt_api_key


def get_llm_for_user(user: User) -> ChatOpenAI:
    """根据用户配置（或默认配置）创建 LLM 实例"""
    api_key = decrypt_api_key(user.llm_api_key) if user.llm_api_key else settings.DEFAULT_LLM_API_KEY
    base_url = user.llm_base_url or settings.DEFAULT_LLM_BASE_URL
    model = user.llm_model or settings.DEFAULT_LLM_MODEL

    if not api_key:
        raise ValueError("请先在「API Key 配置」中设置 LLM API Key")

    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=0.3,
        streaming=True,
    )


def get_embedding_api_key(user: User) -> Optional[str]:
    """获取用户的 Embedding API Key"""
    if user.embedding_api_key:
        return decrypt_api_key(user.embedding_api_key)
    return settings.DEFAULT_EMBEDDING_API_KEY
