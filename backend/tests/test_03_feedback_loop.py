"""负反馈 → 测试用例 闭环回归

守护点：
1. 「没用」的 AI 回答会出现在待处理列表，并自动配对用户提问与当时引用来源；
2. 人工确认后可一键转为测试用例，记录来源消息（可追溯）；
3. 幂等：同一条反馈不能重复转换（409）；转换后从待处理列表消失；
4. 只认「没用」的 AI 回答（其它类型一律 400）；
5. 该闭环属后台功能，普通用户 403。
"""
from tests.helpers import (
    expect, hdr, make_chat_pair, clean_chat_messages, make_item,
)


def _pending(client, token):
    r = client.get("/api/evaluation/pending-feedback", headers=hdr(token))
    expect(r, 200, "查询待处理负反馈")
    return r.json()


def _cases(client, token):
    r = client.get("/api/evaluation/cases", headers=hdr(token))
    expect(r, 200, "查询测试用例列表")
    return r.json()


def test_pending_feedback_lists_useless_with_paired_question(client, ctx, db):
    """待处理列表：包含配对提问与引用来源"""
    uid = ctx["uc"]["user"]["id"]
    clean_chat_messages(db, [uid])

    item = make_item(client, ctx["admin"]["token"], "负反馈闭环-期望条目")
    refs = [{"item_id": item["id"], "title": item["title"], "score": 0.91}]
    msg_id = make_chat_pair(
        db, uid,
        question="负反馈闭环测试问题-报销标准是多少",
        answer="（AI 回答）这是测试回答",
        feedback="useless",
        references=refs,
    )

    data = _pending(client, ctx["admin"]["token"])
    row = next((x for x in data["items"] if x["message_id"] == msg_id), None)
    assert row is not None, "「没用」的回答应出现在待处理列表"
    assert row["question"] == "负反馈闭环测试问题-报销标准是多少", "应自动配对到用户提问"
    assert row["username"] == ctx["uc"]["user"]["username"], "应带出反馈人"
    assert row["references"] and row["references"][0]["title"] == item["title"], "应带出当时引用来源"


def test_convert_feedback_to_case_and_dedupe(client, ctx, db):
    """转用例成功 + 来源可追溯 + 全局去重"""
    uid = ctx["uc"]["user"]["id"]
    clean_chat_messages(db, [uid])
    token = ctx["admin"]["token"]

    item = make_item(client, token, "负反馈闭环-期望命中条目")
    msg_id = make_chat_pair(
        db, uid,
        question="负反馈闭环-如何申请年假",
        answer="（AI 回答）这是测试回答",
        feedback="useless",
        references=[{"item_id": item["id"], "title": item["title"], "score": 0.8}],
    )

    r = client.post(
        "/api/evaluation/feedback-to-case",
        json={"message_id": msg_id, "question": "如何申请年假", "expected_item_id": item["id"]},
        headers=hdr(token),
    )
    expect(r, 201, "把负反馈转为测试用例")
    case = r.json()
    assert case["source_message_id"] == msg_id, "用例应记录来源消息（可追溯）"
    assert case["expected_item_id"] == item["id"]
    assert case["expected_item_title"] == item["title"], "应冗余回写期望条目标题"

    # 转换后从待处理列表消失
    pending_ids = {x["message_id"] for x in _pending(client, token)["items"]}
    assert msg_id not in pending_ids, "已转换的反馈不应再出现在待处理列表"

    # 重复转换被拦截
    r = client.post(
        "/api/evaluation/feedback-to-case",
        json={"message_id": msg_id, "question": "如何申请年假", "expected_item_id": item["id"]},
        headers=hdr(token),
    )
    expect(r, 409, "重复转换同一条反馈")

    # 用例进入列表，可被正常删除
    case_ids = {c["id"] for c in _cases(client, token)["cases"]}
    assert case["id"] in case_ids, "新用例应出现在用例列表"
    expect(client.delete(f"/api/evaluation/cases/{case['id']}", headers=hdr(token)), 204, "删除用例")


def test_only_useless_answers_can_be_converted(client, ctx, db):
    """只认「没用」的 AI 回答：用户提问、有用回答、无反馈回答都转不了"""
    uid = ctx["uc"]["user"]["id"]
    clean_chat_messages(db, [uid])
    token = ctx["admin"]["token"]
    item = make_item(client, token, "负反馈闭环-类型校验条目")

    useful_id = make_chat_pair(db, uid, "有用回答的提问", "（AI 回答）好回答", feedback="useful",
                               offset_seconds=10)
    none_id = make_chat_pair(db, uid, "无反馈的提问", "（AI 回答）普通回答", feedback=None,
                             offset_seconds=20)
    user_msg_id = make_chat_pair(db, uid, "用户自己的消息", "（AI 回答）x", feedback=None,
                                 offset_seconds=30)

    from app.models import ChatMessage
    # 把最后一条的“提问”消息单独拿来当助手消息，检验 role 校验
    um = db.query(ChatMessage).filter(ChatMessage.user_id == uid,
                                      ChatMessage.role == "user").order_by(
        ChatMessage.created_at.desc()).first()
    user_msg_id = um.id
    db.query(ChatMessage).filter(ChatMessage.id == um.id).update({"feedback": "useless"})
    db.commit()
    db.expire_all()

    for label, mid in (("「有用」的回答", useful_id), ("无反馈的回答", none_id), ("用户提问消息", user_msg_id)):
        r = client.post(
            "/api/evaluation/feedback-to-case",
            json={"message_id": mid, "question": "x", "expected_item_id": item["id"]},
            headers=hdr(token),
        )
        expect(r, 400, f"转换{label}")


def test_convert_rejects_missing_targets(client, ctx, db):
    """目标消息/期望条目不存在时给出 404，不产生脏用例"""
    uid = ctx["uc"]["user"]["id"]
    clean_chat_messages(db, [uid])
    token = ctx["admin"]["token"]

    item = make_item(client, token, "负反馈闭环-404 校验条目")
    msg_id = make_chat_pair(db, uid, "404 校验提问", "（AI 回答）x", feedback="useless",
                            offset_seconds=40)

    r = client.post(
        "/api/evaluation/feedback-to-case",
        json={"message_id": "not-exist", "question": "x", "expected_item_id": item["id"]},
        headers=hdr(token),
    )
    expect(r, 404, "反馈消息不存在")

    r = client.post(
        "/api/evaluation/feedback-to-case",
        json={"message_id": msg_id, "question": "x", "expected_item_id": "not-exist"},
        headers=hdr(token),
    )
    expect(r, 404, "期望的知识条目不存在")


def test_feedback_loop_rejects_normal_user(client, ctx, db):
    """普通用户无权访问负反馈闭环（读与写都 403）"""
    token = ctx["ua"]["token"]
    expect(client.get("/api/evaluation/pending-feedback", headers=hdr(token)),
           403, "普通用户查看待处理负反馈")
    expect(
        client.post("/api/evaluation/feedback-to-case",
                    json={"message_id": "x", "question": "y", "expected_item_id": "z"},
                    headers=hdr(token)),
        403, "普通用户转换负反馈",
    )


def test_stats_counts_feedback(client, ctx, db):
    """统计卡片口径：有用/没用反馈被计入统计"""
    uid = ctx["uc"]["user"]["id"]
    clean_chat_messages(db, [uid])
    make_chat_pair(db, uid, "统计用提问", "（AI 回答）x", feedback="useless", offset_seconds=50)
    make_chat_pair(db, uid, "统计用提问2", "（AI 回答）y", feedback="useful", offset_seconds=60)

    r = client.get("/api/evaluation/stats", headers=hdr(ctx["admin"]["token"]))
    expect(r, 200, "查询评测统计")
    stats = r.json()
    assert stats["feedback_unhelpful"] >= 1, "没用反馈数应被统计"
    assert stats["feedback_helpful"] >= 1, "有用反馈数应被统计"
