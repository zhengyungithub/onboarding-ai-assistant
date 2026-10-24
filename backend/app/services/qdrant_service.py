"""Qdrant 向量库服务：混合检索（dense + sparse BM25 + RRF 融合）"""
from typing import List, Dict, Any, Optional
from qdrant_client import QdrantClient, models
from app.core.config import settings
from app.core.roles import is_staff, VIS_ALL, VIS_CUSTOM

COLLECTION_NAME = "knowledge_items"

# 全局 Qdrant 客户端
_client: Optional[QdrantClient] = None


def get_qdrant_client() -> QdrantClient:
    global _client
    if _client is None:
        # timeout 放宽到 30s：Qdrant 冷启动后的首个请求可能较慢，默认 5s 会误报超时
        _client = QdrantClient(url=settings.QDRANT_URL, timeout=30)
    return _client


def _ensure_payload_index(field_name: str, schema):
    """幂等创建 payload 索引（已存在时忽略报错）"""
    try:
        get_qdrant_client().create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name=field_name,
            field_schema=schema,
        )
    except Exception:
        pass


def ensure_collection(dense_size: Optional[int] = None) -> bool:
    """确保集合存在，dense + sparse 双向量，并保证过滤字段索引存在

    dense_size 跟随实际使用的 embedding 模型维度（如 text-embedding-v3=1024，
    bge-small-zh-v1.5=512）。规则：
    - 集合不存在 → 按 dense_size（缺省 512）创建，返回 True；
    - 集合存在且 dense_size 已知但与集合维度不一致（如更换过 embedding 模型）→
      重建空集合，返回 True（调用方需重新回灌向量）；
    - 集合存在且 dense_size 未知（embedding 探测失败）→ 不动现有集合，返回 False，
      绝不盲目用默认维度重建，否则会把已有向量数据清空且后续写入全部维度不符。
    """
    client = get_qdrant_client()
    collections = client.get_collections().collections
    existing = next((c for c in collections if c.name == COLLECTION_NAME), None)

    created = existing is None
    if existing is not None and dense_size is not None:
        current_dim = _get_collection_dim()
        if current_dim and current_dim != dense_size:
            print(
                f"[初始化] Qdrant 集合维度({current_dim})与 embedding 模型维度({dense_size})不一致，重建集合"
            )
            client.delete_collection(COLLECTION_NAME)
            created = True

    if created:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config={
                "dense": models.VectorParams(
                    size=dense_size or 512,
                    distance=models.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                "sparse": models.SparseVectorParams(
                    index=models.SparseIndexParams(on_disk=True)
                )
            },
        )

    # 过滤字段索引：user_id（归属）+ visibility / shared_user_ids（三态可见范围）
    _ensure_payload_index("user_id", models.PayloadSchemaType.KEYWORD)
    _ensure_payload_index("is_shared", models.PayloadSchemaType.BOOL)
    _ensure_payload_index("visibility", models.PayloadSchemaType.KEYWORD)
    _ensure_payload_index("shared_user_ids", models.PayloadSchemaType.KEYWORD)
    return created


def _get_collection_dim() -> Optional[int]:
    """读取当前集合的 dense 向量维度（集合不存在或读取失败返回 None）"""
    try:
        info = get_qdrant_client().get_collection(COLLECTION_NAME)
        vectors = info.config.params.vectors
        if isinstance(vectors, dict) and "dense" in vectors:
            return vectors["dense"].size
    except Exception:
        return None
    return None


def upsert_item(
    item_id: str,
    user_id: str,
    dense_vector: List[float],
    sparse_vector: Dict[str, Any],
    payload: Dict[str, Any],
):
    """写入/更新单条知识条目到 Qdrant"""
    client = get_qdrant_client()
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            models.PointStruct(
                id=item_id,
                vector={
                    "dense": dense_vector,
                    "sparse": models.SparseVector(
                        indices=sparse_vector["indices"],
                        values=sparse_vector["values"],
                    ),
                },
                payload={**payload, "user_id": user_id},
            )
        ],
    )


def delete_item(item_id: str):
    """从 Qdrant 删除条目"""
    client = get_qdrant_client()
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=models.PointIdsList(points=[item_id]),
    )


def set_payload_for_points(item_ids: List[str], payload: Dict[str, Any]) -> None:
    """批量更新已有向量的 payload（用于共享标记等元数据的批量同步）"""
    if not item_ids:
        return
    get_qdrant_client().set_payload(
        collection_name=COLLECTION_NAME,
        payload=payload,
        points=item_ids,
    )


def _scope_filter(user):
    """检索范围过滤：管理员/超管不过滤；普通用户 = 自己的 + 全员共享 + 定向共享给自己的"""
    if is_staff(user):
        return None
    return models.Filter(
        should=[
            models.FieldCondition(key="user_id", match=models.MatchValue(value=user.id)),
            models.FieldCondition(key="visibility", match=models.MatchValue(value=VIS_ALL)),
            # 定向共享：visibility=custom 且 shared_user_ids 数组包含自己
            models.Filter(
                must=[
                    models.FieldCondition(
                        key="visibility", match=models.MatchValue(value=VIS_CUSTOM)
                    ),
                    models.FieldCondition(
                        key="shared_user_ids", match=models.MatchValue(value=user.id)
                    ),
                ]
            ),
        ]
    )


def hybrid_search(
    user,
    dense_query_vector: List[float],
    sparse_query_vector: Dict[str, Any],
    limit: int = 5,
    score_threshold: float = 0.3,
) -> List[Dict[str, Any]]:
    """混合检索：dense + sparse，RRF 融合（按角色决定检索范围）"""
    client = get_qdrant_client()

    scope = _scope_filter(user)
    query_filter = models.Filter(must=[scope]) if scope is not None else None

    # dense 检索（qdrant-client >= 1.10 使用 query_points，search_points 已移除）
    dense_results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=dense_query_vector,
        using="dense",
        limit=limit,
        query_filter=query_filter,
        with_payload=True,
    )

    # sparse 检索
    sparse_results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=models.SparseVector(
            indices=sparse_query_vector["indices"],
            values=sparse_query_vector["values"],
        ),
        using="sparse",
        limit=limit,
        query_filter=query_filter,
        with_payload=True,
    )

    # RRF 融合
    k = 60  # RRF 常数
    scores: Dict[str, float] = {}
    payloads: Dict[str, Any] = {}

    for rank, point in enumerate(dense_results.points):
        rid = point.id
        scores[rid] = scores.get(rid, 0) + 1 / (k + rank + 1)
        payloads[rid] = point.payload

    for rank, point in enumerate(sparse_results.points):
        rid = point.id
        scores[rid] = scores.get(rid, 0) + 1 / (k + rank + 1)
        payloads[rid] = point.payload

    # 按融合分数排序
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    results = []
    for rid, fused_score in ranked:
        if fused_score < 1 / (k + limit):  # 简单阈值过滤
            continue
        payload = payloads.get(rid, {})
        results.append({
            "id": rid,
            "score": fused_score,
            "title": payload.get("title", ""),
            "content": payload.get("content", ""),
            "source_type": "knowledge_item",
        })

    return results[:limit]
