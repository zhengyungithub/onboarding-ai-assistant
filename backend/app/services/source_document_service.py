"""源文档服务：上传文档存本地 + 入库 + CRUD + AI 抽取"""
import os
import uuid
import shutil
from typing import Optional, List, Tuple
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import SourceDocument, User, DocConfirmation
from app.core.roles import is_staff, read_scope, write_scope, apply_visibility, filter_valid_user_ids
from app.services.document_service import parse_uploaded_file

# 文档本地存储目录（项目下 backend/uploads/）
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 图片存储根目录
IMAGES_DIR = os.path.join(UPLOAD_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)


def _superseded_ids_subquery():
    """所有「已被取代」的文档 id 子查询（作为 supersedes_id 被引用过即被取代）"""
    return select(SourceDocument.supersedes_id).where(SourceDocument.supersedes_id.isnot(None))


def apply_latest_flags(db: Session, docs: List[SourceDocument]) -> List[SourceDocument]:
    """给文档对象打 is_latest 标记（非列属性）：被别的记录 supersedes 过就不是最新版

    列表、详情、绑定任务等所有对外返回路径都要走这里，
    否则「旧版详情」会错误地显示成最新版。
    """
    if not docs:
        return docs
    superseded = {
        row[0]
        for row in db.query(SourceDocument.supersedes_id)
        .filter(SourceDocument.supersedes_id.isnot(None))
        .all()
    }
    for d in docs:
        d.is_latest = d.id not in superseded
    return docs


def _find_latest_same_name(
    db: Session, user_id: str, filename: str, task_id: Optional[str] = None
) -> Optional[SourceDocument]:
    """找「同名 + 同用户 + 当前最新版」的文档，作为升版基准

    归属规则（避免不同任务的同名表格被串成一条版本链）：
      - 上传时指定了任务：优先同任务；找不到则退回到「未归属任何任务」的同名文档
      - 上传时未指定任务：匹配任意归属的同名文档，并继承其任务归属
    """
    base = db.query(SourceDocument).filter(
        SourceDocument.user_id == user_id,
        SourceDocument.filename == filename,
        SourceDocument.id.notin_(_superseded_ids_subquery()),
    )
    if task_id:
        hit = (
            base.filter(SourceDocument.task_id == task_id)
            .order_by(SourceDocument.version.desc())
            .first()
        )
        if hit is not None:
            return hit
        return (
            base.filter(SourceDocument.task_id.is_(None))
            .order_by(SourceDocument.version.desc())
            .first()
        )
    return base.order_by(SourceDocument.version.desc()).first()


def save_upload(
    db: Session,
    user: User,
    file_bytes: bytes,
    filename: str,
    visibility: Optional[str] = None,
    shared_user_ids: Optional[list] = None,
    task_id: Optional[str] = None,
) -> SourceDocument:
    """保存上传文件到本地并入库，同时解析内容（含图片提取）

    同名同用户重复上传 → 自动升版：新记录 version+1 且 supersedes_id 指向上一版，
    旧记录本身不改动（仍可下载），列表默认隐藏被取代的版本。
    """
    ext = os.path.splitext(filename)[1] or ".txt"
    # 用 uuid 重命名，避免文件名冲突
    safe_name = f"{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)

    with open(file_path, "wb") as f:
        f.write(file_bytes)

    # 生成文档 ID，用于图片目录和 URL 前缀
    doc_id = str(uuid.uuid4())
    image_dir = os.path.join(IMAGES_DIR, doc_id)
    image_uri_prefix = f"/api/source-documents/{doc_id}/images/"

    # 解析文档内容（含图片提取）；解析失败也保存文件，content 留空
    content = ""
    try:
        content = parse_uploaded_file(
            file_bytes,
            filename,
            image_dir=image_dir,
            image_uri_prefix=image_uri_prefix,
        )
    except Exception as e:
        print(f"[警告] 文档解析失败，文件仍保存: {e}")

    # 版本判定：同名同用户 + 当前最新版 → 升版
    latest = _find_latest_same_name(db, user.id, filename, task_id)
    version = 1 if latest is None else (latest.version or 1) + 1
    supersedes_id = latest.id if latest is not None else None
    # 未显式指定任务时继承上一版的归属，避免升版把文档从任务里"掉出来"
    effective_task_id = task_id if task_id else (latest.task_id if latest is not None else None)

    doc = SourceDocument(
        id=doc_id,
        user_id=user.id,
        filename=filename,
        file_path=file_path,
        file_size=len(file_bytes),
        file_type=ext.lstrip(".").lower(),
        content=content,
        status="stored",
        extracted_count=0,
        version=version,
        supersedes_id=supersedes_id,
        task_id=effective_task_id,
    )
    # 可见范围：普通用户强制仅自己；管理员/超管默认全员共享，可选定向共享
    apply_visibility(doc, user, visibility, filter_valid_user_ids(db, shared_user_ids))
    db.add(doc)
    db.commit()
    db.refresh(doc)
    # 供接口层判断是否需要提示「已升版」（非列属性，不影响持久化）
    doc.is_new_version = supersedes_id is not None
    doc.is_latest = True
    return doc


def list_documents(
    db: Session,
    user: User,
    keyword: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    include_superseded: bool = False,
    task_id: Optional[str] = None,
) -> Tuple[int, List[SourceDocument]]:
    """文档台账：管理员全局可见；普通用户 = 自己的 + 管理员共享的

    include_superseded=False（默认）时只返回各版本链的最新版；
    task_id 用于任务详情聚合「这个活相关的材料」。
    """
    query = db.query(SourceDocument)
    scope = read_scope(SourceDocument, user)
    if scope is not None:
        query = query.filter(scope)
    if keyword:
        query = query.filter(SourceDocument.filename.contains(keyword))
    if task_id:
        query = query.filter(SourceDocument.task_id == task_id)
    if not include_superseded:
        query = query.filter(SourceDocument.id.notin_(_superseded_ids_subquery()))
    total = query.count()
    items = query.order_by(SourceDocument.created_at.desc()).offset(skip).limit(limit).all()
    return total, apply_latest_flags(db, items)


def list_versions(db: Session, doc_id: str, user: User) -> Optional[List[SourceDocument]]:
    """返回该文档所在版本链的全部版本（由旧到新）；越权或不存在返回 None"""
    doc = get_document(db, doc_id, user)
    if not doc:
        return None

    chain = [doc]
    seen = {doc.id}

    # 向上追到 v1
    cur = doc
    while cur.supersedes_id and cur.supersedes_id not in seen:
        prev = db.query(SourceDocument).filter(SourceDocument.id == cur.supersedes_id).first()
        # 只认同一用户的同一链，避免历史数据脏指向串到别人文档
        if prev is None or prev.user_id != doc.user_id:
            break
        chain.insert(0, prev)
        seen.add(prev.id)
        cur = prev

    # 向下追到最新版（从中间某个版本打开时也能看到更新的版本）
    cur = doc
    while True:
        nxt = (
            db.query(SourceDocument)
            .filter(SourceDocument.supersedes_id == cur.id, SourceDocument.user_id == doc.user_id)
            .first()
        )
        if nxt is None or nxt.id in seen:
            break
        chain.append(nxt)
        seen.add(nxt.id)
        cur = nxt

    for d in chain:
        d.is_latest = d.id == chain[-1].id
    return chain


def bind_task(db: Session, doc_id: str, user: User, task_id: Optional[str]) -> Optional[SourceDocument]:
    """把文档关联到某个任务（task_id 传 None/空串表示解除关联）"""
    doc = get_document(db, doc_id, user, for_write=True)
    if not doc:
        return None
    doc.task_id = task_id or None
    db.commit()
    db.refresh(doc)
    apply_latest_flags(db, [doc])
    return doc


def get_document(db: Session, doc_id: str, user: User, for_write: bool = False) -> Optional[SourceDocument]:
    """for_write=True 时要求"本人或管理员"（删除/标记等）"""
    query = db.query(SourceDocument).filter(SourceDocument.id == doc_id)
    scope = write_scope(SourceDocument, user) if for_write else read_scope(SourceDocument, user)
    if scope is not None:
        query = query.filter(scope)
    return query.first()


def delete_document(db: Session, doc_id: str, user: User) -> bool:
    """删除文档记录及本地文件（含图片目录）"""
    doc = get_document(db, doc_id, user, for_write=True)
    if not doc:
        return False
    # 删除本地文件
    # 注意：except BaseException —— 某些环境会向 os.remove 注入安全删除 shim，
    # 失败时抛出 SystemExit(1)（不继承 Exception），必须一并兜住，否则删除接口 500
    try:
        if doc.file_path and os.path.exists(doc.file_path):
            os.remove(doc.file_path)
    except BaseException as e:
        print(f"[警告] 删除本地文件失败: {e}")
    # 删除图片目录
    image_dir = os.path.join(IMAGES_DIR, doc_id)
    try:
        if os.path.exists(image_dir):
            shutil.rmtree(image_dir)
    except BaseException as e:
        print(f"[警告] 删除图片目录失败: {e}")
    # 确认清单随文档一起删除：文档没了，挂在它上面的"请大家看一下"也没有意义，
    # 留着只会在「待我确认」里出现打不开的死记录。
    db.query(DocConfirmation).filter(
        DocConfirmation.document_id == doc_id
    ).delete(synchronize_session=False)
    db.delete(doc)
    db.commit()
    return True


def mark_extracted(db: Session, doc_id: str, user: User, count: int) -> Optional[SourceDocument]:
    """标记文档为已抽取"""
    doc = get_document(db, doc_id, user, for_write=True)
    if not doc:
        return None
    doc.status = "extracted"
    doc.extracted_count = count
    db.commit()
    db.refresh(doc)
    return doc


def update_visibility(
    db: Session, doc_id: str, user: User, visibility: str, shared_user_ids: Optional[list] = None
) -> Optional[SourceDocument]:
    """修改文档可见范围（仅本人或管理员）"""
    doc = get_document(db, doc_id, user, for_write=True)
    if not doc:
        return None
    apply_visibility(doc, user, visibility, filter_valid_user_ids(db, shared_user_ids))
    db.commit()
    db.refresh(doc)
    return doc
