"""流程服务：CRUD + 节点管理"""
from datetime import datetime
from typing import Optional, List
from sqlalchemy.orm import Session
from app.models import Flow, FlowNode, KnowledgeItem, User
from app.schemas.flow import FlowCreate, FlowUpdate, FlowNodeCreate
from app.core.roles import is_staff, read_scope, write_scope, apply_visibility, filter_valid_user_ids

# 节点增删改时需要同步 FlowNode 的 order


class FlowNodeValidationError(ValueError):
    """节点配置不合法（依赖越界、自依赖、成环、负责人不存在等）"""


def _validate_node_deps(db: Session, flow: Flow, deps: Optional[List[str]], node_id: Optional[str] = None) -> List[str]:
    """校验前置节点依赖，返回去重后的列表。

    三条约束：
    1. 前置节点必须属于同一流程（跨流程依赖会让任务推进语义变得不可判定）；
    2. 不能依赖自己；
    3. 不能形成环——否则任务永远推进不到任何一个节点。

    环检测用 DFS 三色标记：以「本次提交的依赖」覆盖该节点的现有依赖，
    再对整个流程做一次深度优先遍历，遇到灰色节点即说明有环。
    """
    if not deps:
        return []
    deduped: List[str] = []
    for d in deps:
        if d and d not in deduped:
            deduped.append(d)

    if node_id and node_id in deduped:
        raise FlowNodeValidationError("节点不能依赖自己")

    sibling_ids = {n.id for n in flow.nodes}
    if any(d not in sibling_ids for d in deduped):
        raise FlowNodeValidationError("前置节点必须属于同一流程")

    # 构图：所有节点用现有依赖，被测节点用新依赖
    graph = {n.id: list(n.depends_on or []) for n in flow.nodes}
    if node_id:
        graph[node_id] = deduped
    else:
        # 新建节点：它只能指向已有节点，不可能被谁引用，直接把它作为无依赖的孤立点
        graph["__new__"] = deduped

    state: dict = {}  # 0=未访问 1=访问中 2=已完成

    def _dfs(start: str) -> bool:
        stack = [(start, iter(graph.get(start, [])))]
        state[start] = 1
        while stack:
            node, it = stack[-1]
            advanced = False
            for dep in it:
                if dep not in graph:
                    continue
                if state.get(dep) == 1:
                    return True  # 回到访问中的节点 → 有环
                if state.get(dep) is None:
                    state[dep] = 1
                    stack.append((dep, iter(graph.get(dep, []))))
                    advanced = True
                    break
            if not advanced:
                state[node] = 2
                stack.pop()
        return False

    for nid in list(graph.keys()):
        if state.get(nid) is None:
            if _dfs(nid):
                raise FlowNodeValidationError("前置依赖形成环，请检查节点之间的依赖关系")

    return deduped


def _validate_owner(db: Session, owner_id: Optional[str]) -> Optional[str]:
    """负责人必须是一个存在的用户"""
    if not owner_id:
        return None
    if not db.query(User).filter(User.id == owner_id).first():
        raise FlowNodeValidationError("负责人不存在")
    return owner_id


def _resequence_nodes(flow: Flow, target_node: FlowNode, requested_order: int) -> None:
    """把 target_node 放到第 requested_order 位（1-based），
    其余节点保持原相对顺序自动顺延，最终该流程所有节点 order 连续为 1..N。

    序号超出范围时自动收敛：小于 1 视为 1，大于节点数视为排最后。
    """
    nodes = sorted(
        flow.nodes,
        key=lambda n: (n.order if n.order is not None else 0, n.created_at or datetime.now()),
    )
    others = [n for n in nodes if n.id != target_node.id]
    total = len(nodes)
    # requested_order 为 None 表示追加到最后；小于 1 收敛为 1，大于节点数收敛为最后
    if requested_order is None:
        pos = total
    else:
        pos = max(1, min(int(requested_order), total))
    ordered = others[: pos - 1] + [target_node] + others[pos - 1 :]
    for i, n in enumerate(ordered, start=1):
        n.order = i


def get_flows(db: Session, user: User, skip: int = 0, limit: int = 50) -> tuple[int, List[Flow]]:
    """流程列表：管理员全局可见；普通用户 = 自己的 + 管理员共享的模板"""
    query = db.query(Flow)
    scope = read_scope(Flow, user)
    if scope is not None:
        query = query.filter(scope)
    total = query.count()
    flows = query.order_by(Flow.created_at.desc()).offset(skip).limit(limit).all()
    return total, flows


def get_flow(db: Session, flow_id: str, user: User, for_write: bool = False) -> Optional[Flow]:
    """for_write=True 时要求"本人或管理员"（编辑/删除/管理节点）"""
    query = db.query(Flow).filter(Flow.id == flow_id)
    scope = write_scope(Flow, user) if for_write else read_scope(Flow, user)
    if scope is not None:
        query = query.filter(scope)
    return query.first()


def create_flow(db: Session, user: User, data: FlowCreate) -> Flow:
    flow = Flow(
        user_id=user.id,
        name=data.name,
        description=data.description,
    )
    # 可见范围：普通用户强制仅自己；管理员/超管默认全员共享，可选定向共享
    apply_visibility(
        flow, user, data.visibility,
        filter_valid_user_ids(db, data.shared_user_ids),
    )
    db.add(flow)
    db.flush()  # 获取 flow.id

    # 创建节点
    if data.nodes:
        for i, node_data in enumerate(data.nodes):
            node = FlowNode(
                flow_id=flow.id,
                name=node_data.name,
                description=node_data.description,
                order=node_data.order if node_data.order is not None else i,
                related_item_ids=node_data.related_item_ids,
            )
            db.add(node)

    db.commit()
    db.refresh(flow)
    return flow


def update_flow(db: Session, flow_id: str, user: User, data: FlowUpdate) -> Optional[Flow]:
    flow = get_flow(db, flow_id, user, for_write=True)
    if not flow:
        return None
    update_data = data.model_dump(exclude_unset=True)
    # 可见范围单独处理（需要归一化 + 派生 is_shared）
    vis_data = update_data.pop("visibility", None)
    shared_ids = update_data.pop("shared_user_ids", None)
    if vis_data is not None or shared_ids is not None:
        apply_visibility(
            flow, user, vis_data if vis_data is not None else flow.visibility,
            filter_valid_user_ids(db, shared_ids) if shared_ids is not None else flow.shared_user_ids,
        )
    for field, value in update_data.items():
        setattr(flow, field, value)
    db.commit()
    db.refresh(flow)
    return flow


def delete_flow(db: Session, flow_id: str, user: User) -> bool:
    flow = get_flow(db, flow_id, user, for_write=True)
    if not flow:
        return False
    db.delete(flow)  # 级联删除节点
    db.commit()
    return True


def add_node(db: Session, flow_id: str, user: User, data: FlowNodeCreate) -> Optional[FlowNode]:
    flow = get_flow(db, flow_id, user, for_write=True)
    if not flow:
        return None
    # order 默认为追加到末尾（当前节点数 + 1）
    order = data.order if data.order is not None else len(flow.nodes) + 1
    deps = _validate_node_deps(db, flow, data.depends_on)
    owner_id = _validate_owner(db, data.owner_id)
    node = FlowNode(
        flow_id=flow_id,
        name=data.name,
        description=data.description,
        order=order,
        related_item_ids=data.related_item_ids,
        depends_on=deps or None,
        owner_id=owner_id,
        is_gate=bool(data.is_gate),
    )
    db.add(node)
    db.flush()  # 获取 node.id
    # 按输入序号插入，其余节点自动顺延，保持 order 连续
    _resequence_nodes(flow, node, order)
    db.commit()
    db.refresh(node)
    return node


# 允许 update_node 直接覆盖的字段；owner_id / depends_on / related_item_ids 允许显式置空
_NODE_UPDATABLE = {"name", "description", "order", "related_item_ids", "depends_on", "owner_id", "is_gate"}
_NODE_NULLABLE = {"related_item_ids", "depends_on", "owner_id"}


def update_node(db: Session, node_id: str, user: User, data: dict) -> Optional[FlowNode]:
    query = db.query(FlowNode).join(Flow).filter(FlowNode.id == node_id)
    scope = write_scope(Flow, user)
    if scope is not None:
        query = query.filter(scope)
    node = query.first()
    if not node:
        return None
    # 依赖与负责人先校验，校验不过就不落库（避免半成品配置）
    if "depends_on" in data:
        data = dict(data)
        data["depends_on"] = _validate_node_deps(db, node.flow, data.get("depends_on"), node_id=node.id)
    if "owner_id" in data:
        data = dict(data)
        data["owner_id"] = _validate_owner(db, data.get("owner_id"))
    # 只更新允许的字段，排除时间字段（避免字符串写入 DateTime 列报错）
    for field, value in data.items():
        if field in _NODE_UPDATABLE and (value is not None or field in _NODE_NULLABLE):
            setattr(node, field, value)
    # 按输入序号插入到目标位置，其余节点自动顺延，保持 order 连续
    db.flush()
    _resequence_nodes(node.flow, node, node.order)
    db.commit()
    db.refresh(node)
    return node


def delete_node(db: Session, node_id: str, user: User) -> bool:
    query = db.query(FlowNode).join(Flow).filter(FlowNode.id == node_id)
    scope = write_scope(Flow, user)
    if scope is not None:
        query = query.filter(scope)
    node = query.first()
    if not node:
        return False
    db.delete(node)
    db.commit()
    return True


def get_next_node(db: Session, flow_id: str, current_node_id: str, user: User) -> Optional[FlowNode]:
    """获取当前节点的下一个节点"""
    flow = get_flow(db, flow_id, user)
    if not flow:
        return None
    nodes = sorted(flow.nodes, key=lambda n: n.order)
    found = False
    for node in nodes:
        if found:
            return node
        if node.id == current_node_id:
            found = True
    return None  # 已是最后一个节点


def get_related_items(db: Session, node: FlowNode) -> List[dict]:
    """获取节点关联的知识条目"""
    if not node.related_item_ids:
        return []
    items = db.query(KnowledgeItem).filter(
        KnowledgeItem.id.in_(node.related_item_ids)
    ).all()
    return [{"id": i.id, "title": i.title} for i in items]
