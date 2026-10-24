"""任务实例 API"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.core.deps import get_current_user
from app.models import User
from app.schemas.task import (
    TaskCreate,
    TaskUpdate,
    TaskOut,
    TaskDetailOut,
    TaskProgressLogCreate,
    TaskProgressLogOut,
    TaskNodeCompleteRequest,
)
from app.services import task_service

router = APIRouter(prefix="/api/tasks", tags=["任务管理"])


@router.post("", response_model=TaskOut)
def create_task(data: TaskCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """基于流程模板创建任务"""
    try:
        task = task_service.create_task(db, user, data)
        return task
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("")
def list_tasks(status: str = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """获取任务列表"""
    from app.models import Flow
    tasks = task_service.get_tasks(db, user.id, status)
    # 批量查询所有相关流程
    all_flow_ids = set()
    for t in tasks:
        all_flow_ids.update(t.flow_id_list)
    flow_map = {}
    if all_flow_ids:
        flows = db.query(Flow).filter(Flow.id.in_(all_flow_ids)).all()
        flow_map = {f.id: f for f in flows}

    result = []
    for t in tasks:
        flow_ids = t.flow_id_list
        flow_names = [flow_map[fid].name for fid in flow_ids if fid in flow_map]
        result.append(TaskOut(
            id=t.id, flow_id=t.flow_id, flow_ids=flow_ids, name=t.name,
            current_node_id=t.current_node_id, status=t.status, due_date=t.due_date,
            created_at=t.created_at, updated_at=t.updated_at,
            flow_name=flow_names[0] if flow_names else "",
            flow_names=flow_names,
        ))
    return {"tasks": result}


@router.get("/agenda")
def get_agenda(days: int = 7, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """到期视图：合并任务与备忘录的截止时间，返回 逾期 / 今日 / 未来 N 天 三档。

    注意：本路由必须定义在 /{task_id} 之前，否则会被路径参数抢先匹配。
    """
    days = max(1, min(days, 30))
    return task_service.get_agenda(db, user, days=days)


@router.get("/{task_id}", response_model=TaskDetailOut)
def get_task(task_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """获取任务详情（含节点进度）"""
    detail = task_service.get_task_detail(db, task_id, user)
    if not detail:
        raise HTTPException(status_code=404, detail="任务不存在")
    return detail


@router.put("/{task_id}", response_model=TaskOut)
def update_task(task_id: str, data: TaskUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """更新任务信息"""
    task = task_service.update_task(db, task_id, user.id, data)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    flow_name = task.flow.name if task.flow else ""
    return TaskOut(
        id=task.id, flow_id=task.flow_id, name=task.name,
        current_node_id=task.current_node_id, status=task.status, due_date=task.due_date,
        created_at=task.created_at, updated_at=task.updated_at,
        flow_name=flow_name,
    )


@router.delete("/{task_id}")
def delete_task(task_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """删除任务"""
    ok = task_service.delete_task(db, task_id, user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@router.post("/{task_id}/logs", response_model=TaskProgressLogOut)
def add_progress_log(
    task_id: str,
    data: TaskProgressLogCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """给某个节点记一笔进展（只追加，不覆盖历史）"""
    try:
        log = task_service.add_progress_log(db, task_id, user, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not log:
        raise HTTPException(status_code=404, detail="任务不存在")
    return log


@router.get("/{task_id}/logs")
def list_progress_logs(
    task_id: str,
    node_id: str = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询进展流水（按时间倒序，可按节点过滤）"""
    result = task_service.list_progress_logs(db, task_id, user, node_id=node_id, skip=skip, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    total, logs = result
    return {"total": total, "logs": [TaskProgressLogOut.model_validate(lg) for lg in logs]}


@router.post("/{task_id}/nodes/{node_id}/complete", response_model=TaskDetailOut)
def complete_node(
    task_id: str,
    node_id: str,
    payload: Optional[TaskNodeCompleteRequest] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """标记节点为已完成，自动推进到下一节点

    请求体可选：门禁节点必须带 remark（过闸说明）。
    前置未完成 / 门禁缺说明 / 非负责人 → 409。
    """
    detail = task_service.complete_node(
        db, task_id, node_id, user, remark=(payload.remark if payload else None)
    )
    if not detail:
        raise HTTPException(status_code=404, detail="任务或节点不存在")
    return detail


@router.post("/{task_id}/nodes/{node_id}/incomplete", response_model=TaskDetailOut)
def incomplete_node(task_id: str, node_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """标记节点为未完成，回退到该节点"""
    detail = task_service.incomplete_node(db, task_id, node_id, user)
    if not detail:
        raise HTTPException(status_code=404, detail="任务或节点不存在")
    return detail
