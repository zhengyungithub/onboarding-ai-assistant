"""工作流增强回归（节点依赖 / 负责人标注 / 门禁节点）

守护点：
1. 依赖可保存与回显，跨流程依赖 / 自依赖 / 成环一律 400（配置错就别落库）；
2. 前置未完成时不能完成节点（409），详情里能看出被谁挡着（blocked_by / can_complete）；
3. 前置完成后可正常完成，指针推进到「依赖已满足」的节点；
4. 门禁节点必须带过闸说明（≥5 字），说明会落到 progress.remark；
5. 负责人只做责任标注（任务按人隔离，做权限会死锁），但必须校验用户存在；
6. 老流程（无依赖、非门禁）行为完全不变 —— 不能把存量流程改坏。
"""
from uuid import uuid4

from tests.helpers import (
    expect, body, hdr, make_flow, add_node, create_user, login,
)

TASK_API = "/api/tasks"
FLOW_API = "/api/flows"


def _uniq(tag: str) -> str:
    """回归库是复用的，用户名必须每次唯一"""
    return f"regr_wf_{tag}_{uuid4().hex[:6]}"


def _fresh_token(client, ctx, tag: str) -> str:
    u = create_user(client, ctx["sa_token"], _uniq(tag), "user")
    return login(client, u["username"])


def _node(client, token, flow_id, name, order, **extra):
    return add_node(client, token, flow_id, name, order, **extra)


def _task_of(client, token, flow_id, name="任务"):
    r = client.post(TASK_API, json={"name": name, "flow_ids": [flow_id]}, headers=hdr(token))
    task = expect(r, 200, "创建任务").json()
    detail = body(client.get(f"{TASK_API}/{task['id']}", headers=hdr(token)))
    return task, detail


def _by_name(detail, name):
    return next(p for p in detail["progress"] if p["node_name"] == name)


def _complete(client, token, task_id, node_id, remark=None):
    payload = {"remark": remark} if remark is not None else None
    return client.post(f"{TASK_API}/{task_id}/nodes/{node_id}/complete", json=payload, headers=hdr(token))


# ===== 依赖配置 =====

def test_dep_saved_and_returned(client, ctx):
    """依赖保存后，流程详情与任务详情都能读到"""
    token = _fresh_token(client, ctx, "dep_save")
    flow = make_flow(client, token, "依赖流程")
    n1 = _node(client, token, flow["id"], "订场地", 1)
    n2 = _node(client, token, flow["id"], "发通知", 2, depends_on=[n1["id"]])

    assert n2["depends_on"] == [n1["id"]], f"依赖没保存：{n2}"
    assert n2["is_gate"] is False

    flow_detail = body(client.get(f"{FLOW_API}/{flow['id']}", headers=hdr(token)))
    node2 = next(n for n in flow_detail["nodes"] if n["id"] == n2["id"])
    assert node2["depends_on"] == [n1["id"]]


def test_dep_cross_flow_rejected(client, ctx):
    """前置节点必须属于同一流程"""
    token = _fresh_token(client, ctx, "dep_cross")
    f1 = make_flow(client, token, "流程甲")
    f2 = make_flow(client, token, "流程乙")
    other = _node(client, token, f2["id"], "别的流程的节点", 1)

    r = client.post(
        f"{FLOW_API}/{f1['id']}/nodes",
        json={"name": "跨界节点", "order": 1, "depends_on": [other["id"]]},
        headers=hdr(token),
    )
    expect(r, 400, "跨流程依赖应被拒绝")
    assert "同一流程" in body(r)["detail"]


def test_dep_self_rejected(client, ctx):
    """不能依赖自己"""
    token = _fresh_token(client, ctx, "dep_self")
    flow = make_flow(client, token, "自依赖流程")
    n1 = _node(client, token, flow["id"], "节点一", 1)

    r = client.put(
        f"{FLOW_API}/{flow['id']}/nodes/{n1['id']}",
        json={"depends_on": [n1["id"]]},
        headers=hdr(token),
    )
    expect(r, 400, "自依赖应被拒绝")
    assert "自己" in body(r)["detail"]


def test_dep_cycle_rejected(client, ctx):
    """A→B 之后再把 B→A 会成环，必须拦住"""
    token = _fresh_token(client, ctx, "dep_cycle")
    flow = make_flow(client, token, "环流程")
    a = _node(client, token, flow["id"], "节点A", 1)
    b = _node(client, token, flow["id"], "节点B", 2, depends_on=[a["id"]])

    r = client.put(
        f"{FLOW_API}/{flow['id']}/nodes/{a['id']}",
        json={"depends_on": [b["id"]]},
        headers=hdr(token),
    )
    expect(r, 400, "成环应被拒绝")
    assert "环" in body(r)["detail"]

    # 被拒之后原依赖应保持不变（不能半成品落库）
    flow_detail = body(client.get(f"{FLOW_API}/{flow['id']}", headers=hdr(token)))
    node_a = next(n for n in flow_detail["nodes"] if n["id"] == a["id"])
    assert not node_a["depends_on"], f"A 的依赖不应被写入：{node_a}"


# ===== 前置阻塞 =====

def test_complete_blocked_by_unfinished_dep(client, ctx):
    """前置未完成 → 409，且详情能看出被谁挡着"""
    token = _fresh_token(client, ctx, "block")
    flow = make_flow(client, token, "阻塞流程")
    n1 = _node(client, token, flow["id"], "订场地", 1)
    n2 = _node(client, token, flow["id"], "发通知", 2, depends_on=[n1["id"]])
    task, detail = _task_of(client, token, flow["id"])

    # 详情先给出阻塞信息（前端据此禁用按钮）
    node2 = _by_name(detail, "发通知")
    assert node2["blocked_by"] == ["订场地"], node2
    assert node2["can_complete"] is False

    r = _complete(client, token, task["id"], n2["id"])
    expect(r, 409, "前置未完成不能完成本节点")
    assert "订场地" in body(r)["detail"]
    assert body(r)["code"] == "blocked"


def test_complete_after_dep_done(client, ctx):
    """前置完成后即可完成，指针推进到该节点"""
    token = _fresh_token(client, ctx, "unblock")
    flow = make_flow(client, token, "解阻流程")
    n1 = _node(client, token, flow["id"], "订场地", 1)
    n2 = _node(client, token, flow["id"], "发通知", 2, depends_on=[n1["id"]])
    task, detail = _task_of(client, token, flow["id"])

    assert _by_name(detail, "订场地")["can_complete"] is True
    r = _complete(client, token, task["id"], n1["id"])
    detail = expect(r, 200, "完成前置节点").json()

    assert detail["current_node_id"] == n2["id"], "指针应推进到依赖已满足的节点"
    assert _by_name(detail, "发通知")["can_complete"] is True
    assert _by_name(detail, "发通知")["blocked_by"] in (None, [])

    r2 = _complete(client, token, task["id"], n2["id"])
    expect(r2, 200, "前置满足后可以完成")
    assert r2.json()["status"] == "completed"


# ===== 门禁节点 =====

def test_gate_requires_remark(client, ctx):
    """门禁节点：无说明 / 说明太短都拒绝，合格说明会落库"""
    token = _fresh_token(client, ctx, "gate")
    flow = make_flow(client, token, "门禁流程")
    _node(client, token, flow["id"], "场地确认", 1, is_gate=True)
    task, detail = _task_of(client, token, flow["id"])
    node = _by_name(detail, "场地确认")
    assert node["is_gate"] is True

    r = _complete(client, token, task["id"], node["node_id"])
    expect(r, 409, "门禁节点无说明应被拒绝")
    assert body(r)["code"] == "gate_remark_required"

    r2 = _complete(client, token, task["id"], node["node_id"], remark="已确认")
    expect(r2, 409, "说明过短应被拒绝")

    r3 = _complete(client, token, task["id"], node["node_id"], remark="已与张老师确认场地，可发布行程")
    detail = expect(r3, 200, "合格说明可以过闸").json()
    assert detail["progress"][0]["remark"] == "已与张老师确认场地，可发布行程"
    assert detail["progress"][0]["status"] == "completed"


def test_gate_remark_not_required_for_normal_node(client, ctx):
    """普通节点不需要说明，老用法（无请求体）照常可用"""
    token = _fresh_token(client, ctx, "normal")
    flow = make_flow(client, token, "普通流程")
    _node(client, token, flow["id"], "节点一", 1)
    task, detail = _task_of(client, token, flow["id"])

    r = client.post(
        f"{TASK_API}/{task['id']}/nodes/{detail['progress'][0]['node_id']}/complete",
        headers=hdr(token),
    )
    detail = expect(r, 200, "无请求体也能完成普通节点").json()
    assert detail["status"] == "completed"


# ===== 负责人（责任标注） =====

def test_owner_displayed(client, ctx):
    """负责人姓名回显；负责人不存在 → 400"""
    token = _fresh_token(client, ctx, "owner")
    flow = make_flow(client, token, "负责人流程")
    owner = create_user(client, ctx["sa_token"], _uniq("owner_target"), "user")

    n1 = _node(client, token, flow["id"], "带负责人", 1, owner_id=owner["id"])
    assert n1["owner_id"] == owner["id"]
    assert n1["owner_name"] == owner["username"]

    r = client.post(
        f"{FLOW_API}/{flow['id']}/nodes",
        json={"name": "幽灵负责人", "order": 2, "owner_id": "not-exist-id"},
        headers=hdr(token),
    )
    expect(r, 400, "负责人不存在应被拒绝")


def test_owner_update_and_clear(client, ctx):
    """负责人与依赖都可以显式置空（配置可回退）"""
    token = _fresh_token(client, ctx, "owner_clear")
    flow = make_flow(client, token, "清空流程")
    owner = create_user(client, ctx["sa_token"], _uniq("owner_clear"), "user")
    n1 = _node(client, token, flow["id"], "节点一", 1, owner_id=owner["id"])
    n2 = _node(client, token, flow["id"], "节点二", 2, depends_on=[n1["id"]])

    r = client.put(
        f"{FLOW_API}/{flow['id']}/nodes/{n1['id']}",
        json={"owner_id": None},
        headers=hdr(token),
    )
    assert expect(r, 200, "清空负责人").json()["owner_id"] is None

    r2 = client.put(
        f"{FLOW_API}/{flow['id']}/nodes/{n2['id']}",
        json={"depends_on": []},
        headers=hdr(token),
    )
    assert expect(r2, 200, "清空依赖").json()["depends_on"] in (None, [])


# ===== 存量兼容 =====

def test_legacy_flow_still_linear(client, ctx):
    """没有依赖、没有门禁的老流程：完成即推进，行为不变"""
    token = _fresh_token(client, ctx, "legacy")
    flow = make_flow(client, token, "老流程")
    n1 = _node(client, token, flow["id"], "第一步", 1)
    n2 = _node(client, token, flow["id"], "第二步", 2)
    task, detail = _task_of(client, token, flow["id"])

    assert all(p["depends_on"] in (None, []) for p in detail["progress"])
    assert all(p["can_complete"] for p in detail["progress"])
    assert _by_name(detail, "第一步")["is_gate"] is False

    r = _complete(client, token, task["id"], n1["id"])
    detail = expect(r, 200, "老流程第一步").json()
    assert detail["current_node_id"] == n2["id"]

    r2 = _complete(client, token, task["id"], n2["id"])
    assert expect(r2, 200, "老流程第二步").json()["status"] == "completed"
