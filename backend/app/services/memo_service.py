"""备忘录（待办事项）服务层"""
import uuid
from typing import Optional, Tuple, List, Set
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.models.memo import Memo, MemoFolder
from app.models.user import User
from app.schemas.memo import MemoCreate, MemoUpdate, MemoFolderCreate, MemoFolderUpdate

# 文件夹最大层级数（1 = 只有顶层文件夹）
MAX_FOLDER_DEPTH = 3


def get_memos(
    db: Session,
    user_id: str,
    is_completed: Optional[bool] = None,
    priority: Optional[str] = None,
    keyword: Optional[str] = None,
    folder_id: Optional[str] = None,
    ungrouped: bool = False,
    skip: int = 0,
    limit: int = 100,
) -> Tuple[int, List[Memo]]:
    """获取当前用户的备忘录列表

    folder_id 指定具体文件夹；ungrouped=True 只看未分组；两者都不传 = 全部。
    """
    query = db.query(Memo).filter(Memo.user_id == user_id)
    if is_completed is not None:
        query = query.filter(Memo.is_completed == is_completed)
    if priority:
        query = query.filter(Memo.priority == priority)
    if keyword:
        query = query.filter(Memo.title.ilike(f"%{keyword}%"))
    if ungrouped:
        query = query.filter(Memo.folder_id.is_(None))
    elif folder_id:
        # 选中文件夹时聚合展示其全部子孙文件夹里的备忘录
        folder_ids = [folder_id] + get_descendant_ids(db, user_id, folder_id)
        query = query.filter(Memo.folder_id.in_(folder_ids))
    total = query.count()
    # 未完成在前；同组内按 updated_at 降序——编辑过的待办浮到最上方，不「沉底」；
    # created_at 作同秒并列时的稳定次序
    items = (
        query.order_by(
            Memo.is_completed.asc(),
            Memo.updated_at.desc(),
            Memo.created_at.desc(),
        )
        .offset(skip)
        .limit(limit)
        .all()
    )
    return total, items


def create_memo(db: Session, current_user: User, data: MemoCreate) -> Memo:
    """创建备忘录"""
    folder_id = data.folder_id
    if folder_id:
        # 文件夹必须属于当前用户，防止越权挂载
        folder = get_folder(db, folder_id, current_user.id)
        if not folder:
            folder_id = None
    memo = Memo(
        user_id=current_user.id,
        title=data.title,
        content=data.content,
        priority=data.priority or "medium",
        due_date=data.due_date,
        folder_id=folder_id,
    )
    db.add(memo)
    db.commit()
    db.refresh(memo)
    return memo


def get_memo(db: Session, memo_id: str, user_id: str) -> Optional[Memo]:
    """获取单个备忘录"""
    return db.query(Memo).filter(Memo.id == memo_id, Memo.user_id == user_id).first()


def update_memo(db: Session, memo_id: str, user_id: str, data: MemoUpdate) -> Optional[Memo]:
    """更新备忘录"""
    memo = get_memo(db, memo_id, user_id)
    if not memo:
        return None
    update_data = data.model_dump(exclude_unset=True)
    if "folder_id" in update_data and update_data["folder_id"]:
        if not get_folder(db, update_data["folder_id"], user_id):
            return None
    for key, value in update_data.items():
        setattr(memo, key, value)
    db.commit()
    db.refresh(memo)
    return memo


def toggle_memo(db: Session, memo_id: str, user_id: str) -> Optional[Memo]:
    """切换备忘录完成状态"""
    memo = get_memo(db, memo_id, user_id)
    if not memo:
        return None
    memo.is_completed = not memo.is_completed
    db.commit()
    db.refresh(memo)
    return memo


def delete_memo(db: Session, memo_id: str, user_id: str) -> bool:
    """删除备忘录"""
    memo = get_memo(db, memo_id, user_id)
    if not memo:
        return False
    db.delete(memo)
    db.commit()
    return True


def update_memo_folder(db: Session, memo_id: str, user_id: str, folder_id: Optional[str]) -> Optional[Memo]:
    """移动备忘录到指定文件夹（folder_id=None 表示移入未分组）"""
    memo = get_memo(db, memo_id, user_id)
    if not memo:
        return None
    if folder_id:
        if not get_folder(db, folder_id, user_id):
            return None
    memo.folder_id = folder_id
    db.commit()
    db.refresh(memo)
    return memo


# ---------- 文件夹（支持子文件夹，树形层级） ----------

def list_folders(db: Session, user_id: str) -> List[dict]:
    """获取当前用户的文件夹列表（按创建时间正序），附每个文件夹的直接备忘录数量"""
    folders = (
        db.query(MemoFolder)
        .filter(MemoFolder.user_id == user_id)
        .order_by(MemoFolder.created_at.asc())
        .all()
    )
    counts = dict(
        db.query(Memo.folder_id, func.count(Memo.id))
        .filter(Memo.user_id == user_id, Memo.folder_id.isnot(None))
        .group_by(Memo.folder_id)
        .all()
    )
    return [
        {
            "id": f.id,
            "name": f.name,
            "parent_id": f.parent_id,
            "memo_count": int(counts.get(f.id, 0)),
            "created_at": f.created_at,
        }
        for f in folders
    ]


def get_folder(db: Session, folder_id: str, user_id: str) -> Optional[MemoFolder]:
    """获取单个文件夹（归属校验）"""
    return (
        db.query(MemoFolder)
        .filter(MemoFolder.id == folder_id, MemoFolder.user_id == user_id)
        .first()
    )


def get_descendant_ids(db: Session, user_id: str, folder_id: str) -> List[str]:
    """获取文件夹的全部子孙文件夹 id（BFS，不含自身）"""
    folders = db.query(MemoFolder).filter(MemoFolder.user_id == user_id).all()
    children_map: dict = {}
    for f in folders:
        children_map.setdefault(f.parent_id, []).append(f.id)
    result: List[str] = []
    queue = [folder_id]
    while queue:
        current = queue.pop(0)
        for child_id in children_map.get(current, []):
            result.append(child_id)
            queue.append(child_id)
    return result


def _get_ancestor_chain(db: Session, folder_id: str, user_id: str) -> List[MemoFolder]:
    """从 folder_id 沿 parent_id 向上追溯到根，返回 [自身, 父, 祖先...]（异常环路自动截断）"""
    chain: List[MemoFolder] = []
    seen: Set[str] = set()
    current_id = folder_id
    while current_id and current_id not in seen:
        seen.add(current_id)
        folder = get_folder(db, current_id, user_id)
        if not folder:
            break
        chain.append(folder)
        current_id = folder.parent_id
    return chain


def _sibling_name_exists(db: Session, user_id: str, parent_id: Optional[str], name: str, exclude_id: Optional[str] = None) -> bool:
    """同一父文件夹下是否已存在同名文件夹（同名拦截限定在兄弟层级）"""
    query = db.query(MemoFolder).filter(
        MemoFolder.user_id == user_id,
        MemoFolder.parent_id == parent_id,  # parent_id=None 时匹配所有顶层
        MemoFolder.name == name,
    )
    if exclude_id:
        query = query.filter(MemoFolder.id != exclude_id)
    return query.first() is not None


def create_folder(db: Session, user_id: str, data: MemoFolderCreate) -> Optional[MemoFolder]:
    """创建文件夹（可指定父文件夹成为子文件夹）

    校验：父文件夹归属；层级不超过 MAX_FOLDER_DEPTH；同层不重名。
    """
    name = data.name.strip()
    parent_id = data.parent_id or None
    if parent_id:
        parent = get_folder(db, parent_id, user_id)
        if not parent:
            return None
        # 父链深度 = 父文件夹所在层级；子层级 = 父层级 + 1，不得超过上限
        parent_depth = len(_get_ancestor_chain(db, parent_id, user_id))
        if parent_depth + 1 > MAX_FOLDER_DEPTH:
            return None
    if _sibling_name_exists(db, user_id, parent_id, name):
        return None
    folder = MemoFolder(user_id=user_id, name=name, parent_id=parent_id)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder


def update_folder(db: Session, folder_id: str, user_id: str, data: MemoFolderUpdate) -> Optional[MemoFolder]:
    """更新文件夹：重命名 和/或 移动到新的父文件夹（都不传返回 None）

    移动校验：目标父文件夹归属、层级不超限、不能移动到自身或自己的子孙（防环）。
    """
    folder = get_folder(db, folder_id, user_id)
    if not folder:
        return None
    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        return None

    new_name = update_data.get("name")
    if new_name is not None:
        new_name = str(new_name).strip()
        if _sibling_name_exists(db, user_id, folder.parent_id, new_name, exclude_id=folder_id):
            return None

    # parent_id 未传 = 不移动；显式传值（含 null=移到顶层）= 移动
    move = "parent_id" in update_data
    new_parent_id = update_data.get("parent_id") or None if move else folder.parent_id

    if move and new_parent_id != folder.parent_id:
        if new_parent_id:
            new_parent = get_folder(db, new_parent_id, user_id)
            if not new_parent:
                return None
            # 不能移动到自身或自己的子孙（防环）
            descendant_ids = get_descendant_ids(db, user_id, folder_id)
            if new_parent_id == folder_id or new_parent_id in descendant_ids:
                return None
            # 层级校验：新父深度 + 自身子树深度 ≤ 上限
            new_parent_depth = len(_get_ancestor_chain(db, new_parent_id, user_id))
            own_depth = len(_get_ancestor_chain(db, folder_id, user_id))
            subtree_depth = _subtree_depth(db, user_id, folder_id)
            if new_parent_depth + subtree_depth > MAX_FOLDER_DEPTH:
                return None

    if new_name is not None:
        folder.name = new_name
    if move:
        folder.parent_id = new_parent_id
    db.commit()
    db.refresh(folder)
    return folder


def _subtree_depth(db: Session, user_id: str, folder_id: str) -> int:
    """以 folder_id 为根的子树层数（自身算 1 层）"""
    descendant_ids = get_descendant_ids(db, user_id, folder_id)
    if not descendant_ids:
        return 1
    folders = {f.id: f for f in db.query(MemoFolder).filter(MemoFolder.user_id == user_id).all()}
    max_rel = 1
    for fid in descendant_ids:
        rel = 1
        cur = fid
        # 沿 parent 向上数到 folder_id 的相对层数（异常数据自动截断）
        while cur != folder_id and cur in folders and folders[cur].parent_id:
            cur = folders[cur].parent_id
            rel += 1
        max_rel = max(max_rel, rel)
    return max_rel


def delete_folder(db: Session, folder_id: str, user_id: str) -> bool:
    """删除文件夹：其备忘录移入「未分组」，子文件夹上移一级（挂到被删文件夹的父级）"""
    folder = get_folder(db, folder_id, user_id)
    if not folder:
        return False
    db.query(Memo).filter(
        Memo.user_id == user_id, Memo.folder_id == folder_id
    ).update({Memo.folder_id: None}, synchronize_session=False)
    # 子文件夹上移一级，保持相对层级
    db.query(MemoFolder).filter(
        MemoFolder.user_id == user_id, MemoFolder.parent_id == folder_id
    ).update({MemoFolder.parent_id: folder.parent_id}, synchronize_session=False)
    db.delete(folder)
    db.commit()
    return True
