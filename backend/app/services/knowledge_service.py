"""知识条目服务：CRUD + 向量化同步"""
from typing import Optional, List
from sqlalchemy.orm import Session
from app.models import KnowledgeItem, User
from app.schemas.knowledge import KnowledgeItemCreate, KnowledgeItemUpdate
from app.core.roles import is_staff, read_scope, write_scope, apply_visibility, filter_valid_user_ids
from app.services.embedding_service import embed_dense, embed_sparse
from app.services import qdrant_service


def get_items(
    db: Session,
    user: User,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    related_flow: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[int, List[KnowledgeItem]]:
    """分页查询知识条目

    可见范围：管理员/超管 = 全局；普通用户 = 自己的 + 管理员共享的
    """
    query = db.query(KnowledgeItem)
    scope = read_scope(KnowledgeItem, user)
    if scope is not None:
        query = query.filter(scope)
    if status:
        query = query.filter(KnowledgeItem.status == status)
    if keyword:
        query = query.filter(KnowledgeItem.title.contains(keyword))
    if related_flow:
        # related_flow 存储为 JSON 数组字符串，精确匹配带引号的 flow_id
        query = query.filter(KnowledgeItem.related_flow.like(f'%"{related_flow}"%'))
    total = query.count()
    items = query.order_by(KnowledgeItem.created_at.desc()).offset(skip).limit(limit).all()
    return total, items


def get_item(db: Session, item_id: str, user: User, for_write: bool = False) -> Optional[KnowledgeItem]:
    """按 ID 取条目。for_write=True 时要求"本人或管理员"（用于编辑/删除）。"""
    query = db.query(KnowledgeItem).filter(KnowledgeItem.id == item_id)
    scope = write_scope(KnowledgeItem, user) if for_write else read_scope(KnowledgeItem, user)
    if scope is not None:
        query = query.filter(scope)
    return query.first()


def create_item(db: Session, user: User, data: KnowledgeItemCreate) -> KnowledgeItem:
    item = KnowledgeItem(
        user_id=user.id,
        title=data.title,
        steps=data.steps,
        system_or_material=data.system_or_material,
        caution=data.caution,
        related_flow=data.related_flow,
        status=data.status or "draft",
    )
    # 可见范围：普通用户强制仅自己；管理员/超管默认全员共享，可选定向共享
    apply_visibility(
        item, user, data.visibility,
        filter_valid_user_ids(db, data.shared_user_ids),
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    # 如果是 published，立即向量化
    if item.status == "published":
        _sync_to_qdrant(item, user)
    return item


def update_item(
    db: Session, item_id: str, user: User, data: KnowledgeItemUpdate
) -> Optional[KnowledgeItem]:
    item = get_item(db, item_id, user, for_write=True)
    if not item:
        return None
    update_data = data.model_dump(exclude_unset=True)
    # 可见范围单独处理（需要归一化 + 派生 is_shared）
    vis_data = update_data.pop("visibility", None)
    shared_ids = update_data.pop("shared_user_ids", None)
    if vis_data is not None or shared_ids is not None:
        apply_visibility(
            item, user, vis_data if vis_data is not None else item.visibility,
            filter_valid_user_ids(db, shared_ids) if shared_ids is not None else item.shared_user_ids,
        )
    for field, value in update_data.items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)

    # 可见范围或状态变更时同步 Qdrant（Qdrant 不可用时仅告警，不影响本次编辑）
    if vis_data is not None or shared_ids is not None or "status" in update_data:
        try:
            if item.status == "published":
                _sync_to_qdrant(item, user)
            elif item.status == "draft":
                qdrant_service.delete_item(item.id)
        except Exception as e:
            print(f"[警告] 向量同步失败（编辑已保存）: {e}")
    return item


def delete_item(db: Session, item_id: str, user: User) -> bool:
    item = get_item(db, item_id, user, for_write=True)
    if not item:
        return False
    # Qdrant 不可用时跳过向量删除，不影响数据库删除
    try:
        qdrant_service.delete_item(item.id)
    except Exception as e:
        print(f"[警告] Qdrant 删除向量失败，跳过: {e}")
    db.delete(item)
    db.commit()
    return True


def publish_item(db: Session, item_id: str, user: User) -> Optional[KnowledgeItem]:
    """发布条目：状态改为 published 并向量化"""
    item = get_item(db, item_id, user, for_write=True)
    if not item:
        return None
    item.status = "published"
    db.commit()
    db.refresh(item)
    _sync_to_qdrant(item, user)
    return item


def _sync_to_qdrant(item: KnowledgeItem, user: User):
    """将条目向量化并写入 Qdrant（不可用时跳过）"""
    try:
        text = item.to_search_text()
        dense_vec = embed_dense(text, user)
        sparse_vec = embed_sparse(text)
        qdrant_service.upsert_item(
            item_id=item.id,
            user_id=item.user_id,
            dense_vector=dense_vec,
            sparse_vector=sparse_vec,
            payload={
                "title": item.title,
                "content": text,
                "is_shared": bool(item.is_shared),
                "visibility": item.visibility or "private",
                "shared_user_ids": item.shared_user_ids or [],
            },
        )
    except Exception as e:
        print(f"[警告] Qdrant 向量化失败，跳过: {e}")
