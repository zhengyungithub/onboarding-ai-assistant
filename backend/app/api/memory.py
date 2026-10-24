"""记忆 API（改造 4）：查看 / 清空自己的长期记忆，管理员查看未答问题、重建记忆

全部为**新增**接口，不改动任何既有接口——`AGENT_MEMORY_ENABLED=false` 时
这些接口依然可用（只是返回的都是空状态），因此不影响前端或灰度回滚。

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| GET    | `/api/memory/me`            | 登录 | 我的记忆状态（短期轮数/摘要/偏好/未答数） |
| GET    | `/api/memory/summary`       | 登录 | 我的会话摘要正文 |
| DELETE | `/api/memory/me/preference` | 登录 | 清空我的长期偏好（隐私自助，立即生效） |
| GET    | `/api/memory/unanswered`    | 登录 | 未答问题（普通用户仅自己；`scope=all` 需超管） |
| POST   | `/api/memory/rebuild`       | 管理员 | 重建指定用户的摘要与历史索引（用于离线校准） |
| GET    | `/api/memory/stats`         | 管理员 | 记忆层统计（用户数/摘要数/未答数，判断是否在生效） |
"""
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import memory
from app.core.deps import get_current_staff, get_current_user
from app.core.roles import ROLE_SUPER_ADMIN
from app.db.session import get_db
from app.models import ConversationSummary, UnansweredQuestion, User, UserMemory

router = APIRouter(prefix="/api/memory", tags=["记忆"])


@router.get("/me")
def my_memory(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """我的记忆状态（含摘要正文、偏好词与命中计数）"""
    return memory.stats(db, current_user.id)


@router.get("/summary")
def my_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """我的会话摘要（会话很短时为空的正常情况，不是错误）"""
    return {
        "summary": memory.summary_text(db, current_user.id),
        "short_turns": memory.short_turns(),
        "enabled": memory.enabled(),
    }


@router.delete("/me/preference")
def clear_my_preference(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """清空我的长期偏好（用户可自助；只删偏好，不动对话与摘要）"""
    removed = memory.clear_preference(db, current_user.id)
    return {"cleared": removed, "preference": memory.preference_snapshot(db, current_user.id)}


class RebuildRequest(BaseModel):
    """记忆重建请求（管理员）

    - `user_id` 留空 = 重建自己的记忆；
    - `force=True` 时丢弃既有摘要重新生成（默认），`False` 则只做增量刷新。
    """

    user_id: str = Field(default="", description="要重建的用户 id（留空=自己）")
    force: bool = Field(default=True, description="是否丢弃既有摘要重新生成")


@router.post("/rebuild")
def rebuild_memory(
    payload: RebuildRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
) -> Dict[str, Any]:
    """重建某用户的记忆（摘要 + 历史索引）

    用途：调过摘要阈值/换过 embedding 模型之后，不用等用户下次提问就能让
    记忆生效；也方便排查"召回不准"是索引问题还是阈值问题。
    """
    target = current_user
    if payload.user_id and payload.user_id != current_user.id:
        if current_user.role != ROLE_SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="只有超级管理员可以重建他人的记忆")
        target = db.query(User).filter(User.id == payload.user_id).first()
        if target is None:
            raise HTTPException(status_code=404, detail="用户不存在")
    result = memory.rebuild(db, target, force=payload.force)
    result["user_id"] = target.id
    return result


@router.get("/unanswered")
def list_unanswered(
    scope: str = Query("me", description="me=仅自己；all=全站（仅超管）"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """未答问题列表（知识库缺口线索）

    普通用户只看自己的（用于确认"我提的问题确实没被收录"）；
    `scope=all` 全站视角仅超管可用，是改造 5 缺口聚类的原料。
    """
    if scope == "all":
        if current_user.role != ROLE_SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="只有超级管理员可以查看全站未答问题")
        return memory.list_unanswered(db, user_id=None, skip=skip, limit=limit)
    return memory.list_unanswered(db, user_id=current_user.id, skip=skip, limit=limit)


@router.get("/stats")
def memory_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
) -> Dict[str, Any]:
    """记忆层整体统计（管理员）

    判断"记忆是否真的在生效"的最小依据：有多少用户有偏好、有多少份摘要、
    累计多少条未答问题。全部为 0 说明开关没开或用户量还太少。
    """
    try:
        return {
            "config": memory.mode_info(),
            "users_with_preference": db.query(UserMemory).count(),
            "summaries": db.query(ConversationSummary).count(),
            "unanswered": db.query(UnansweredQuestion).count(),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"统计失败: {exc}")
