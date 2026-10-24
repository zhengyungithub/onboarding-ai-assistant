"""任务到期视图回归（due_date 字段 / 三档分档 / 合并备忘录 / 权限隔离）

守护点：
1. due_date 可选，缺省为 None（旧任务数据兼容）；
2. 更新时支持设置，也支持显式传 null 清空；
3. 分档口径：逾期 < 今天 00:00 <= 今日 < 明天 00:00 <= 未来 N 天；
4. 只统计未完成任务与未完成备忘录；
5. 到期视图合并备忘录（type=memo），且严格按用户隔离；
6. 超出窗口的远期项不返回；
7. 路由顺序：/api/tasks/agenda 不被 /{task_id} 抢先匹配。
"""
from datetime import datetime, timedelta

from tests.helpers import (
    expect, body, hdr, make_flow, add_node, create_user, login,
)

TASK_API = "/api/tasks"
MEMO_API = "/api/memos"


# ===== 工具 =====

def _fresh_token(client, ctx, tag: str) -> str:
    """每个用例独立用户，避免 agenda 是「当前用户全量」造成的相互干扰"""
    u = create_user(client, ctx["sa_token"], f"regr_due_{tag}", "user")
    return login(client, u["username"])


def _today_start() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _make_flow(client, token: str, name: str) -> dict:
    flow = make_flow(client, token, name)
    add_node(client, token, flow["id"], "节点一", 0)
    return flow


def _make_task(client, token: str, name: str, flow_id: str,
               due=None, status=None) -> dict:
    payload = {"name": name, "flow_ids": [flow_id]}
    if due is not None:
        payload["due_date"] = due
    r = client.post(TASK_API, json=payload, headers=hdr(token))
    expect(r, 200, f"创建任务「{name}」")
    task = body(r)
    if status:
        r2 = client.put(f"{TASK_API}/{task['id']}", json={"status": status}, headers=hdr(token))
        expect(r2, 200, f"更新任务「{name}」状态")
        task = body(r2)
    return task


def _make_memo(client, token: str, title: str, due=None, completed=False) -> dict:
    payload = {"title": title}
    if due is not None:
        payload["due_date"] = due
    r = client.post(MEMO_API, json=payload, headers=hdr(token))
    expect(r, 201, f"创建备忘录「{title}」")
    memo = body(r)
    if completed:
        r2 = client.patch(f"{MEMO_API}/{memo['id']}/toggle", headers=hdr(token))
        expect(r2, 200, "切换备忘录完成状态")
    return memo


def _agenda(client, token: str, days: int = 7) -> dict:
    r = client.get(f"{TASK_API}/agenda", params={"days": days}, headers=hdr(token))
    expect(r, 200, "查询到期视图")
    return body(r)


def _ids(bucket) -> set:
    return {f"{item['type']}-{item['id']}" for item in bucket}


# ===== 1. 字段默认值与写读一致性 =====

def test_due_date_optional(client, ctx):
    """不传 due_date 时为 None，不影响任务创建"""
    token = _fresh_token(client, ctx, "opt")
    flow = _make_flow(client, token, "流程-可选")

    task = _make_task(client, token, "无截止任务", flow["id"])
    assert task["due_date"] is None, f"缺省应为 null，实际 {task['due_date']}"

    r = client.get(TASK_API, headers=hdr(token))
    expect(r, 200, "任务列表")
    listed = body(r)["tasks"]
    assert listed and listed[0]["due_date"] is None, "列表也应返回 null 而非报错"


def test_due_date_create_and_read(client, ctx):
    """带 due_date 创建后，列表与详情口径一致"""
    token = _fresh_token(client, ctx, "crd")
    flow = _make_flow(client, token, "流程-写入")
    due = _today_start() + timedelta(days=2, hours=10)

    task = _make_task(client, token, "有截止任务", flow["id"], due=_iso(due))
    assert task["due_date"].startswith(due.strftime("%Y-%m-%dT%H:%M")), task["due_date"]

    r = client.get(f"{TASK_API}/{task['id']}", headers=hdr(token))
    expect(r, 200, "任务详情")
    assert body(r)["due_date"].startswith(due.strftime("%Y-%m-%dT%H:%M"))


# ===== 2. 更新与清空 =====

def test_due_date_update_and_clear(client, ctx):
    """可改期，也可显式传 null 取消跟踪"""
    token = _fresh_token(client, ctx, "upd")
    flow = _make_flow(client, token, "流程-改期")
    first = _today_start() + timedelta(days=1, hours=9)
    second = _today_start() + timedelta(days=5, hours=9)

    task = _make_task(client, token, "改期任务", flow["id"], due=_iso(first))

    r = client.put(f"{TASK_API}/{task['id']}", json={"due_date": _iso(second)}, headers=hdr(token))
    expect(r, 200, "改期")
    assert body(r)["due_date"].startswith(second.strftime("%Y-%m-%dT%H:%M"))

    r = client.put(f"{TASK_API}/{task['id']}", json={"due_date": None}, headers=hdr(token))
    expect(r, 200, "清空截止时间")
    assert body(r)["due_date"] is None, "显式传 null 应清空"


# ===== 3. 三档分档 =====

def test_agenda_buckets(client, ctx):
    """昨天 → 逾期；今天 → 今日；3 天后 → 未来 7 天"""
    token = _fresh_token(client, ctx, "bkt")
    flow = _make_flow(client, token, "流程-分档")
    base = _today_start()

    overdue = _make_task(client, token, "逾期任务", flow["id"],
                         due=_iso(base - timedelta(hours=12)))
    today = _make_task(client, token, "今日任务", flow["id"],
                       due=_iso(base + timedelta(hours=12)))
    later = _make_task(client, token, "三天后任务", flow["id"],
                       due=_iso(base + timedelta(days=3, hours=12)))

    agenda = _agenda(client, token)
    assert f"task-{overdue['id']}" in _ids(agenda["overdue"]), "昨天到期的任务应在逾期桶"
    assert f"task-{today['id']}" in _ids(agenda["today"]), "今天到期的任务应在今日桶"
    assert f"task-{later['id']}" in _ids(agenda["this_week"]), "3 天后到期的任务应在未来桶"

    assert agenda["counts"] == {
        "overdue": len(agenda["overdue"]),
        "today": len(agenda["today"]),
        "this_week": len(agenda["this_week"]),
    }, "counts 必须与各桶长度一致"


def test_agenda_day_boundary(client, ctx):
    """今日 00:00 属于今日桶，而非逾期；明天 00:00 属于未来桶"""
    token = _fresh_token(client, ctx, "bnd")
    flow = _make_flow(client, token, "流程-边界")
    base = _today_start()

    at_midnight = _make_task(client, token, "零点整", flow["id"], due=_iso(base))
    next_midnight = _make_task(client, token, "次日夜", flow["id"],
                               due=_iso(base + timedelta(days=1)))

    agenda = _agenda(client, token)
    assert f"task-{at_midnight['id']}" in _ids(agenda["today"]), "今天 00:00 应算今日"
    assert f"task-{next_midnight['id']}" in _ids(agenda["this_week"]), "明天 00:00 应算未来"


# ===== 4. 完成态排除 =====

def test_agenda_excludes_completed_task(client, ctx):
    """已完成任务即使过期也不进逾期桶"""
    token = _fresh_token(client, ctx, "done")
    flow = _make_flow(client, token, "流程-完成")
    base = _today_start()

    task = _make_task(client, token, "已完成任务", flow["id"],
                      due=_iso(base - timedelta(days=3)), status="completed")

    agenda = _agenda(client, token)
    assert f"task-{task['id']}" not in _ids(agenda["overdue"]), "已完成任务不应出现在逾期桶"


def test_agenda_excludes_completed_memo(client, ctx):
    """已完成备忘录同样不进桶"""
    token = _fresh_token(client, ctx, "mdone")
    base = _today_start()
    memo = _make_memo(client, token, "已办备忘", due=_iso(base + timedelta(hours=12)),
                      completed=True)

    agenda = _agenda(client, token)
    assert f"memo-{memo['id']}" not in _ids(agenda["today"]), "已完成备忘录不应出现在今日桶"


# ===== 5. 合并备忘录 =====

def test_agenda_merges_memos(client, ctx):
    """备忘录到期项与任务合并展示，type 可区分"""
    token = _fresh_token(client, ctx, "mix")
    base = _today_start()
    memo = _make_memo(client, token, "今日备忘", due=_iso(base + timedelta(hours=15)))

    agenda = _agenda(client, token)
    today_ids = _ids(agenda["today"])
    assert f"memo-{memo['id']}" in today_ids, "备忘录应合并进今日桶"

    hit = next(i for i in agenda["today"] if i["id"] == memo["id"])
    assert hit["type"] == "memo"
    assert hit["priority"] in ("low", "medium", "high"), "备忘录应带出优先级"


# ===== 6. 窗口与隔离 =====

def test_agenda_window_limit(client, ctx):
    """超出窗口的远期项不返回"""
    token = _fresh_token(client, ctx, "win")
    flow = _make_flow(client, token, "流程-窗口")
    far = _make_task(client, token, "十天后任务", flow["id"],
                     due=_iso(_today_start() + timedelta(days=10)))

    agenda = _agenda(client, token, days=7)
    all_ids = _ids(agenda["overdue"]) | _ids(agenda["today"]) | _ids(agenda["this_week"])
    assert f"task-{far['id']}" not in all_ids, "10 天后到期不应出现在 7 天窗口内"

    wider = _agenda(client, token, days=14)
    wider_ids = _ids(wider["overdue"]) | _ids(wider["today"]) | _ids(wider["this_week"])
    assert f"task-{far['id']}" in wider_ids, "窗口放宽到 14 天后应能看到"


def test_agenda_no_due_not_listed(client, ctx):
    """未设置截止时间的任务不进入任何桶"""
    token = _fresh_token(client, ctx, "nodue")
    flow = _make_flow(client, token, "流程-无期")
    task = _make_task(client, token, "无期限任务", flow["id"])

    agenda = _agenda(client, token)
    all_ids = _ids(agenda["overdue"]) | _ids(agenda["today"]) | _ids(agenda["this_week"])
    assert f"task-{task['id']}" not in all_ids, "无截止时间的任务不应被列出"


def test_agenda_user_isolation(client, ctx):
    """A 的任务不会出现在 B 的到期视图"""
    token_a = _fresh_token(client, ctx, "iso_a")
    token_b = _fresh_token(client, ctx, "iso_b")
    flow = _make_flow(client, token_a, "流程-隔离")
    task = _make_task(client, token_a, "A 的逾期任务", flow["id"],
                      due=_iso(_today_start() - timedelta(hours=6)))

    agenda_a = _agenda(client, token_a)
    assert f"task-{task['id']}" in _ids(agenda_a["overdue"])

    agenda_b = _agenda(client, token_b)
    all_b = _ids(agenda_b["overdue"]) | _ids(agenda_b["today"]) | _ids(agenda_b["this_week"])
    assert f"task-{task['id']}" not in all_b, "到期视图必须按用户隔离"
    assert agenda_b["counts"] == {"overdue": 0, "today": 0, "this_week": 0}


def test_agenda_days_param_clamped(client, ctx):
    """days 参数越界时被收敛，不报错"""
    token = _fresh_token(client, ctx, "clamp")

    low = _agenda(client, token, days=0)
    assert low["days"] == 1, "days 下限应为 1"

    high = _agenda(client, token, days=999)
    assert high["days"] == 30, "days 上限应为 30"
