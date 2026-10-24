"""对话消息服务层（数据库持久化）"""
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session
from app.models.chat_message import ChatMessage


def _forget_vectors(user_id: str, message_ids: Optional[List[str]] = None, all_for_user: bool = False) -> None:
    """删消息时同步清理长期记忆里的向量（改造 4）

    不做这件事的话，用户删掉的对话仍留在 `chat_history` 里，下次提问会
    把"已经删掉的内容"当历史片段召回回来——既是残留数据也是隐私问题。
    失败静默（删消息是用户操作，不能被记忆层拖住或弄失败）。
    """
    try:
        from app.agent import memory

        memory.forget_messages_async(user_id, message_ids, all_for_user=all_for_user)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 历史向量清理调度失败（不影响删除）: {exc}")


def get_messages(db: Session, user_id: str, limit: int = 200) -> Tuple[int, List[ChatMessage]]:
    """获取当前用户的对话历史（按时间正序）"""
    query = db.query(ChatMessage).filter(ChatMessage.user_id == user_id)
    total = query.count()
    items = query.order_by(ChatMessage.created_at.asc()).limit(limit).all()
    return total, items


def create_message(
    db: Session,
    user_id: str,
    role: str,
    content: str,
    references: Optional[list] = None,
    citation_coverage: Optional[float] = None,
    citation_dropped: Optional[int] = None,
    agent_trace_id: Optional[str] = None,
) -> ChatMessage:
    """保存一条对话消息

    `citation_coverage` / `citation_dropped` / `agent_trace_id` 为可选的引用校验信息（改造 3）：
    不传时的行为与改造前完全一致。
    """
    msg = ChatMessage(
        user_id=user_id,
        role=role,
        content=content,
        references=references,
        citation_coverage=citation_coverage,
        citation_dropped=citation_dropped,
        agent_trace_id=agent_trace_id,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def delete_message(db: Session, message_id: str, user_id: str) -> bool:
    """删除单条消息（同时清掉它的历史向量，避免被后续召回）"""
    msg = db.query(ChatMessage).filter(
        ChatMessage.id == message_id,
        ChatMessage.user_id == user_id,
    ).first()
    if not msg:
        return False
    db.delete(msg)
    db.commit()
    _forget_vectors(user_id, [message_id])
    return True


def batch_delete_messages(db: Session, ids: List[str], user_id: str) -> int:
    """批量删除消息（同时清理对应历史向量）"""
    deleted = db.query(ChatMessage).filter(
        ChatMessage.id.in_(ids),
        ChatMessage.user_id == user_id,
    ).delete(synchronize_session=False)
    db.commit()
    if deleted:
        _forget_vectors(user_id, list(ids))
    return deleted


def clear_messages(db: Session, user_id: str) -> int:
    """清空当前用户所有对话

    会话级记忆一并清空（改造 4）：历史向量 + 会话摘要。
    长期偏好（L2）与未答问题台账（L3）刻意保留——前者有独立的自助清空接口，
    后者是组织级台账，都不属于"这一段对话"。
    """
    deleted = db.query(ChatMessage).filter(ChatMessage.user_id == user_id).delete(
        synchronize_session=False
    )
    db.commit()
    try:
        from app.agent import memory

        memory.clear_conversation_memory(db, user_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 会话级记忆清理失败（不影响清空对话）: {exc}")
    return deleted


def set_feedback(
    db: Session, message_id: str, user_id: str, feedback: str, reason: Optional[str] = None
) -> Optional[ChatMessage]:
    """设置消息反馈（有用/没用）

    点「没用」时记录原因；切换为有用或取消反馈时清空原因。
    """
    msg = db.query(ChatMessage).filter(
        ChatMessage.id == message_id,
        ChatMessage.user_id == user_id,
    ).first()
    if not msg:
        return None
    msg.feedback = feedback
    if feedback == "useless":
        msg.feedback_reason = (reason or "").strip() or None
    else:
        msg.feedback_reason = None
    db.commit()
    db.refresh(msg)
    return msg
