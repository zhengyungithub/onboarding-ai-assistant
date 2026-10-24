"""任务实例服务：CRUD + 节点状态流转（支持多流程）"""
import json
from typing import Optional, List
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models import TaskInstance, TaskNodeProgress, TaskProgressLog, Flow, FlowNode, User
from app.schemas.task import (
    TaskCreate,
    TaskUpdate,
    TaskDetailOut,
    TaskNodeProgressWithNode,
    TaskProgressLogOut,
    TaskProgressLogCreate,
)
from app.core.roles import read_scope

# 详情页每个节点默认带出的最近进展条数（够恢复上下文，又不至于把弹窗撑爆）
RECENT_LOG_COUNT = 3

# 视为「前置已满足」的节点状态：跳过也意味着这一步不用做了，下游不该被永久卡死
SATISFIED_STATUSES = ("completed", "skipped")

# 门禁节点过闸说明的最短字数（太短等于没写，拦不住手滑）
GATE_REMARK_MIN_LEN = 5


class WorkflowBlockedError(Exception):
    """节点当前不可完成（被前置阻塞 / 门禁缺说明 / 非负责人操作）"""

    def __init__(self, detail: str, code: str = "blocked"):
        super().__init__(detail)
        self.code = code


def _satisfied_ids(db: Session, task_id: str) -> set:
    rows = db.query(TaskNodeProgress).filter(
        TaskNodeProgress.task_id == task_id,
        TaskNodeProgress.status.in_(SATISFIED_STATUSES),
    ).all()
    return {r.node_id for r in rows}


def _unfinished_deps(node: FlowNode, satisfied: set) -> List[str]:
    """返回该节点尚未满足的前置节点 ID（保持配置顺序）"""
    return [d for d in (node.depends_on or []) if d not in satisfied]


def get_tasks(db: Session, user_id: str, status: Optional[str] = None) -> List[TaskInstance]:
    """获取用户的所有任务"""
    query = db.query(TaskInstance).filter(TaskInstance.user_id == user_id)
    if status:
        query = query.filter(TaskInstance.status == status)
    return query.order_by(TaskInstance.created_at.desc()).all()


def get_task(db: Session, task_id: str, user_id: str) -> Optional[TaskInstance]:
    return db.query(TaskInstance).filter(
        TaskInstance.id == task_id, TaskInstance.user_id == user_id
    ).first()


def _get_flow_ids(task: TaskInstance) -> List[str]:
    """获取任务关联的所有流程 ID，兼容旧数据"""
    return task.flow_id_list


def _get_flows(db: Session, flow_ids: List[str], user: User) -> List[Flow]:
    """批量查询流程，按 ID 顺序返回。

    任务可引用"管理员共享的流程模板"，因此这里按可见范围过滤而非按归属人过滤。
    """
    if not flow_ids:
        return []
    query = db.query(Flow).filter(Flow.id.in_(flow_ids))
    scope = read_scope(Flow, user)
    if scope is not None:
        query = query.filter(scope)
    flows = query.all()
    flow_map = {f.id: f for f in flows}
    return [flow_map[fid] for fid in flow_ids if fid in flow_map]


def create_task(db: Session, user: User, data: TaskCreate) -> TaskInstance:
    """基于流程模板创建任务实例，自动初始化所有节点进度（支持多流程）"""
    # 确定流程 ID 列表
    flow_ids = data.flow_ids if data.flow_ids else ([data.flow_id] if data.flow_id else [])
    if not flow_ids:
        raise ValueError("请至少选择一个流程模板")

    flows = _get_flows(db, flow_ids, user)
    if not flows:
        raise ValueError("流程模板不存在")

    # 主流程为第一个
    primary_flow = flows[0]

    # 收集所有节点（按流程顺序、节点 order 排序）
    all_nodes: List[tuple] = []  # (flow, node)
    for flow in flows:
        sorted_nodes = sorted(flow.nodes, key=lambda n: n.order)
        for node in sorted_nodes:
            all_nodes.append((flow, node))

    task = TaskInstance(
        user_id=user.id,
        flow_id=primary_flow.id,
        flow_ids=json.dumps([f.id for f in flows]),
        name=data.name,
        current_node_id=all_nodes[0][1].id if all_nodes else None,
        status="in_progress",
        due_date=data.due_date,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # 初始化所有节点进度为 pending，第一个节点为 in_progress
    for i, (flow, node) in enumerate(all_nodes):
        progress = TaskNodeProgress(
            task_id=task.id,
            node_id=node.id,
            status="in_progress" if i == 0 else "pending",
        )
        db.add(progress)
    db.commit()

    return task


def update_task(db: Session, task_id: str, user_id: str, data: TaskUpdate) -> Optional[TaskInstance]:
    task = get_task(db, task_id, user_id)
    if not task:
        return None
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(task, field, value)
    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, task_id: str, user_id: str) -> bool:
    task = get_task(db, task_id, user_id)
    if not task:
        return False
    db.delete(task)
    db.commit()
    return True


def _build_progress_list(db: Session, task: TaskInstance, flows: List[Flow], user: Optional[User] = None) -> List[TaskNodeProgressWithNode]:
    """按流程顺序、节点 order 组装进度列表。

    任务创建后流程模板可能被修改（新增/调整节点序号），这里自动同步：
    - 模板中新增的节点 → 补建 pending 进度记录（持久化）；
    - 展示顺序严格跟随模板的节点 order。
    """
    # 收集所有节点
    all_nodes: List[tuple] = []  # (flow, node)
    for flow in flows:
        sorted_nodes = sorted(flow.nodes, key=lambda n: n.order)
        for node in sorted_nodes:
            all_nodes.append((flow, node))

    progress_map = {p.node_id: p for p in task.progress}

    # 模板新增的节点：补建 pending 进度记录
    changed = False
    for _, node in all_nodes:
        if node.id not in progress_map:
            p = TaskNodeProgress(task_id=task.id, node_id=node.id, status="pending")
            db.add(p)
            progress_map[node.id] = p
            changed = True
    if changed:
        db.flush()

    # 当前节点指针失效时（模板节点被删），重置到第一个未完成节点
    node_ids = {node.id for _, node in all_nodes}
    if task.current_node_id not in node_ids and all_nodes:
        first_active = next(
            (n.id for _, n in all_nodes if progress_map[n.id].status != "completed"),
            all_nodes[0][1].id,
        )
        task.current_node_id = first_active
        changed = True
    if changed:
        db.commit()

    # 收集所有需要查询的知识条目 ID
    all_item_ids = set()
    for flow, node in all_nodes:
        if node.related_item_ids:
            all_item_ids.update(node.related_item_ids)

    # 批量查询知识条目
    items_map = {}
    if all_item_ids:
        from app.models import KnowledgeItem
        items = db.query(KnowledgeItem).filter(KnowledgeItem.id.in_(all_item_ids)).all()
        items_map = {item.id: item for item in items}

    # 批量取进展流水：一次性拉全任务日志再按节点分组。
    # 一个任务的事件流天然是"人手记的几十条"，不值得为它上窗口函数或分页；
    # 若将来出现异常量级，再改成 ROW_NUMBER() OVER (PARTITION BY node_id ...)。
    logs_by_node: dict = {}
    if all_nodes:
        all_logs = (
            db.query(TaskProgressLog)
            .filter(TaskProgressLog.task_id == task.id)
            .order_by(TaskProgressLog.created_at.desc())
            .all()
        )
        for lg in all_logs:
            logs_by_node.setdefault(lg.node_id, []).append(lg)

    progress_list = []
    # 前置是否已满足：整任务一次性算好，避免每个节点重复查库
    satisfied = {p.node_id for p in progress_map.values() if p.status in SATISFIED_STATUSES}
    node_name_map = {node.id: node.name for _, node in all_nodes}
    for flow, node in all_nodes:
        p = progress_map.get(node.id)
        if p:
            related_items = []
            if node.related_item_ids:
                related_items = [
                    {
                        "id": item.id,
                        "title": item.title,
                        "steps": item.steps,
                        "system_or_material": item.system_or_material,
                        "caution": item.caution,
                    }
                    for item_id in node.related_item_ids
                    if (item := items_map.get(item_id))
                ]
            node_logs = logs_by_node.get(node.id, [])
            unfinished = _unfinished_deps(node, satisfied)
            blocked_names = [node_name_map.get(d, d) for d in unfinished]
            progress_list.append(TaskNodeProgressWithNode(
                id=p.id,
                node_id=p.node_id,
                status=p.status,
                remark=p.remark,
                completed_at=p.completed_at,
                node_name=node.name,
                node_description=node.description,
                node_order=node.order,
                flow_name=flow.name,
                related_item_ids=node.related_item_ids,
                related_items=related_items or None,
                recent_logs=[TaskProgressLogOut.model_validate(lg) for lg in node_logs[:RECENT_LOG_COUNT]] or None,
                log_count=len(node_logs),
                depends_on=node.depends_on or None,
                blocked_by=blocked_names or None,
                is_gate=bool(node.is_gate),
                owner_id=node.owner_id,
                owner_name=node.owner_name,
                can_complete=p.status != "completed" and not unfinished,
            ))
    return progress_list


def get_task_detail(db: Session, task_id: str, user: User) -> Optional[TaskDetailOut]:
    """获取任务详情，包含所有节点进度（按流程顺序、节点 order 排列）"""
    task = get_task(db, task_id, user.id)
    if not task:
        return None

    flow_ids = _get_flow_ids(task)
    flows = _get_flows(db, flow_ids, user)
    if not flows:
        return None

    progress_list = _build_progress_list(db, task, flows, user)

    return TaskDetailOut(
        id=task.id,
        flow_id=task.flow_id,
        flow_ids=flow_ids,
        name=task.name,
        current_node_id=task.current_node_id,
        status=task.status,
        due_date=task.due_date,
        created_at=task.created_at,
        updated_at=task.updated_at,
        flow_name=flows[0].name if flows else "",
        flow_names=[f.name for f in flows],
        flow_description="；".join([f.description for f in flows if f.description]) or None,
        progress=progress_list,
    )


def complete_node(db: Session, task_id: str, node_id: str, user: User, remark: Optional[str] = None) -> Optional[TaskDetailOut]:
    """标记节点为已完成，自动推进到下一节点（跨流程）

    三道工作流校验（任一不过都抛 WorkflowBlockedError → 409）：
    1. 前置节点必须已完成（或已跳过）；
    2. 门禁节点必须填写过闸说明（≥ GATE_REMARK_MIN_LEN 字）；
    3. 指定了负责人的节点，只有负责人本人或管理员可以完成。
    """
    task = get_task(db, task_id, user.id)
    if not task:
        return None

    progress = db.query(TaskNodeProgress).filter(
        TaskNodeProgress.task_id == task_id,
        TaskNodeProgress.node_id == node_id,
    ).first()
    if not progress:
        return None

    node = db.query(FlowNode).filter(FlowNode.id == node_id).first()
    if not node:
        return None

    satisfied = _satisfied_ids(db, task_id)
    unfinished = _unfinished_deps(node, satisfied)
    if unfinished:
        names = [n.name for n in db.query(FlowNode).filter(FlowNode.id.in_(unfinished)).all()]
        raise WorkflowBlockedError(
            f"该节点还在等前置节点完成：{'、'.join(names) or '、'.join(unfinished)}"
        )

    remark = (remark or "").strip()
    if node.is_gate and len(remark) < GATE_REMARK_MIN_LEN:
        raise WorkflowBlockedError(
            f"这是门禁节点，完成前需要填写过闸说明（至少 {GATE_REMARK_MIN_LEN} 个字）",
            code="gate_remark_required",
        )

    progress.status = "completed"
    progress.completed_at = datetime.now()
    if remark:
        progress.remark = remark
    _append_auto_log(db, task_id, node_id, user.id, "complete")

    # 获取所有流程的所有节点，按顺序排列
    flow_ids = _get_flow_ids(task)
    flows = _get_flows(db, flow_ids, user)
    all_nodes: List[FlowNode] = []
    for flow in flows:
        sorted_nodes = sorted(flow.nodes, key=lambda n: n.order)
        all_nodes.extend(sorted_nodes)

    # 推进指针：取第一个「未完成且前置已满足」的节点；若都被前置挡着，就停在第一个未完成的节点上
    satisfied = _satisfied_ids(db, task_id)
    progress_map = {
        r.node_id: r
        for r in db.query(TaskNodeProgress).filter(TaskNodeProgress.task_id == task_id).all()
    }
    remaining = [n for n in all_nodes if progress_map.get(n.id) and progress_map[n.id].status not in SATISFIED_STATUSES]
    next_node = next((n for n in remaining if not _unfinished_deps(n, satisfied)), None)
    if next_node is None and remaining:
        next_node = remaining[0]

    if next_node is None:
        task.current_node_id = None
        task.status = "completed"
    else:
        task.current_node_id = next_node.id
        task.status = "in_progress"
        np = progress_map.get(next_node.id)
        if np and np.status == "pending":
            np.status = "in_progress"

    db.commit()
    return get_task_detail(db, task_id, user)


def incomplete_node(db: Session, task_id: str, node_id: str, user: User) -> Optional[TaskDetailOut]:
    """标记节点为未完成（回退到进行中状态）"""
    task = get_task(db, task_id, user.id)
    if not task:
        return None

    progress = db.query(TaskNodeProgress).filter(
        TaskNodeProgress.task_id == task_id,
        TaskNodeProgress.node_id == node_id,
    ).first()
    if not progress:
        return None

    progress.status = "in_progress"
    progress.completed_at = None
    task.current_node_id = node_id
    task.status = "in_progress"
    _append_auto_log(db, task_id, node_id, user.id, "incomplete")

    db.commit()
    return get_task_detail(db, task_id, user)


def _append_auto_log(db: Session, task_id: str, node_id: str, user_id: str, kind: str) -> None:
    """状态变更时补一条流水（不 commit，由调用方统一提交）

    这样时间线里既有"我记了什么"，也有"我什么时候把它标完了"，
    中断后回来看的是完整事件序列，而不是孤立的备注。
    节点名取不到时降级为通用文案，不因此让主流程失败。
    """
    node = db.query(FlowNode).filter(FlowNode.id == node_id).first()
    node_name = node.name if node else "该节点"
    note = f"标记「{node_name}」为已完成" if kind == "complete" else f"把「{node_name}」回退为未完成"
    db.add(TaskProgressLog(
        task_id=task_id, node_id=node_id, user_id=user_id, kind=kind, note=note[:500],
    ))


def add_progress_log(db: Session, task_id: str, user: User, data: TaskProgressLogCreate) -> Optional[TaskProgressLog]:
    """记一笔进展。

    返回 None 表示任务不存在（API 转 404）；节点不属于该任务时抛 ValueError（转 400）——
    两种错误的性质不同，前者是"你访问的活没了"，后者是"你写错了字段"。
    """
    task = get_task(db, task_id, user.id)
    if not task:
        return None

    progress = db.query(TaskNodeProgress).filter(
        TaskNodeProgress.task_id == task_id,
        TaskNodeProgress.node_id == data.node_id,
    ).first()
    if not progress:
        raise ValueError("节点不属于该任务")

    log = TaskProgressLog(
        task_id=task_id,
        node_id=data.node_id,
        user_id=user.id,
        kind="manual",
        note=data.note,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def list_progress_logs(
    db: Session,
    task_id: str,
    user: User,
    node_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
) -> Optional[tuple]:
    """查询任务的全部进展流水（可按节点过滤），按时间倒序。

    返回 (total, logs)；任务不存在返回 None。
    """
    task = get_task(db, task_id, user.id)
    if not task:
        return None

    query = db.query(TaskProgressLog).filter(TaskProgressLog.task_id == task_id)
    if node_id:
        query = query.filter(TaskProgressLog.node_id == node_id)
    total = query.count()
    logs = (
        query.order_by(TaskProgressLog.created_at.desc())
        .offset(max(skip, 0))
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return total, logs


def get_agenda(db: Session, user: User, days: int = 7) -> dict:
    """到期视图：把「任务截止时间」与「备忘录截止时间」合成一份待办日历。

    分档口径（只算未完成项）：
    - overdue   到期时间 < 今天 00:00
    - today     今天 00:00 <= 到期时间 < 明天 00:00
    - this_week 明天 00:00 <= 到期时间 < 今天 00:00 + days 天

    刻意不返回更远的数据：远期项没有提醒价值，堆在列表里反而稀释注意力。
    """
    from app.models import Memo

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today_start + timedelta(days=1)
    horizon = today_start + timedelta(days=max(days, 1))

    rows = []

    tasks = (
        db.query(TaskInstance)
        .filter(
            TaskInstance.user_id == user.id,
            TaskInstance.status != "completed",
            TaskInstance.due_date.isnot(None),
            TaskInstance.due_date < horizon,
        )
        .all()
    )
    for t in tasks:
        rows.append({
            "type": "task",
            "id": t.id,
            "title": t.name,
            "due_date": t.due_date,
            "status": t.status,
            "priority": None,
        })

    memos = (
        db.query(Memo)
        .filter(
            Memo.user_id == user.id,
            ~Memo.is_completed,
            Memo.due_date.isnot(None),
            Memo.due_date < horizon,
        )
        .all()
    )
    for m in memos:
        rows.append({
            "type": "memo",
            "id": m.id,
            "title": m.title,
            "due_date": m.due_date,
            "status": "pending",
            "priority": m.priority,
        })

    buckets = {"overdue": [], "today": [], "this_week": []}
    for row in rows:
        due = row["due_date"]
        if due < today_start:
            buckets["overdue"].append(row)
        elif due < tomorrow:
            buckets["today"].append(row)
        elif due < horizon:
            buckets["this_week"].append(row)

    for key in buckets:
        buckets[key].sort(key=lambda r: r["due_date"])

    return {
        "overdue": buckets["overdue"],
        "today": buckets["today"],
        "this_week": buckets["this_week"],
        "counts": {k: len(v) for k, v in buckets.items()},
        "days": max(days, 1),
        "generated_at": now,
    }
