"""备忘录文件夹层级回归（子文件夹 / 层级上限 / 防环 / 删除上移 / 聚合视图）

守护点：
1. 子文件夹创建：parent_id 挂载正确，最多 3 层（第 4 层拦截）；
2. 同名拦截限定在兄弟层级：同级重名 400，不同层级同名允许；
3. 移动校验：不能移到自身或自己的子孙（防环），层级超限拦截；
4. 删除父文件夹：其备忘录移入「未分组」、子文件夹上移一级；
5. 聚合视图：选中父文件夹时能查到子孙文件夹里的备忘录。
"""
from tests.helpers import expect, body, hdr

FOLDER_API = "/api/memo-folders"
MEMO_API = "/api/memos"


def _create_folder(client, token, name, parent_id=None):
    r = client.post(
        FOLDER_API,
        json={"name": name, "parent_id": parent_id},
        headers=hdr(token),
    )
    return r


def _make_memo(client, token, title, folder_id=None):
    r = client.post(
        MEMO_API,
        json={"title": title, "folder_id": folder_id},
        headers=hdr(token),
    )
    expect(r, 201, f"创建备忘录「{title}」")
    return body(r)


def _folder_map(client, token) -> dict:
    r = client.get(FOLDER_API, headers=hdr(token))
    expect(r, 200, "文件夹列表")
    return {f["name"]: f for f in body(r)["items"]}


# ===== 1. 子文件夹创建与层级上限 =====

def test_subfolder_create_and_depth_limit(client, ctx):
    token = ctx["ua"]["token"]

    r = _create_folder(client, token, "层级测试-顶层")
    expect(r, 201, "创建顶层文件夹")
    root = body(r)
    assert root["parent_id"] is None

    r = _create_folder(client, token, "层级测试-二层", root["id"])
    expect(r, 201, "创建二层子文件夹")
    child = body(r)
    assert child["parent_id"] == root["id"]

    r = _create_folder(client, token, "层级测试-三层", child["id"])
    expect(r, 201, "创建三层子文件夹")
    grandchild = body(r)

    # 第 4 层应被拦截（MAX_FOLDER_DEPTH=3）
    r = _create_folder(client, token, "层级测试-四层", grandchild["id"])
    expect(r, 400, "第 4 层文件夹应被拦截")

    # 清理
    for fid in (grandchild["id"], child["id"], root["id"]):
        client.delete(f"{FOLDER_API}/{fid}", headers=hdr(token))


# ===== 2. 同名拦截限定兄弟层级 =====

def test_sibling_name_conflict_only(client, ctx):
    token = ctx["ua"]["token"]

    r = _create_folder(client, token, "重名测试-父")
    root = body(r)

    # 父夹下建与父夹同名的子文件夹 → 允许（同名拦截只限兄弟层级，跨层不冲突）
    r = _create_folder(client, token, "重名测试-父", root["id"])
    expect(r, 201, "不同层级同名应允许")
    sub = body(r)

    # 同级（父夹下）再建同名 → 400
    expect(_create_folder(client, token, "重名测试-父", root["id"]), 400, "同级重名应拦截")

    # 顶层再建同名（与第 1 个顶层夹冲突）→ 400
    expect(_create_folder(client, token, "重名测试-父"), 400, "顶层同名应拦截")

    # 清理
    for fid in (sub["id"], root["id"]):
        client.delete(f"{FOLDER_API}/{fid}", headers=hdr(token))


# ===== 3. 防环与移动校验 =====

def test_move_cycle_prevention(client, ctx):
    token = ctx["ua"]["token"]

    root = body(_create_folder(client, token, "防环-顶层"))
    child = body(_create_folder(client, token, "防环-二层", root["id"]))
    grandchild = body(_create_folder(client, token, "防环-三层", child["id"]))

    # 移到自身 → 400
    expect(
        client.put(f"{FOLDER_API}/{root['id']}", json={"parent_id": root["id"]}, headers=hdr(token)),
        400, "移到自身应拦截",
    )
    # 移到自己的子孙 → 400（会成环）
    expect(
        client.put(f"{FOLDER_API}/{root['id']}", json={"parent_id": grandchild["id"]}, headers=hdr(token)),
        400, "移到自己的子孙应拦截",
    )

    # 清理
    for fid in (grandchild["id"], child["id"], root["id"]):
        client.delete(f"{FOLDER_API}/{fid}", headers=hdr(token))


# ===== 4. 删除父文件夹：备忘录移入未分组、子文件夹上移 =====

def test_delete_folder_promotes_children(client, ctx, db):
    from app.models import Memo
    from tests.helpers import user_obj

    token = ctx["ub"]["token"]
    user_id = user_obj(db, ctx["ub"]["user"]["username"]).id

    root = body(_create_folder(client, token, "删除测试-顶层"))
    child = body(_create_folder(client, token, "删除测试-二层", root["id"]))

    memo_root = _make_memo(client, token, "删除测试-父夹内待办", root["id"])
    memo_child = _make_memo(client, token, "删除测试-子夹内待办", child["id"])

    expect(
        client.delete(f"{FOLDER_API}/{root['id']}", headers=hdr(token)),
        204, "删除父文件夹",
    )

    # 子文件夹上移一级
    folders = _folder_map(client, token)
    assert "删除测试-二层" in folders, "子文件夹删除父级后应仍存在"
    assert folders["删除测试-二层"]["parent_id"] is None, "子文件夹应上移为顶层"

    # 父夹内备忘录移入未分组
    memo = db.query(Memo).filter(Memo.id == memo_root["id"]).first()
    assert memo is not None, "备忘录不应被删除"
    assert memo.folder_id is None, "父夹内备忘录应移入未分组"
    memo_child_row = db.query(Memo).filter(Memo.id == memo_child["id"]).first()
    assert memo_child_row.folder_id == child["id"], "子夹内备忘录归属不变"

    # 清理
    client.delete(f"{FOLDER_API}/{child['id']}", headers=hdr(token))
    client.delete(f"{MEMO_API}/{memo_root['id']}", headers=hdr(token))
    client.delete(f"{MEMO_API}/{memo_child['id']}", headers=hdr(token))


# ===== 5. 聚合视图：选中父文件夹包含子孙内容 =====

def test_folder_aggregate_view(client, ctx):
    token = ctx["uc"]["token"]

    root = body(_create_folder(client, token, "聚合-顶层"))
    child = body(_create_folder(client, token, "聚合-二层", root["id"]))

    _make_memo(client, token, "聚合-父夹待办", root["id"])
    _make_memo(client, token, "聚合-子夹待办", child["id"])

    # 选中父文件夹应同时看到父夹与子夹的备忘录
    r = client.get(MEMO_API, params={"folder_id": root["id"], "limit": 50}, headers=hdr(token))
    expect(r, 200, "父文件夹聚合查询")
    titles = [m["title"] for m in body(r)["items"]]
    assert "聚合-父夹待办" in titles, "父夹自身内容应在结果中"
    assert "聚合-子夹待办" in titles, "子夹内容应被聚合进父文件夹视图"

    # 清理
    for fid in (child["id"], root["id"]):
        client.delete(f"{FOLDER_API}/{fid}", headers=hdr(token))
    r = client.get(MEMO_API, params={"ungrouped": "true", "limit": 50}, headers=hdr(token))
    for m in body(r)["items"]:
        if m["title"].startswith("聚合-"):
            client.delete(f"{MEMO_API}/{m['id']}", headers=hdr(token))
