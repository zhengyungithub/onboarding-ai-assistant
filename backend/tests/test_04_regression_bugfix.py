"""历史 Bug 修复的回归守护

每个用例对应一个曾经真实出过问题、已修复的点，防止后续改动把它弄坏：
1. 列表响应必须带 user_id / visibility / shared_user_ids（前端判断「只读」「共享」标签的数据源）；
2. 普通用户能读取共享条目详情（知识库「共享内容·只读」弹窗、引用来源跳转定位依赖）；
3. 知识库按标题关键词搜索能定位到目标条目（引用来源自动跳转依赖）；
4. 编辑节点顺序后，该流程节点按输入序号重排且序号连续（1..N）；
5. 流程列表返回节点信息（节点后展示所属流程模板标识依赖）；
6. 资料文档删除返回成功且从列表消失（此前前端提示「请求失败」）；
7. 台账多选批量删除可连续成功；
8. 任务可引用管理员共享的流程模板，且列表返回流程名（「任务名称（关联流程）」下拉依赖）。
"""
from tests.helpers import (
    expect, hdr, make_item, item_ids, make_flow, add_node, update_node,
    upload_doc, doc_ids, list_items,
)


# ===== 1. 列表字段完整性 =====

def test_item_list_exposes_permission_fields(client, ctx):
    """列表响应必须带权限判断所需的字段（前端不能靠猜）"""
    make_item(client, ctx["admin"]["token"], "字段完整性-全员共享", visibility="all")
    rows = list_items(client, ctx["ua"]["token"])
    assert rows, "普通用户应至少能看到共享条目"

    row = next(r for r in rows if r["visibility"] == "all")
    for field in ("id", "user_id", "visibility", "is_shared", "title", "status"):
        assert field in row, f"列表响应缺少字段 {field}"
    assert row["user_id"] == ctx["admin"]["user"]["id"], "user_id 用于前端判断“是不是我创建的”"
    assert row["is_shared"] is True
    assert "shared_user_ids" in row, "shared_user_ids 用于前端展示定向共享标签"


# ===== 2. 共享条目只读详情 =====

def test_normal_user_can_read_shared_item_detail(client, ctx):
    """普通用户读取共享条目详情：200（只读弹窗要显示内容），但不能改"""
    it = make_item(client, ctx["admin"]["token"], "只读详情-入职材料清单",
                   steps=["领取工牌", "领取电脑"], system_or_material="HR 系统", caution="需提前预约",
                   visibility="all")
    token = ctx["ua"]["token"]

    r = client.get(f"/api/items/{it['id']}", headers=hdr(token))
    expect(r, 200, "普通用户读取共享条目详情")
    detail = r.json()
    assert detail["title"] == it["title"]
    assert detail["steps"] == ["领取工牌", "领取电脑"], "步骤应完整返回（只读弹窗按条展示）"
    assert detail["system_or_material"] == "HR 系统"
    assert detail["caution"] == "需提前预约"

    expect(client.put(f"/api/items/{it['id']}", json={"title": "改"}, headers=hdr(token)),
           404, "普通用户改共享条目")


# ===== 3. 关键词定位 =====

def test_item_keyword_search_locates_target(client, ctx):
    """按标题关键词搜索能唯一定位到目标条目（引用来源跳转靠它定位并高亮）"""
    unique = "引用来源定位校验专用标题ABC"
    target = make_item(client, ctx["admin"]["token"], unique, visibility="all")

    rows = list_items(client, ctx["ua"]["token"], keyword=unique)
    assert [r["id"] for r in rows] == [target["id"]], (
        f"按标题搜索应唯一定位到目标条目，实际命中 {len(rows)} 条"
    )

    # 状态过滤 + 关键词组合（前端跳转时会把状态/流程过滤清空后重查）
    rows = list_items(client, ctx["ua"]["token"], keyword=unique, status="draft")
    assert [r["id"] for r in rows] == [target["id"]]


# ===== 4. 节点顺序重排 =====

def test_flow_node_order_is_resequenced(client, ctx):
    """编辑节点顺序：输入序号即排到该位置，其余节点顺延，序号连续 1..N"""
    flow = make_flow(client, ctx["admin"]["token"], "节点顺序回归流程")
    a = add_node(client, ctx["admin"]["token"], flow["id"], "第一站", 1)
    b = add_node(client, ctx["admin"]["token"], flow["id"], "第二站", 2)
    c = add_node(client, ctx["admin"]["token"], flow["id"], "第三站", 3)

    # 把「第三站」调到第 1 位
    update_node(client, ctx["admin"]["token"], flow["id"], c["id"], order=1)

    detail = client.get(f"/api/flows/{flow['id']}", headers=hdr(ctx["admin"]["token"])).json()
    nodes = sorted(detail["nodes"], key=lambda n: n["order"])
    assert [n["name"] for n in nodes] == ["第三站", "第一站", "第二站"], (
        f"顺序应为 第三站→第一站→第二站，实际 {[n['name'] for n in nodes]}"
    )
    assert [n["order"] for n in nodes] == [1, 2, 3], "序号必须连续为 1..N"

    # 越界序号自动收敛（小于 1 视为第 1 位）
    update_node(client, ctx["admin"]["token"], flow["id"], b["id"], order=0)
    detail = client.get(f"/api/flows/{flow['id']}", headers=hdr(ctx["admin"]["token"])).json()
    nodes = sorted(detail["nodes"], key=lambda n: n["order"])
    assert [n["order"] for n in nodes] == [1, 2, 3], "越界序号应收敛后仍保持连续"
    assert nodes[0]["name"] == "第二站", "序号 <1 应视为排到第 1 位"
    assert a["id"] in {n["id"] for n in nodes}, "节点不应丢失"


# ===== 5. 流程节点带所属模板信息 =====

def test_flow_list_includes_nodes(client, ctx):
    """流程列表返回节点（前端据此在每个节点后标注所属流程模板）"""
    flow = make_flow(client, ctx["admin"]["token"], "节点标识回归流程")
    add_node(client, ctx["admin"]["token"], flow["id"], "宣讲准备", 1,
             related_item_ids=[])

    row = next(f for f in client.get("/api/flows", headers=hdr(ctx["ua"]["token"])).json()["flows"]
               if f["id"] == flow["id"])
    assert len(row["nodes"]) == 1, "流程应返回其节点"
    node = row["nodes"][0]
    for field in ("id", "name", "order", "related_item_ids"):
        assert field in node, f"节点缺少字段 {field}"


# ===== 6. 资料文档删除 =====

def test_document_delete_succeeds_and_disappears(client, ctx):
    """删除资料文档：接口返回成功，且从台账消失（此前前端提示「请求失败」）"""
    doc = upload_doc(client, ctx["admin"]["token"], "待删除资料.txt", "待删除的测试内容。")
    token = ctx["admin"]["token"]
    assert doc["id"] in doc_ids(client, token)

    r = client.delete(f"/api/source-documents/{doc['id']}", headers=hdr(token))
    expect(r, 200, "删除资料文档")
    assert doc["id"] not in doc_ids(client, token), "删除后不应再出现在台账"

    expect(client.delete(f"/api/source-documents/{doc['id']}", headers=hdr(token)),
           404, "重复删除应提示不存在")


def test_normal_user_cannot_delete_others_document(client, ctx):
    """普通用户不能删除管理员的文档"""
    doc = upload_doc(client, ctx["admin"]["token"], "他人资料不可删.txt", "内容。")
    expect(client.delete(f"/api/source-documents/{doc['id']}", headers=hdr(ctx["ua"]["token"])),
           404, "普通用户删除他人文档")


# ===== 7. 批量删除 =====

def test_batch_delete_items(client, ctx):
    """台账多选批量删除：连续删除均成功，列表清零"""
    token = ctx["admin"]["token"]
    created = [make_item(client, token, f"批量删除校验-{i}", visibility="private") for i in range(3)]
    ids = {c["id"] for c in created}
    assert ids <= item_ids(client, token)

    for i in sorted(ids):
        expect(client.delete(f"/api/items/{i}", headers=hdr(token)), 204, f"删除条目 {i}")

    left = item_ids(client, token)
    assert not (ids & left), f"批量删除后不应残留：{ids & left}"


# ===== 8. 任务引用共享流程 =====

def test_task_references_shared_flow_and_exposes_flow_name(client, ctx):
    """普通用户可基于管理员共享的流程建任务，列表返回流程名（下拉命名依赖）"""
    flow = make_flow(client, ctx["admin"]["token"], "共享流程-校招接洽")
    add_node(client, ctx["admin"]["token"], flow["id"], "确认拜访路线", 1)
    add_node(client, ctx["admin"]["token"], flow["id"], "联系面试官", 2)

    token = ctx["ua"]["token"]
    r = client.post("/api/tasks", json={"flow_ids": [flow["id"]], "name": "2027届校招"},
                    headers=hdr(token))
    expect(r, 200, "普通用户基于共享流程创建任务")

    rows = client.get("/api/tasks", headers=hdr(token))
    expect(rows, 200, "查询任务列表")
    tasks = rows.json()["tasks"]
    task = next(t for t in tasks if t["id"] == r.json()["id"])
    assert task["name"] == "2027届校招"
    assert task["flow_names"] == ["共享流程-校招接洽"], (
        f"任务应带出关联流程名，实际 {task.get('flow_names')}"
    )
    assert task["flow_ids"] == [flow["id"]]


def test_tasks_are_isolated_between_users(client, ctx):
    """任务按用户隔离：别人的任务不可见、不可读"""
    flow = make_flow(client, ctx["admin"]["token"], "隔离校验流程")
    add_node(client, ctx["admin"]["token"], flow["id"], "节点一", 1)

    r = client.post("/api/tasks", json={"flow_ids": [flow["id"]], "name": "甲的任务"},
                    headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "甲创建任务")
    task_id = r.json()["id"]

    ub_tasks = client.get("/api/tasks", headers=hdr(ctx["ub"]["token"])).json()["tasks"]
    assert task_id not in {t["id"] for t in ub_tasks}, "乙不应看到甲的任务"
    expect(client.get(f"/api/tasks/{task_id}", headers=hdr(ctx["ub"]["token"])),
           404, "乙读取甲的任务详情")
    expect(client.get(f"/api/tasks/{task_id}", headers=hdr(ctx["ua"]["token"])),
           200, "甲读取自己的任务详情")
