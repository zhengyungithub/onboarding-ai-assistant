"""任务进展流水回归（「记一笔」事件流 / 自动状态日志 / 详情聚合 / 隔离）

守护点：
1. 记一笔只追加不覆盖，原 TaskNodeProgress.remark 字段完全不受影响（向后兼容）；
2. 节点必须属于该任务，跨任务写日志一律拒绝；
3. 任务不存在 → 404，别人的任务同样 404（不泄露存在性）；
4. 详情页每节点只带最近 3 条，log_count 是真实总数；
5. 标记完成 / 回退 会自动生成对应流水，正文带节点名；
6. 列表支持按节点过滤、分页、倒序，limit 有上限钳制；
7. 删除任务级联删除流水，不留孤儿。
"""
from datetime import datetime, timedelta

from tests.helpers import (
    expect, body, hdr, make_flow, add_node, create_user, login,
)

TASK_API = "/api/tasks"


# ===== 工具 =====

def _fresh_token(client, ctx, tag: str) -> str:
    """每个用例独立用户：任务按 user_id 隔离，用例之间不能互相看见"""
    u = create_user(client, ctx["sa_token"], f"regr_log_{tag}", "user")
    return login(client, u["username"])


def _make_task(client, token: str, name: str, node_names=("节点甲", "节点乙")) -> tuple:
    """建流程 + 建任务，返回 (task, progress 列表)

    注意：节点 order 是 1-based（服务端按「插入到第 N 位」重排），
    传 0 会被收敛为 1，导致后建的节点插到最前面。这里显式从 1 开始。
    """
    flow = make_flow(client, token, f"{name}-流程")
    for i, n in enumerate(node_names):
        add_node(client, token, flow["id"], n, i + 1)
    r = client.post(TASK_API, json={"name": name, "flow_ids": [flow["id"]]}, headers=hdr(token))
    task = expect(r, 200, f"创建任务「{name}」").json()
    detail = body(client.get(f"{TASK_API}/{task['id']}", headers=hdr(token)))
    return task, detail["progress"]


def _by_name(progress: list, name: str) -> dict:
    """按节点名取进度（不依赖数组下标，避免受重排影响）"""
    return next(p for p in progress if p["node_name"] == name)


def _add_log(client, token: str, task_id: str, node_id: str, note: str):
    return client.post(
        f"{TASK_API}/{task_id}/logs",
        json={"node_id": node_id, "note": note},
        headers=hdr(token),
    )


def _add_log_ok(client, token: str, task_id: str, node_id: str, note: str) -> dict:
    r = _add_log(client, token, task_id, node_id, note)
    expect(r, 200, f"记一笔「{note}」")
    return body(r)


def _list_logs(client, token: str, task_id: str, **params):
    r = client.get(f"{TASK_API}/{task_id}/logs", params=params, headers=hdr(token))
    expect(r, 200, "查询进展流水")
    return body(r)


def _detail(client, token: str, task_id: str) -> dict:
    r = client.get(f"{TASK_API}/{task_id}", headers=hdr(token))
    expect(r, 200, "获取任务详情")
    return body(r)


def _node(detail: dict, node_id: str) -> dict:
    return next(p for p in detail["progress"] if p["node_id"] == node_id)


def _shift(db, log_id: str, seconds: int):
    """手动挪动日志时间：同一次用例内多条日志会落在同一秒，排序不可靠"""
    from app.models import TaskProgressLog
    lg = db.query(TaskProgressLog).filter(TaskProgressLog.id == log_id).first()
    assert lg is not None, f"日志不存在：{log_id}"
    lg.created_at = lg.created_at + timedelta(seconds=seconds)
    db.commit()
    db.expire_all()


# ===== 基础：记一笔 =====

def test_new_task_has_no_logs(client, ctx):
    """新任务的每个节点都是 0 条进展、recent_logs 为空"""
    token = _fresh_token(client, ctx, "empty")
    task, progress = _make_task(client, token, "空日志任务")
    for p in progress:
        assert p["log_count"] == 0
        assert not p["recent_logs"]


def test_add_log_returns_manual_kind(client, ctx):
    """记一笔默认 kind=manual，返回用户与节点归属"""
    token = _fresh_token(client, ctx, "basic")
    task, progress = _make_task(client, token, "基础任务")
    node_id = _by_name(progress, "节点甲")["node_id"]

    log = _add_log_ok(client, token, task["id"], node_id, "已把名单发给张老师，等回复")
    assert log["kind"] == "manual"
    assert log["node_id"] == node_id
    assert log["note"] == "已把名单发给张老师，等回复"
    assert log["user_id"]
    assert log["created_at"]


def test_log_appears_in_detail(client, ctx):
    """记下的进展会出现在详情的 recent_logs 里"""
    token = _fresh_token(client, ctx, "detail")
    task, progress = _make_task(client, token, "详情任务")
    node_id = _by_name(progress, "节点甲")["node_id"]
    _add_log_ok(client, token, task["id"], node_id, "材料已寄出")

    p = _node(_detail(client, token, task["id"]), node_id)
    assert p["log_count"] == 1
    assert p["recent_logs"][0]["note"] == "材料已寄出"


def test_logs_are_append_only(client, ctx):
    """同一节点记多次全部保留，且不覆盖原 remark 字段"""
    token = _fresh_token(client, ctx, "append")
    task, progress = _make_task(client, token, "追加任务")
    node_id = _by_name(progress, "节点甲")["node_id"]

    _add_log_ok(client, token, task["id"], node_id, "第一步：收集信息")
    _add_log_ok(client, token, task["id"], node_id, "第二步：提交审批")
    _add_log_ok(client, token, task["id"], node_id, "第三步：等结果")

    p = _node(_detail(client, token, task["id"]), node_id)
    assert p["log_count"] == 3
    notes = [lg["note"] for lg in p["recent_logs"]]
    assert set(notes) == {"第一步：收集信息", "第二步：提交审批", "第三步：等结果"}
    # 向后兼容：remark 仍是独立的覆盖式字段，没被流水动过
    assert p["remark"] is None


def test_note_is_stripped(client, ctx):
    """首尾空白被裁掉（用户粘贴时常带换行和空格）"""
    token = _fresh_token(client, ctx, "strip")
    task, progress = _make_task(client, token, "裁剪任务")
    log = _add_log_ok(client, token, task["id"], _by_name(progress, "节点甲")["node_id"], "  已联系财务  \n")
    assert log["note"] == "已联系财务"


# ===== 参数校验 =====

def test_blank_note_rejected(client, ctx):
    """空内容不许记（否则时间线里全是无信息的空行）"""
    token = _fresh_token(client, ctx, "blank")
    task, progress = _make_task(client, token, "空内容任务")
    r = _add_log(client, token, task["id"], _by_name(progress, "节点甲")["node_id"], "   ")
    assert r.status_code in (400, 422), f"期望被拒，实际 {r.status_code}"


def test_too_long_note_rejected(client, ctx):
    """超过 500 字被拒（与 remark 字段长度一致）"""
    token = _fresh_token(client, ctx, "toolong")
    task, progress = _make_task(client, token, "超长任务")
    r = _add_log(client, token, task["id"], _by_name(progress, "节点甲")["node_id"], "字" * 501)
    assert r.status_code in (400, 422), f"期望被拒，实际 {r.status_code}"


def test_500_chars_accepted(client, ctx):
    """刚好 500 字可以记（边界不误杀）"""
    token = _fresh_token(client, ctx, "boundary")
    task, progress = _make_task(client, token, "边界任务")
    log = _add_log_ok(client, token, task["id"], _by_name(progress, "节点甲")["node_id"], "字" * 500)
    assert len(log["note"]) == 500


# ===== 归属与权限 =====

def test_node_from_other_task_rejected(client, ctx):
    """拿另一个任务的节点 id 来记日志 → 400"""
    token = _fresh_token(client, ctx, "crossnode")
    task_a, progress_a = _make_task(client, token, "任务A")
    task_b, _ = _make_task(client, token, "任务B")
    r = _add_log(client, token, task_b["id"], _by_name(progress_a, "节点甲")["node_id"], "串到别的任务了")
    expect(r, 400, "跨任务写日志")


def test_unknown_task_404(client, ctx):
    """任务不存在 → 404"""
    token = _fresh_token(client, ctx, "no404")
    r = _add_log(client, token, "not-a-task", "not-a-node", "随便写写")
    expect(r, 404, "写不存在的任务")


def test_other_users_task_404(client, ctx):
    """别人的任务返回 404 而不是 403（不泄露任务是否存在）"""
    owner = _fresh_token(client, ctx, "owner")
    other = _fresh_token(client, ctx, "intruder")
    task, progress = _make_task(client, owner, "别人的任务")

    r = _add_log(client, other, task["id"], _by_name(progress, "节点甲")["node_id"], "越权写入")
    expect(r, 404, "给别人的任务写日志")

    r = client.get(f"{TASK_API}/{task['id']}/logs", headers=hdr(other))
    expect(r, 404, "查别人的任务日志")

    r = client.get(f"{TASK_API}/{task['id']}", headers=hdr(other))
    expect(r, 404, "查别人的任务详情")


# ===== 详情聚合 =====

def test_detail_keeps_only_recent_three(client, ctx, db):
    """详情每节点只带最近 3 条，但 log_count 是真实总数"""
    token = _fresh_token(client, ctx, "recent")
    task, progress = _make_task(client, token, "多条任务")
    node_id = _by_name(progress, "节点甲")["node_id"]

    for i in range(5):
        lg = _add_log_ok(client, token, task["id"], node_id, f"第 {i + 1} 笔")
        _shift(db, lg["id"], i)  # 拉开时间，保证顺序确定

    p = _node(_detail(client, token, task["id"]), node_id)
    assert p["log_count"] == 5
    assert len(p["recent_logs"]) == 3
    # 最新在前
    assert [lg["note"] for lg in p["recent_logs"]] == ["第 5 笔", "第 4 笔", "第 3 笔"]


def test_nodes_isolated(client, ctx):
    """两个节点的流水互不串台"""
    token = _fresh_token(client, ctx, "isolate")
    task, progress = _make_task(client, token, "隔离任务")
    a, b = _by_name(progress, "节点甲")["node_id"], _by_name(progress, "节点乙")["node_id"]

    _add_log_ok(client, token, task["id"], a, "甲节点的进展")
    _add_log_ok(client, token, task["id"], b, "乙节点的进展")
    _add_log_ok(client, token, task["id"], b, "乙节点又记一笔")

    d = _detail(client, token, task["id"])
    assert _node(d, a)["log_count"] == 1
    assert _node(d, b)["log_count"] == 2
    assert _node(d, a)["recent_logs"][0]["note"] == "甲节点的进展"


# ===== 自动流水 =====

def test_complete_node_writes_auto_log(client, ctx):
    """标记完成自动补一条 kind=complete 的流水，正文含节点名"""
    token = _fresh_token(client, ctx, "autocomplete")
    task, progress = _make_task(client, token, "自动完成任务")
    node_id = _by_name(progress, "节点甲")["node_id"]

    r = client.post(f"{TASK_API}/{task['id']}/nodes/{node_id}/complete", headers=hdr(token))
    expect(r, 200, "标记节点完成")

    p = _node(_detail(client, token, task["id"]), node_id)
    assert p["log_count"] == 1
    lg = p["recent_logs"][0]
    assert lg["kind"] == "complete"
    assert "节点甲" in lg["note"]


def test_incomplete_node_writes_auto_log(client, ctx):
    """回退节点同样留痕，kind=incomplete"""
    token = _fresh_token(client, ctx, "autoincomplete")
    task, progress = _make_task(client, token, "自动回退任务")
    node_id = _by_name(progress, "节点甲")["node_id"]

    client.post(f"{TASK_API}/{task['id']}/nodes/{node_id}/complete", headers=hdr(token))
    r = client.post(f"{TASK_API}/{task['id']}/nodes/{node_id}/incomplete", headers=hdr(token))
    expect(r, 200, "回退节点")

    p = _node(_detail(client, token, task["id"]), node_id)
    assert p["log_count"] == 2
    assert p["recent_logs"][0]["kind"] == "incomplete"
    assert p["recent_logs"][1]["kind"] == "complete"


def test_auto_and_manual_logs_coexist(client, ctx):
    """自动流水与手动记一笔在同一条时间线上共存"""
    token = _fresh_token(client, ctx, "mixed")
    task, progress = _make_task(client, token, "混合任务")
    node_id = _by_name(progress, "节点甲")["node_id"]

    _add_log_ok(client, token, task["id"], node_id, "先跟对方确认了时间")
    client.post(f"{TASK_API}/{task['id']}/nodes/{node_id}/complete", headers=hdr(token))

    p = _node(_detail(client, token, task["id"]), node_id)
    kinds = {lg["kind"] for lg in p["recent_logs"]}
    assert kinds == {"manual", "complete"}


# ===== 列表接口 =====

def test_list_logs_desc(client, ctx, db):
    """列表按时间倒序"""
    token = _fresh_token(client, ctx, "desc")
    task, progress = _make_task(client, token, "倒序任务")
    node_id = _by_name(progress, "节点甲")["node_id"]

    for i in range(3):
        lg = _add_log_ok(client, token, task["id"], node_id, f"进展 {i + 1}")
        _shift(db, lg["id"], i)

    res = _list_logs(client, token, task["id"])
    assert res["total"] == 3
    assert [lg["note"] for lg in res["logs"]] == ["进展 3", "进展 2", "进展 1"]


def test_list_logs_filter_by_node(client, ctx):
    """按 node_id 过滤只返回该节点"""
    token = _fresh_token(client, ctx, "filternode")
    task, progress = _make_task(client, token, "过滤任务")
    a, b = _by_name(progress, "节点甲")["node_id"], _by_name(progress, "节点乙")["node_id"]
    _add_log_ok(client, token, task["id"], a, "甲")
    _add_log_ok(client, token, task["id"], b, "乙")

    res = _list_logs(client, token, task["id"], node_id=b)
    assert res["total"] == 1
    assert res["logs"][0]["note"] == "乙"


def test_list_logs_pagination(client, ctx, db):
    """分页：total 是总数，logs 受 skip/limit 限制"""
    token = _fresh_token(client, ctx, "page")
    task, progress = _make_task(client, token, "分页任务")
    node_id = _by_name(progress, "节点甲")["node_id"]

    for i in range(5):
        lg = _add_log_ok(client, token, task["id"], node_id, f"P{i + 1}")
        _shift(db, lg["id"], i)

    res = _list_logs(client, token, task["id"], skip=1, limit=2)
    assert res["total"] == 5
    assert [lg["note"] for lg in res["logs"]] == ["P4", "P3"]


def test_list_logs_limit_clamped(client, ctx):
    """limit 超上限被钳到 200，不会让接口被当成导出用"""
    token = _fresh_token(client, ctx, "clamp")
    task, progress = _make_task(client, token, "钳制任务")
    _add_log_ok(client, token, task["id"], _by_name(progress, "节点甲")["node_id"], "一条")
    res = _list_logs(client, token, task["id"], limit=99999)
    assert res["total"] == 1


def test_list_logs_empty_task(client, ctx):
    """没有进展的任务返回空列表而不是报错"""
    token = _fresh_token(client, ctx, "emptylist")
    task, _ = _make_task(client, token, "无日志任务")
    res = _list_logs(client, token, task["id"])
    assert res["total"] == 0
    assert res["logs"] == []


# ===== 级联与兼容 =====

def test_delete_task_cascades_logs(client, ctx, db):
    """删任务要把流水一起删掉，不留孤儿记录"""
    from app.models import TaskProgressLog

    token = _fresh_token(client, ctx, "cascade")
    task, progress = _make_task(client, token, "级联任务")
    _add_log_ok(client, token, task["id"], _by_name(progress, "节点甲")["node_id"], "会被级联删除")

    before = db.query(TaskProgressLog).filter(TaskProgressLog.task_id == task["id"]).count()
    assert before == 1

    r = client.delete(f"{TASK_API}/{task['id']}", headers=hdr(token))
    expect(r, 200, "删除任务")

    db.expire_all()
    after = db.query(TaskProgressLog).filter(TaskProgressLog.task_id == task["id"]).count()
    assert after == 0


def test_task_list_unaffected(client, ctx):
    """任务列表接口不受流水表影响，字段保持原样"""
    token = _fresh_token(client, ctx, "listcompat")
    task, progress = _make_task(client, token, "列表兼容任务")
    _add_log_ok(client, token, task["id"], _by_name(progress, "节点甲")["node_id"], "随便记一笔")

    r = client.get(TASK_API, headers=hdr(token))
    expect(r, 200, "任务列表")
    row = next(t for t in body(r)["tasks"] if t["id"] == task["id"])
    assert "due_date" in row
    assert "log_count" not in row  # 列表不背这个负担，只在详情里给
