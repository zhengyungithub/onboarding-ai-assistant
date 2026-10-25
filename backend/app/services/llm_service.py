"""LLM 服务：按用户配置动态创建 ChatOpenAI 实例

两级角色模型的 Key 兜底链（2026-10-25）：
1. 用户自己在「API Key 配置」页配置的 Key（仅超管可配置）；
2. 超管账号配置的 Key —— 作为系统级兜底，超管配一次全员可用；
3. .env 的 DEFAULT_*（历史行为，保留兼容）。

注意：走第 2 级时 Base URL / 模型名整体跟随超管配置，避免「A 家的 Key 配 B 家的 URL」。
"""
from typing import Optional
from langchain_openai import ChatOpenAI
from app.models import User
from app.core.config import settings
from app.core.roles import ROLE_SUPER_ADMIN
from app.core.security import decrypt_api_key


def _super_admin_model_config(db=None) -> Optional[dict]:
    """取超管账号的模型配置（普通用户未配置时的系统级兜底）"""
    from app.db.session import SessionLocal

    s = db or SessionLocal()
    try:
        sa = s.query(User).filter(User.role == ROLE_SUPER_ADMIN).first()
        if not sa:
            return None
        return {
            "llm_api_key": decrypt_api_key(sa.llm_api_key) if sa.llm_api_key else None,
            "llm_base_url": sa.llm_base_url,
            "llm_model": sa.llm_model,
            "embedding_api_key": decrypt_api_key(sa.embedding_api_key) if sa.embedding_api_key else None,
        }
    finally:
        if db is None:
            s.close()


def get_llm_for_user(user: User, db=None) -> ChatOpenAI:
    """根据用户配置创建 LLM 实例；未配置时依次回落：超管配置 → .env 默认值"""
    api_key = None
    base_url = None
    model = None

    if user.llm_api_key:
        # 用户自己配过：整套用自己的（Base URL / 模型与 Key 同源）
        api_key = decrypt_api_key(user.llm_api_key)
        base_url = user.llm_base_url
        model = user.llm_model
    else:
        # 系统级兜底：用超管的配置（超管配一次，全员可用）
        sa = _super_admin_model_config(db)
        if sa and sa["llm_api_key"]:
            api_key = sa["llm_api_key"]
            base_url = sa["llm_base_url"]
            model = sa["llm_model"]

    if not api_key:
        api_key = settings.DEFAULT_LLM_API_KEY
    base_url = base_url or settings.DEFAULT_LLM_BASE_URL
    model = model or settings.DEFAULT_LLM_MODEL

    if not api_key:
        raise ValueError("系统还没有配置模型：请联系超管在「API Key 配置」中完成配置")

    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=0.3,
        streaming=True,
    )


def get_embedding_api_key(user: User, db=None) -> Optional[str]:
    """获取用户的 Embedding API Key（兜底链：用户 → 超管 → .env 默认值）"""
    if user.embedding_api_key:
        return decrypt_api_key(user.embedding_api_key)

    sa = _super_admin_model_config(db)
    if sa and sa["embedding_api_key"]:
        return sa["embedding_api_key"]

    return settings.DEFAULT_EMBEDDING_API_KEY
