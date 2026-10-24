"""检索引用质量回归（流程节点补全 / 近重复去重 / 反馈原因）

守护点：
1. 问题命中流程名时，该流程的全部节点都会出现在引用来源中
   （流程节点不入向量库，Qdrant 相似度检索必然漏召回——历史缺陷）；
2. 流程节点与其关联知识条目标题近重复（「流程名 - 条目名」vs「条目名」）时只保留一条；
3. 检索默认上限为 8；
4. 点「没用」可附带原因并持久化，切换/取消反馈时清空；
5. 待处理负反馈列表返回原因字段。
"""
from app.models import User, ChatMessage
from app.services import chat_service
from app.services import chat_message_service
from tests.helpers import (
    expect, hdr, make_item, make_flow, add_node, list_flows,
    publish_directly, user_obj,
)

TOKEN = "校招出发"


# ===== 1. 流程节点全量补全 =====

def test_flow_name_hit_returns_all_nodes(client, ctx, db):
    """问题包含流程名 → 引用来源包含该流程全部 5 个节点"""
    admin_token = ctx["admin"]["token"]
    flow = make_flow(client, admin_token, f"筹备/{TOKEN}前")
    names = ["预订宣讲场地", "完成进校申请", "物色校园大使", "邮寄物料", "联系站点面试官"]
    for i, n in enumerate(names, 1):
        add_node(client, admin_token, flow["id"], n, i, description=f"{n}的具体操作说明")

    user = user_obj(db, ctx["ua"]["user"]["username"])
    refs = chat_service.search_relevant_items(user, f"{TOKEN}前需要做哪些工作内容", db)

    titles = [r["title"] for r in refs]
    for n in names:
        assert any(n in t for t in titles), f"节点「{n}」应被引用，实际: {titles}"

    # 清理
    expect(client.delete(f"/api/flows/{flow['id']}", headers=hdr(admin_token)), 204, "清理流程")


def test_flow_augmentation_not_leak_to_others(client, ctx, db):
    """普通用户的私有流程不会被其他普通用户的检索补全"""
    ua_token = ctx["ua"]["token"]
    flow = make_flow(client, ua_token, f"甲的私有流程{TOKEN}手册")
    add_node(client, ua_token, flow["id"], "内部步骤一", 1, description="甲的私有内容")

    ub = user_obj(db, ctx["ub"]["user"]["username"])
    refs = chat_service.search_relevant_items(ub, f"{TOKEN}手册怎么用", db)
    assert not any("甲的私有流程" in r["title"] for r in refs), \
        "乙不应通过检索补全看到甲的私有流程"

    expect(client.delete(f"/api/flows/{flow['id']}", headers=hdr(ua_token)), 204, "清理流程")


# ===== 2. 近重复去重 =====

def test_near_duplicate_titles_deduped(client, ctx, db):
    """流程节点「流程名 - 条目名」与其关联知识条目「条目名」只保留一条"""
    admin_token = ctx["admin"]["token"]
    item = make_item(client, admin_token,
                     f"邮寄物料至酒店学校与大使确认收件情况以及物料数量{TOKEN}SOP",
                     status="published")
    publish_directly(db, item["id"])

    flow = make_flow(client, admin_token, f"筹备/{TOKEN}前")
    add_node(client, admin_token, flow["id"], "邮寄物料", 1,
             description="打包与提单",
             related_item_ids=[item["id"]])

    user = user_obj(db, ctx["ua"]["user"]["username"])
    refs = chat_service.search_relevant_items(user, f"{TOKEN}前需要做哪些工作", db)

    dup_titles = [r["title"] for r in refs
                  if "邮寄物料至酒店学校" in r["title"]]
    assert len(dup_titles) <= 1, f"近重复标题应只保留一条，实际: {dup_titles}"

    expect(client.delete(f"/api/flows/{flow['id']}", headers=hdr(admin_token)), 204, "清理流程")
    expect(client.delete(f"/api/items/{item['id']}", headers=hdr(admin_token)), 204, "清理条目")


def test_dup_title_helper_rules():
    """去重启发式自身的边界：短标题不比较，后缀包含才判重"""
    f = chat_service._is_dup_title
    assert f("筹备/出发前 - 邮寄物料至酒店学校SOP", "邮寄物料至酒店学校SOP") is True
    assert f("预订宣讲场地", "预订宣讲场地") is True
    assert f("短标题", "短标题") is True           # 完全相等恒判重（不受长度门槛限制）
    assert f("短标题A", "短标题B") is False        # 不同短标题不比较
    assert f("预订宣讲场地的全部流程说明", "完成进校申请") is False


def test_search_default_limit_is_eight(client, ctx):
    """检索默认上限已扩到 8（5 节点流程 + 少量文档）"""
    import inspect
    sig = inspect.signature(chat_service.search_relevant_items)
    assert sig.parameters["limit"].default == 8


# ===== 3. 「没用」反馈原因 =====

def test_useless_feedback_with_reason(client, ctx, db):
    """点「没用」可附带原因；响应与数据库均持久化原因"""
    uid = ctx["ua"]["user"]["id"]
    chat_message_service.create_message(db, uid, "assistant", "一条需要被吐槽的回答")
    _, msgs = chat_message_service.get_messages(db, uid, limit=10)
    mid = msgs[-1].id

    r = client.patch(f"/api/chat/messages/{mid}/feedback",
                     json={"feedback": "useless", "reason": "漏了联系站点面试官这条"},
                     headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "提交带原因的没用反馈")
    assert r.json()["feedback"] == "useless"
    assert r.json()["feedback_reason"] == "漏了联系站点面试官这条"

    # 待处理负反馈应包含原因
    r2 = client.get("/api/evaluation/pending-feedback", headers=hdr(ctx["admin"]["token"]))
    expect(r2, 200, "查询待处理负反馈")
    row = next((x for x in r2.json()["items"] if x["message_id"] == mid), None)
    assert row is not None, "该负反馈应出现在待处理列表"
    assert row["reason"] == "漏了联系站点面试官这条"

    # 改为「有用」→ 原因清空，且不再出现在待处理列表
    r3 = client.patch(f"/api/chat/messages/{mid}/feedback",
                      json={"feedback": "useful"}, headers=hdr(ctx["ua"]["token"]))
    expect(r3, 200, "改为有用反馈")
    assert r3.json()["feedback_reason"] is None
    r4 = client.get("/api/evaluation/pending-feedback", headers=hdr(ctx["admin"]["token"]))
    assert not any(x["message_id"] == mid for x in r4.json()["items"]), "改回有用后不应在负反馈列表"

    # 取消反馈 → 原因同样为空
    r5 = client.patch(f"/api/chat/messages/{mid}/feedback",
                      json={"feedback": "useless", "reason": "临时踩一下"},
                      headers=hdr(ctx["ua"]["token"]))
    expect(r5, 200, "再次点没用")
    r6 = client.patch(f"/api/chat/messages/{mid}/feedback",
                      json={"feedback": "useless", "reason": ""},
                      headers=hdr(ctx["ua"]["token"]))
    expect(r6, 200, "重复提交覆盖原因")
    assert r6.json()["feedback_reason"] is None, "空原因应存为 None"

    chat_message_service.clear_messages(db, uid)
