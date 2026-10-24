"""文档确认服务：把「群发一份表格，问大家看一下」变成可追踪的确认清单

业务背景（P4）：
群里的表格太多、同名文件反复改，发起人无法确认「谁看过、谁有意见」。
追问靠刷群，改完一版又得重新问一遍，最后谁也不敢说这版是对的。

这里的三个关键取舍：
1. **邀请即授权**：被邀请的确认人可能根本看不到该文档（默认 private），
   所以确认记录本身就是访问凭据——详情/下载走确认记录，而不是走文档的可见范围。
   权限面收在「这一条记录对应的这一个文档」，不外扩。
2. **同版本内不堆积**：同一文档 × 同一人 × 仍是 pending 时重复发起，只更新附言、不新增记录；
   已确认/已提意见的人再发起才算新一轮（这样「改完再确认」是可数的）。
3. **免登录通道默认关闭**：token 链接能解决「确认人不装系统」的冷启动问题，
   但它把文档暴露到登录体系之外，属于安全边界的变化，必须由部署方显式打开。
"""
import secrets
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.roles import is_staff
from app.models import DocConfirmation, SourceDocument, User
from app.models.confirmation import (
    STATUS_CANCELLED,
    STATUS_CHOICES,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    STATUS_REJECTED,
)
from app.services import source_document_service

# 免登录页展示的正文上限：确认人要能看清内容，但不能把整份长文档塞进一次响应
PREVIEW_MAX_CHARS = 20000


class ConfirmationError(Exception):
    """服务层错误：由 API 层转成 HTTP 状态码（服务层不直接依赖 FastAPI）"""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _now() -> datetime:
    return datetime.now()


def _gen_token() -> str:
    # 24 字节 ≈ 32 字符 url-safe，足够抗猜；token 仅对「一个文档 + 一个人」有效
    return secrets.token_urlsafe(24)


def _user_name_map(db: Session, user_ids: Iterable[str]) -> Dict[str, str]:
    ids = {i for i in user_ids if i}
    if not ids:
        return {}
    rows = db.query(User.id, User.username).filter(User.id.in_(ids)).all()
    return {r[0]: r[1] for r in rows}


def _token_alive(conf: DocConfirmation) -> bool:
    if not conf.token:
        return False
    if conf.token_expires_at and conf.token_expires_at < _now():
        return False
    return True


def _ensure_token(conf: DocConfirmation) -> None:
    """按需补发免登录 token（仅在通道开启时）"""
    if settings.DOC_CONFIRM_PUBLIC_ENABLED and not _token_alive(conf):
        conf.token = _gen_token()
        ttl = max(1, int(settings.DOC_CONFIRM_TOKEN_TTL_DAYS or 7))
        conf.token_expires_at = _now() + timedelta(days=ttl)


def serialize(conf: DocConfirmation, names: Dict[str, str], doc: Optional[SourceDocument] = None) -> dict:
    """把确认记录转成对外的扁平结构（不含 token，token 单独在发起响应里返回）"""
    item = {
        "id": conf.id,
        "document_id": conf.document_id,
        "user_id": conf.user_id,
        "user_name": names.get(conf.user_id, ""),
        "requested_by": conf.requested_by,
        "requested_by_name": names.get(conf.requested_by, ""),
        "status": conf.status,
        "comment": conf.comment or "",
        "message": conf.message or "",
        "doc_version": conf.doc_version or 1,
        "first_viewed_at": conf.first_viewed_at.isoformat() if conf.first_viewed_at else None,
        "created_at": conf.created_at.isoformat() if conf.created_at else "",
        "resolved_at": conf.resolved_at.isoformat() if conf.resolved_at else None,
        "token_expires_at": conf.token_expires_at.isoformat() if conf.token_expires_at else None,
        "token_alive": _token_alive(conf),
    }
    if doc is not None:
        item.update({
            "filename": doc.filename,
            "file_size": doc.file_size or 0,
            "file_type": doc.file_type or "",
            "version": getattr(doc, "version", None) or 1,
            "owner_id": doc.user_id,
            "owner_name": names.get(doc.user_id, ""),
        })
    return item


# --------------------------------------------------------------------------
# 发起 / 撤销
# --------------------------------------------------------------------------

def request_confirmations(
    db: Session,
    doc_id: str,
    requester: User,
    target_user_ids: List[str],
    message: Optional[str] = None,
) -> dict:
    """对一份文档发起确认（仅文档本人或管理员）

    返回 created / reused 两个计数，让发起的用户知道「哪些人是新邀请，哪些人还没处理」。
    """
    doc = source_document_service.get_document(db, doc_id, requester, for_write=True)
    if doc is None:
        raise ConfirmationError("文档不存在或无权操作", 404)

    # 去重、去掉自己（自己确认自己没有意义）
    wanted: List[str] = []
    for uid in target_user_ids or []:
        uid = (uid or "").strip()
        if uid and uid != requester.id and uid not in wanted:
            wanted.append(uid)
    if not wanted:
        raise ConfirmationError("请至少选择一位确认人（不能只选自己）", 400)

    valid = {r[0] for r in db.query(User.id).filter(User.id.in_(wanted)).all()}
    wanted = [u for u in wanted if u in valid]
    if not wanted:
        raise ConfirmationError("所选用户不存在", 400)

    message = (message or "").strip() or None
    version = getattr(doc, "version", None) or 1

    items: List[DocConfirmation] = []
    created = reused = 0
    for uid in wanted:
        conf = (
            db.query(DocConfirmation)
            .filter(
                DocConfirmation.document_id == doc.id,
                DocConfirmation.user_id == uid,
                DocConfirmation.status == STATUS_PENDING,
            )
            .order_by(DocConfirmation.created_at.desc())
            .first()
        )
        if conf is not None:
            # 还没处理的重复邀请：只更新附言，不新增记录（否则统计分母会被灌水）
            if message is not None:
                conf.message = message
            reused += 1
        else:
            conf = DocConfirmation(
                document_id=doc.id,
                user_id=uid,
                requested_by=requester.id,
                status=STATUS_PENDING,
                message=message,
                doc_version=version,
            )
            db.add(conf)
            created += 1
        _ensure_token(conf)
        db.flush()
        items.append(conf)

    db.commit()
    for c in items:
        db.refresh(c)

    names = _user_name_map(db, [c.user_id for c in items] + [requester.id, doc.user_id])
    return {
        "document_id": doc.id,
        "filename": doc.filename,
        "version": version,
        "created": created,
        "reused": reused,
        "public_enabled": bool(settings.DOC_CONFIRM_PUBLIC_ENABLED),
        "items": [
            {**serialize(c, names, doc), "token": c.token if _token_alive(c) else None}
            for c in items
        ],
    }


def revoke(db: Session, conf_id: str, user: User) -> dict:
    """撤销一条待确认（发起人或管理员）；撤销后不计入统计分母"""
    conf = db.query(DocConfirmation).filter(DocConfirmation.id == conf_id).first()
    if conf is None:
        raise ConfirmationError("确认记录不存在", 404)
    if conf.requested_by != user.id and not is_staff(user):
        raise ConfirmationError("只有发起人可以撤销", 403)
    if conf.status != STATUS_PENDING:
        raise ConfirmationError("该记录已处理，无需撤销", 409)
    conf.status = STATUS_CANCELLED
    conf.resolved_at = _now()
    db.commit()
    return {"ok": True, "id": conf.id, "status": conf.status}


# --------------------------------------------------------------------------
# 确认人视角
# --------------------------------------------------------------------------

def list_mine(db: Session, user: User, status: str = STATUS_PENDING, limit: int = 100) -> dict:
    """待我确认（默认只看 pending；status=all 时看全部历史）"""
    if status and status != "all" and status not in STATUS_CHOICES:
        raise ConfirmationError("无效的状态", 400)

    query = db.query(DocConfirmation).filter(DocConfirmation.user_id == user.id)
    if status and status != "all":
        query = query.filter(DocConfirmation.status == status)
    total = query.count()
    rows = (
        query.order_by(DocConfirmation.created_at.desc())
        .limit(max(1, min(int(limit or 100), 200)))
        .all()
    )

    doc_ids = list({r.document_id for r in rows})
    docs: Dict[str, SourceDocument] = {}
    if doc_ids:
        docs = {
            d.id: d
            for d in db.query(SourceDocument).filter(SourceDocument.id.in_(doc_ids)).all()
        }
    names = _user_name_map(
        db,
        [r.requested_by for r in rows] + [d.user_id for d in docs.values()],
    )

    items = []
    for r in rows:
        doc = docs.get(r.document_id)
        if doc is None:
            # 文档已被删除（理论上会级联清理，这里只做防御）
            continue
        items.append(serialize(r, names, doc))

    return {"total": total, "items": items}


def pending_count(db: Session, user_id: str) -> int:
    return (
        db.query(DocConfirmation)
        .filter(
            DocConfirmation.user_id == user_id,
            DocConfirmation.status == STATUS_PENDING,
        )
        .count()
    )


def list_contacts(db: Session, user: User) -> List[dict]:
    """可以邀请来确认的人（所有启用账号，排除自己）

    为什么不复用 /api/auth/users：那个接口只对管理员开放，
    而发起确认是每个普通用户都要做的事。这里只返回 id + 用户名，不暴露角色和邮箱。
    """
    rows = db.query(User.id, User.username).order_by(User.created_at.asc()).all()
    return [{"id": uid, "username": name} for uid, name in rows if uid != user.id]


def issue_link(db: Session, conf_id: str, user: User) -> dict:
    """重新取回/补发某条确认的免登录链接（仅发起人或管理员）

    为什么不放在文档进度列表里返回 token：
    文档如果设成「全员共享」，任何登录用户都能看进度列表，
    把 token 一起返回等于把所有人的免登录入口发给了全公司。
    """
    conf = _get_conf(db, conf_id)
    if conf.requested_by != user.id and not is_staff(user):
        raise ConfirmationError("只有发起人可以获取确认链接", 403)
    if not settings.DOC_CONFIRM_PUBLIC_ENABLED:
        raise ConfirmationError("免登录确认通道未启用（DOC_CONFIRM_PUBLIC_ENABLED=false）", 400)
    if conf.status != STATUS_PENDING:
        raise ConfirmationError("该记录已处理，链接已无需使用", 409)

    _ensure_token(conf)
    db.commit()
    db.refresh(conf)
    return {
        "confirmation_id": conf.id,
        "token": conf.token,
        "expires_at": conf.token_expires_at.isoformat() if conf.token_expires_at else None,
        "url_path": f"/public/confirm/{conf.token}",
    }


def _get_conf(db: Session, conf_id: str) -> DocConfirmation:
    conf = db.query(DocConfirmation).filter(DocConfirmation.id == conf_id).first()
    if conf is None:
        raise ConfirmationError("确认记录不存在", 404)
    return conf


def _load_doc(db: Session, conf: DocConfirmation) -> SourceDocument:
    doc = db.query(SourceDocument).filter(SourceDocument.id == conf.document_id).first()
    if doc is None:
        raise ConfirmationError("文档已不存在", 404)
    return doc


def get_detail(db: Session, conf_id: str, user: User) -> dict:
    """确认记录详情（含文档正文预览）

    可看的人：被邀请的确认人、发起人、管理员。
    """
    conf = _get_conf(db, conf_id)
    if conf.user_id != user.id and conf.requested_by != user.id and not is_staff(user):
        raise ConfirmationError("无权查看该确认记录", 403)
    doc = _load_doc(db, conf)
    names = _user_name_map(db, [conf.user_id, conf.requested_by, doc.user_id])

    content = doc.content or ""
    return {
        **serialize(conf, names, doc),
        "content": content[:PREVIEW_MAX_CHARS],
        "content_truncated": len(content) > PREVIEW_MAX_CHARS,
        "is_requester": conf.requested_by == user.id,
        "is_invitee": conf.user_id == user.id,
    }


def _apply_response(conf: DocConfirmation, action: str, comment: Optional[str]) -> None:
    if action not in (STATUS_CONFIRMED, STATUS_REJECTED):
        raise ConfirmationError("无效的确认结果", 400)
    if conf.status != STATUS_PENDING:
        raise ConfirmationError("该确认已处理，不能重复提交", 409)
    comment = (comment or "").strip()
    if action == STATUS_REJECTED and not comment:
        raise ConfirmationError("请说明需要调整的地方（一句话即可）", 400)
    if len(comment) > 1000:
        raise ConfirmationError("意见最多 1000 字", 400)

    conf.status = action
    conf.comment = comment or None
    conf.resolved_at = _now()


def respond(
    db: Session, conf_id: str, user: User, action: str, comment: Optional[str] = None
) -> dict:
    """登录态提交确认结果（只能处理邀请给自己的记录）"""
    conf = _get_conf(db, conf_id)
    if conf.user_id != user.id:
        raise ConfirmationError("这条确认不是发给你的", 403)
    _apply_response(conf, action, comment)
    db.commit()
    db.refresh(conf)
    names = _user_name_map(db, [conf.user_id, conf.requested_by])
    return serialize(conf, names)


# --------------------------------------------------------------------------
# 文档维度进度
# --------------------------------------------------------------------------

def _latest_per_user(rows: List[DocConfirmation]) -> List[DocConfirmation]:
    """同一人可能有多轮记录（改完再确认），进度只认最新一条"""
    latest: Dict[str, DocConfirmation] = {}
    for r in rows:
        cur = latest.get(r.user_id)
        if cur is None or (r.created_at or datetime.min) >= (cur.created_at or datetime.min):
            latest[r.user_id] = r
    return list(latest.values())


def summarize(rows: List[DocConfirmation]) -> dict:
    """统计口径：分母 = 被邀请过的人数（撤销的整条记录不计入）"""
    latest = _latest_per_user([r for r in rows if r.status != STATUS_CANCELLED])
    total = len(latest)
    confirmed = sum(1 for r in latest if r.status == STATUS_CONFIRMED)
    rejected = sum(1 for r in latest if r.status == STATUS_REJECTED)
    pending = sum(1 for r in latest if r.status == STATUS_PENDING)
    return {
        "total": total,
        "confirmed": confirmed,
        "rejected": rejected,
        "pending": pending,
        "all_confirmed": total > 0 and confirmed == total,
    }


def summary_map(db: Session, doc_ids: List[str]) -> Dict[str, dict]:
    """批量取多份文档的确认进度（文档列表/任务详情用，避免 N+1）"""
    if not doc_ids:
        return {}
    rows = (
        db.query(DocConfirmation)
        .filter(DocConfirmation.document_id.in_(doc_ids))
        .all()
    )
    grouped: Dict[str, List[DocConfirmation]] = {}
    for r in rows:
        grouped.setdefault(r.document_id, []).append(r)
    return {doc_id: summarize(rs) for doc_id, rs in grouped.items()}


def list_for_document(db: Session, doc_id: str, user: User) -> dict:
    """文档的确认进度明细（文档可读即可看）"""
    doc = source_document_service.get_document(db, doc_id, user)
    if doc is None:
        raise ConfirmationError("文档不存在", 404)

    rows = (
        db.query(DocConfirmation)
        .filter(DocConfirmation.document_id == doc_id)
        .order_by(DocConfirmation.created_at.desc())
        .all()
    )
    names = _user_name_map(
        db,
        [r.user_id for r in rows] + [r.requested_by for r in rows] + [doc.user_id],
    )
    # 明细按人给最新一条，但保留每个人的轮次总数（看得出「改过一轮又确认」）
    rounds: Dict[str, int] = {}
    for r in rows:
        if r.status != STATUS_CANCELLED:
            rounds[r.user_id] = rounds.get(r.user_id, 0) + 1

    items = []
    for r in _latest_per_user([r for r in rows if r.status != STATUS_CANCELLED]):
        items.append({**serialize(r, names, doc), "rounds": rounds.get(r.user_id, 1)})
    items.sort(key=lambda x: x["created_at"])

    # 已升版：旧版的确认结果不能代表最新版的结论，前端要明确提示重新发起
    superseded = (
        db.query(SourceDocument.id)
        .filter(SourceDocument.supersedes_id == doc.id)
        .first()
    )
    return {
        "document_id": doc.id,
        "filename": doc.filename,
        "version": getattr(doc, "version", None) or 1,
        "is_latest": superseded is None,
        "summary": summarize(rows),
        "public_enabled": bool(settings.DOC_CONFIRM_PUBLIC_ENABLED),
        "items": items,
    }


# --------------------------------------------------------------------------
# 免登录通道（默认关闭）
# --------------------------------------------------------------------------

def _resolve_token(db: Session, token: str) -> Tuple[DocConfirmation, SourceDocument]:
    if not settings.DOC_CONFIRM_PUBLIC_ENABLED:
        # 关闭时连"存不存在"都不暴露
        raise ConfirmationError("链接无效或已失效", 404)
    if not token or len(token) < 20:
        raise ConfirmationError("链接无效或已失效", 404)
    conf = db.query(DocConfirmation).filter(DocConfirmation.token == token).first()
    if conf is None or conf.status == STATUS_CANCELLED:
        raise ConfirmationError("链接无效或已失效", 404)
    if conf.token_expires_at and conf.token_expires_at < _now():
        raise ConfirmationError("链接已过期，请联系发起人重新发送", 410)
    doc = _load_doc(db, conf)
    return conf, doc


def public_view(db: Session, token: str) -> dict:
    """免登录查看：只暴露这一份文档的必要信息，不含任何其他人的数据"""
    conf, doc = _resolve_token(db, token)

    # 已读回执：第一次打开即记录（发起人能看到「已发链接但还没看」和「看了没表态」）
    if conf.first_viewed_at is None:
        conf.first_viewed_at = _now()
        db.commit()
        db.refresh(conf)

    names = _user_name_map(db, [conf.user_id, conf.requested_by, doc.user_id])
    content = doc.content or ""
    return {
        "confirmation_id": conf.id,
        "status": conf.status,
        "comment": conf.comment or "",
        "message": conf.message or "",
        "doc_version": conf.doc_version or 1,
        "created_at": conf.created_at.isoformat() if conf.created_at else "",
        "resolved_at": conf.resolved_at.isoformat() if conf.resolved_at else None,
        "expires_at": conf.token_expires_at.isoformat() if conf.token_expires_at else None,
        "confirm_user_name": names.get(conf.user_id, ""),
        "requested_by_name": names.get(conf.requested_by, ""),
        "filename": doc.filename,
        "file_size": doc.file_size or 0,
        "file_type": doc.file_type or "",
        "version": getattr(doc, "version", None) or 1,
        "owner_name": names.get(doc.user_id, ""),
        "content": content[:PREVIEW_MAX_CHARS],
        "content_truncated": len(content) > PREVIEW_MAX_CHARS,
        "download_url": f"/api/public/confirmations/{token}/download",
    }


def public_download(db: Session, token: str) -> SourceDocument:
    """免登录下载原始文件（确认人要看的就是那张表，纯 Markdown 预览可能失真）"""
    _, doc = _resolve_token(db, token)
    return doc


def download_document(db: Session, conf_id: str, user: User) -> SourceDocument:
    """登录态下载原始文件：权限与 get_detail 一致（被确认人/发起人/管理员）"""
    conf = _get_conf(db, conf_id)
    if conf.user_id != user.id and conf.requested_by != user.id and not is_staff(user):
        raise ConfirmationError("无权查看该确认记录", 403)
    return _load_doc(db, conf)


def public_respond(db: Session, token: str, action: str, comment: Optional[str] = None) -> dict:
    conf, _ = _resolve_token(db, token)
    _apply_response(conf, action, comment)
    db.commit()
    db.refresh(conf)
    return {
        "status": conf.status,
        "comment": conf.comment or "",
        "resolved_at": conf.resolved_at.isoformat() if conf.resolved_at else None,
    }
