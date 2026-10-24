"""主动巡检 API（改造 5）：手动触发巡检、查看巡检报告

全部为**新增**接口，不改动任何既有接口：
- `GET  /api/agent/patrol/status`         巡检配置（所有登录用户可见）
- `POST /api/agent/patrol`                跑一轮巡检（管理员；`scope=all` 仅超管）
- `GET  /api/agent/patrol/reports`        巡检报告列表（管理员）
- `GET  /api/agent/patrol/reports/{id}`   单份报告详情（管理员）

典型用法（定时任务也调同一个接口）：

```bash
# 超管全站巡检（每天 09:00）
curl -X POST http://127.0.0.1:8000/api/agent/patrol \
  -H "Authorization: Bearer <SUPER_ADMIN_TOKEN>" \
  -H "Content-Type: application/json" -d '{"scope":"all"}'
```

巡检不影响用户请求：它只在被调用时运行，产出全是草稿 + 报告，绝不自动发布。
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import patrol
from app.core.deps import get_current_staff, get_current_user
from app.core.roles import ROLE_SUPER_ADMIN, is_super_admin
from app.db.session import get_db
from app.models import User

router = APIRouter(prefix="/api/agent/patrol", tags=["巡检"])


class PatrolRequest(BaseModel):
    """巡检触发参数

    - `scope` 留空 = 自动：超管 → 全站，其他管理员 → 自己的内容；
      显式传 `all` 需要超管权限（传了也是 403），传 `me` 只巡检自己的；
    - `create_drafts` 留空 = 按配置 `AGENT_PATROL_DRAFT_ENABLED`；
      传 `false` 可以"只看报告、不建草稿"（先观察一轮再说）。
    """

    scope: str = Field(default="", description="me / all / 留空=自动（all 仅超管）")
    create_drafts: Optional[bool] = Field(
        default=None, description="是否生成草稿条目；留空=按配置"
    )


@router.get("/status")
def patrol_status(
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """巡检配置（前端可据此显示"巡检是否开启、阈值多少"）"""
    return {
        **patrol.mode_info(),
        "scope_for_me": patrol.resolve_scope(current_user, ""),
        "can_patrol_all": is_super_admin(current_user),
    }


@router.post("")
def run_patrol(
    payload: PatrolRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
) -> Dict[str, Any]:
    """跑一轮巡检（管理员）——手动触发，也是定时任务的入口

    产出：过期清单 / 缺口聚类（含草稿条目 id）/ 质量异常 / 一致性冲突 + 一句话总览。
    **全部为草稿与报告，不会自动发布任何知识。**
    """
    scope = (payload.scope or "").strip().lower()
    if scope not in ("", "me", "all"):
        raise HTTPException(status_code=422, detail="scope 只能是 me / all 或留空")
    if scope == "all" and current_user.role != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="全站巡检仅超级管理员可用")

    return patrol.run_patrol(
        db,
        current_user,
        scope=scope,
        create_drafts=payload.create_drafts,
    )


@router.get("/reports")
def list_reports(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    scope: str = Query("", description="按范围过滤：me / all，留空为全部"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
) -> Dict[str, Any]:
    """巡检报告列表（新的在前；只返回计数与总览，详情走单份接口）"""
    return patrol.list_reports(db, skip=skip, limit=limit, scope=scope.strip().lower())


@router.get("/reports/{report_id}")
def get_report(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
) -> Dict[str, Any]:
    """单份巡检报告详情（含四个区块的完整内容与生成的草稿 id）"""
    row = patrol.get_report(db, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="巡检报告不存在")
    return row
