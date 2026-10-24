"""迭代 2：文档版本台账 + 绑定任务

覆盖点：
  1. 首次上传为 v1，同名再传自动 v2 / v3，supersedes_id 串成链
  2. 列表默认隐藏被取代版本；include_superseded=true 时能看到且 is_latest=false
  3. 版本链接口由旧到新返回整条链（从中间版本打开也能看到更新版）
  4. 旧版仍可下载；不同用户 / 不同文件名不串链
  5. 绑定任务：本人任务可绑、他人任务 403、不存在 404、可解绑
  6. 按 task_id 过滤；升版继承任务归属；不同任务的同名文件不串链
"""
import uuid

import pytest

from tests.helpers import (
    hdr, expect, body, upload_doc, doc_ids, make_flow,
)


def uniq(stem: str) -> str:
    """每次生成唯一文件名，避免同用户的历史测试互相升版"""
    return f"{stem}-{uuid.uuid4().hex[:6]}.txt"


def make_task(client, token: str, name: str = "测试任务") -> str:
    flow = make_flow(client, token, name + "-流程")
    r = client.post(
        "/api/tasks",
        json={"name": name, "flow_ids": [flow["id"]]},
        headers=hdr(token),
    )
    expect(r, 200, "创建任务")
    return body(r)["id"]


def list_docs(client, token: str, **params):
    r = client.get("/api/source-documents", params={"limit": 200, **params}, headers=hdr(token))
    expect(r, 200, "查询文档列表")
    return body(r)


# ===== 1. 版本号与链 =====

def test_first_upload_is_v1(client, ctx):
    d = upload_doc(client, ctx["ua"]["token"], uniq("首次上传"), "内容 A")
    assert d["version"] == 1
    assert d["supersedes_id"] is None
    assert d["is_latest"] is True
    assert d["is_new_version"] is False


def test_second_upload_bumps_version(client, ctx):
    token, fname = ctx["ua"]["token"], uniq("二次上传")
    v1 = upload_doc(client, token, fname, "第一版")
    v2 = upload_doc(client, token, fname, "第二版")

    assert v2["version"] == 2
    assert v2["supersedes_id"] == v1["id"]
    assert v2["is_new_version"] is True
    assert v2["is_latest"] is True


def test_third_upload_chain(client, ctx):
    token, fname = ctx["ua"]["token"], uniq("三次上传")
    v1 = upload_doc(client, token, fname, "v1")
    v2 = upload_doc(client, token, fname, "v2")
    v3 = upload_doc(client, token, fname, "v3")

    assert [v1["version"], v2["version"], v3["version"]] == [1, 2, 3]
    assert v3["supersedes_id"] == v2["id"]
    assert v2["supersedes_id"] == v1["id"]


def test_different_filename_no_bump(client, ctx):
    token = ctx["ua"]["token"]
    a = upload_doc(client, token, uniq("同名测试甲"), "甲")
    b = upload_doc(client, token, uniq("同名测试乙"), "乙")
    assert a["version"] == 1
    assert b["version"] == 1
    assert b["supersedes_id"] is None


def test_different_user_no_bump(client, ctx):
    """不同用户上传同名文件不应串成一条链（各自独立 v1）"""
    fname = uniq("跨用户同名")
    a = upload_doc(client, ctx["ua"]["token"], fname, "甲的内容")
    b = upload_doc(client, ctx["ub"]["token"], fname, "乙的内容")
    assert a["version"] == 1
    assert b["version"] == 1
    assert b["supersedes_id"] is None


# ===== 2. 列表默认隐藏旧版 =====

def test_list_hides_superseded(client, ctx):
    token, fname = ctx["ua"]["token"], uniq("列表隐藏")
    v1 = upload_doc(client, token, fname, "v1")
    v2 = upload_doc(client, token, fname, "v2")

    res = list_docs(client, token, keyword=fname)
    assert res["total"] == 1
    ids = [d["id"] for d in res["items"]]
    assert ids == [v2["id"]]
    assert v1["id"] not in ids


def test_list_include_superseded(client, ctx):
    token, fname = ctx["ua"]["token"], uniq("列出历史")
    v1 = upload_doc(client, token, fname, "v1")
    v2 = upload_doc(client, token, fname, "v2")

    res = list_docs(client, token, keyword=fname, include_superseded=True)
    assert res["total"] == 2
    by_id = {d["id"]: d for d in res["items"]}
    assert by_id[v1["id"]]["is_latest"] is False
    assert by_id[v2["id"]]["is_latest"] is True


def test_list_counts_are_per_version_chain(client, ctx):
    """total 与分页计数都应以「可见的最新版」为准，而不是物理行数

    用 keyword 精确圈定本次两个文件：同一用户的其它历史文档会污染全局 total，
    所以不能直接对全表计数断言。
    """
    token = ctx["uc"]["token"]
    fname_a, fname_b = uniq("计数甲"), uniq("计数乙")
    upload_doc(client, token, fname_a, "a1")
    upload_doc(client, token, fname_a, "a2")
    upload_doc(client, token, fname_b, "b1")  # b 只有一版

    a_visible = list_docs(client, token, keyword=fname_a)
    a_all = list_docs(client, token, keyword=fname_a, include_superseded=True)
    b_visible = list_docs(client, token, keyword=fname_b)
    b_all = list_docs(client, token, keyword=fname_b, include_superseded=True)

    assert a_visible["total"] == 1   # a 的 v2
    assert a_all["total"] == 2       # a 的 v1 + v2
    assert b_visible["total"] == 1
    assert b_all["total"] == 1


# ===== 3. 版本链接口 =====

def test_versions_returns_chain_old_to_new(client, ctx):
    token, fname = ctx["ua"]["token"], uniq("版本链")
    v1 = upload_doc(client, token, fname, "v1")
    v2 = upload_doc(client, token, fname, "v2")
    v3 = upload_doc(client, token, fname, "v3")

    r = client.get(f"/api/source-documents/{v3['id']}/versions", headers=hdr(token))
    expect(r, 200, "查询版本链")
    chain = body(r)["versions"]

    assert [d["version"] for d in chain] == [1, 2, 3]
    assert [d["id"] for d in chain] == [v1["id"], v2["id"], v3["id"]]
    assert [d["is_latest"] for d in chain] == [False, False, True]


def test_versions_from_middle_sees_newer(client, ctx):
    """从 v1 打开版本链，应能看到 v2（向下追链）"""
    token, fname = ctx["ua"]["token"], uniq("从中间打开")
    v1 = upload_doc(client, token, fname, "v1")
    v2 = upload_doc(client, token, fname, "v2")

    r = client.get(f"/api/source-documents/{v1['id']}/versions", headers=hdr(token))
    expect(r, 200, "从 v1 查询版本链")
    chain = body(r)["versions"]
    assert [d["id"] for d in chain] == [v1["id"], v2["id"]]


def test_versions_single_doc(client, ctx):
    d = upload_doc(client, ctx["ua"]["token"], uniq("单版本"), "唯一一版")
    r = client.get(f"/api/source-documents/{d['id']}/versions", headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "单版本链")
    chain = body(r)["versions"]
    assert len(chain) == 1
    assert chain[0]["is_latest"] is True


def test_versions_forbidden_for_other_user(client, ctx):
    """ub 拿 ua 的 doc_id 查版本链 → 404（读范围外一律当不存在）"""
    d = upload_doc(client, ctx["ua"]["token"], uniq("越权版本"), "内容")
    r = client.get(f"/api/source-documents/{d['id']}/versions", headers=hdr(ctx["ub"]["token"]))
    assert r.status_code == 404


def test_old_version_still_downloadable(client, ctx):
    token, fname = ctx["ua"]["token"], uniq("旧版可下载")
    v1 = upload_doc(client, token, fname, "旧版内容")
    upload_doc(client, token, fname, "新版内容")

    r = client.get(f"/api/source-documents/{v1['id']}/download", headers=hdr(token))
    expect(r, 200, "下载旧版")
    assert r.content.decode("utf-8") == "旧版内容"


def test_old_version_content_intact(client, ctx):
    """升版不动旧记录：旧版详情仍返回它的原文内容"""
    token, fname = ctx["ua"]["token"], uniq("旧版内容不变")
    v1 = upload_doc(client, token, fname, "第一版原文")
    upload_doc(client, token, fname, "第二版原文")

    r = client.get(f"/api/source-documents/{v1['id']}", headers=hdr(token))
    expect(r, 200, "查看旧版详情")
    d = body(r)
    assert d["version"] == 1
    assert "第一版原文" in d["content"]
    assert d["is_latest"] is False


# ===== 4. 绑定任务 =====

def test_bind_task(client, ctx):
    token = ctx["ua"]["token"]
    task_id = make_task(client, token, "绑定测试任务")
    doc = upload_doc(client, token, uniq("待绑定"), "内容")

    r = client.patch(f"/api/source-documents/{doc['id']}/task", json={"task_id": task_id}, headers=hdr(token))
    expect(r, 200, "绑定任务")
    assert body(r)["task_id"] == task_id


def test_bind_task_filters_documents(client, ctx):
    token = ctx["ua"]["token"]
    t1 = make_task(client, token, "任务甲")
    t2 = make_task(client, token, "任务乙")

    d1 = upload_doc(client, token, uniq("任务甲材料"), "甲材料", task_id=t1)
    d2 = upload_doc(client, token, uniq("任务乙材料"), "乙材料", task_id=t2)
    upload_doc(client, token, uniq("无归属材料"), "无归属")

    assert d1["task_id"] == t1
    res = list_docs(client, token, task_id=t1)
    ids = {d["id"] for d in res["items"]}
    assert ids == {d1["id"]}
    assert d2["id"] not in ids


def test_bind_task_of_other_user_forbidden(client, ctx):
    ua_task = make_task(client, ctx["ua"]["token"], "甲的私有任务")
    doc = upload_doc(client, ctx["ub"]["token"], uniq("乙的文档"), "内容")

    r = client.patch(
        f"/api/source-documents/{doc['id']}/task",
        json={"task_id": ua_task},
        headers=hdr(ctx["ub"]["token"]),
    )
    assert r.status_code == 403


def test_bind_missing_task_404(client, ctx):
    token = ctx["ua"]["token"]
    doc = upload_doc(client, token, uniq("绑定不存在任务"), "内容")
    r = client.patch(
        f"/api/source-documents/{doc['id']}/task",
        json={"task_id": "no-such-task-id"},
        headers=hdr(token),
    )
    assert r.status_code == 404


def test_upload_with_missing_task_404(client, ctx):
    r = client.post(
        "/api/source-documents/upload",
        files={"file": ("x.txt", b"content", "text/plain")},
        data={"task_id": "no-such-task-id"},
        headers=hdr(ctx["ua"]["token"]),
    )
    assert r.status_code == 404


def test_unbind_task(client, ctx):
    token = ctx["ua"]["token"]
    task_id = make_task(client, token, "解绑测试任务")
    doc = upload_doc(client, token, uniq("待解绑"), "内容", task_id=task_id)
    assert doc["task_id"] == task_id

    r = client.patch(f"/api/source-documents/{doc['id']}/task", json={"task_id": None}, headers=hdr(token))
    expect(r, 200, "解除绑定")
    assert body(r)["task_id"] is None

    res = list_docs(client, token, task_id=task_id)
    assert doc["id"] not in {d["id"] for d in res["items"]}


def test_bind_document_of_other_user_forbidden(client, ctx):
    """不能改别人的文档归属"""
    doc = upload_doc(client, ctx["ua"]["token"], uniq("甲的文档"), "内容")
    my_task = make_task(client, ctx["ub"]["token"], "乙的任务")
    r = client.patch(
        f"/api/source-documents/{doc['id']}/task",
        json={"task_id": my_task},
        headers=hdr(ctx["ub"]["token"]),
    )
    assert r.status_code == 404


# ===== 5. 升版与任务归属的交互 =====

def test_new_version_inherits_task(client, ctx):
    """不传 task_id 上传同名文件时，继承上一版的任务归属（避免升版后从任务里掉出来）"""
    token = ctx["ua"]["token"]
    task_id = make_task(client, token, "继承任务")
    fname = uniq("继承归属")
    upload_doc(client, token, fname, "v1", task_id=task_id)
    v2 = upload_doc(client, token, fname, "v2")

    assert v2["version"] == 2
    assert v2["task_id"] == task_id


def test_same_name_different_task_not_chained(client, ctx):
    """不同任务下的同名表格各自独立，不被误判成升版"""
    token = ctx["ua"]["token"]
    t1 = make_task(client, token, "任务一")
    t2 = make_task(client, token, "任务二")
    fname = uniq("报名表")

    d1 = upload_doc(client, token, fname, "任务一版本", task_id=t1)
    d2 = upload_doc(client, token, fname, "任务二版本", task_id=t2)

    assert d1["version"] == 1
    assert d2["version"] == 1
    assert d2["supersedes_id"] is None
    assert d2["task_id"] == t2


def test_same_name_bumps_within_same_task(client, ctx):
    token = ctx["ua"]["token"]
    task_id = make_task(client, token, "同任务内升版")
    fname = uniq("同任务升版")

    d1 = upload_doc(client, token, fname, "v1", task_id=task_id)
    d2 = upload_doc(client, token, fname, "v2", task_id=task_id)
    assert d2["version"] == 2
    assert d2["supersedes_id"] == d1["id"]

    res = list_docs(client, token, task_id=task_id, keyword=fname)
    assert res["total"] == 1
    assert res["items"][0]["id"] == d2["id"]


def test_migrated_existing_docs_are_v1(client, ctx):
    """老库补列后，已有文档的 version 默认 1、supersedes_id 为空、仍视为最新版"""
    token = ctx["ua"]["token"]
    doc = upload_doc(client, token, uniq("老数据模拟"), "老内容")
    res = list_docs(client, token, keyword=doc["filename"])
    row = res["items"][0]
    assert row["version"] >= 1
    assert row["supersedes_id"] is None
    assert row["is_latest"] is True


# ===== 6. 检索端：只引用最新版 =====

def test_chat_search_only_uses_latest_version(client, ctx, db):
    """问答检索不能引用被替代的旧版文档

    这是版本台账最容易被忽略的一环：旧版留在库里供下载，但检索一旦命中旧版，
    回答就会给出「上一版口径」——比不知道更糟。
    """
    from tests.helpers import user_obj
    from app.services.chat_service import _search_documents

    token = ctx["ua"]["token"]
    fname = uniq("差旅补贴口径")
    v1 = upload_doc(client, token, fname, "差旅补贴标准：出差住宿每天 80 元，市内交通 20 元。")
    v2 = upload_doc(client, token, fname, "差旅补贴标准：出差住宿每天 100 元，市内交通 30 元。")

    user = user_obj(db, ctx["ua"]["user"]["username"])
    results = _search_documents(user, "差旅补贴标准是多少", db)
    doc_hits = [r for r in results if r["source_type"] == "document" and r["title"] == fname]

    assert len(doc_hits) == 1, f"同名文档应只命中最新版，实际命中 {len(doc_hits)} 条"
    assert doc_hits[0]["id"] == v2["id"]
    assert v1["id"] not in {r["id"] for r in doc_hits}
    assert "100 元" in doc_hits[0]["content"]
    assert "80 元" not in doc_hits[0]["content"]
