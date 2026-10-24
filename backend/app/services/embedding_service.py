"""Embedding 服务：使用 OpenAI 兼容 API 生成 dense 向量，jieba+BM25 生成 sparse 向量"""
from typing import List, Dict, Any
from app.models import User
from app.services.llm_service import get_embedding_api_key
from app.core.security import decrypt_api_key
from app.core.config import settings


def get_embedding_config(user: User):
    """获取用户的 Embedding 配置"""
    api_key = get_embedding_api_key(user)
    base_url = user.llm_base_url or settings.DEFAULT_LLM_BASE_URL
    model = user.embedding_model or settings.DEFAULT_EMBEDDING_MODEL
    return api_key, base_url, model


def embed_dense(text: str, user: User) -> List[float]:
    """用 OpenAI 兼容 API 生成 dense 向量

    check_embedding_ctx_length=False：LangChain 默认会把文本先转成 token 数组再发送，
    DashScope 等兼容 API 不支持 token 数组入参（报 400 "contents is neither str nor
    list of str"），必须关闭该行为，直接发送原始字符串。
    """
    from langchain_openai import OpenAIEmbeddings

    api_key, base_url, model = get_embedding_config(user)
    if not api_key:
        raise ValueError("请先配置 Embedding API Key")

    embeddings = OpenAIEmbeddings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        check_embedding_ctx_length=False,
    )
    return embeddings.embed_query(text)


def embed_sparse(text: str) -> Dict[str, Any]:
    """用 jieba 分词 + BM25 权重生成 sparse 向量"""
    import jieba
    import math
    from collections import Counter

    # 简单分词
    words = list(jieba.cut(text))
    # 过滤停用词和单字
    stopwords = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这"}
    words = [w for w in words if len(w) > 1 and w not in stopwords]

    if not words:
        return {"indices": [], "values": []}

    # BM25 权重（简化版：词频）
    counter = Counter(words)
    total = sum(counter.values())

    indices = []
    values = []
    # 用词的 hash 作为 index
    for word, count in counter.items():
        idx = abs(hash(word)) % (2**31)
        tf = count / total
        idf = 1.0  # 简化，不计算全局 IDF
        score = tf * idf
        indices.append(idx)
        values.append(score)

    return {"indices": indices, "values": values}
