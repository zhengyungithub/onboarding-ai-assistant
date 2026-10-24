"""多期望条目测试用例（同一问题覆盖多个知识点时，全部命中才算通过）

守护点：
1. `expected_item_ids` 可多选；`expected_item_id`（单值）保持向后兼容；
2. 一个都不给 → 400；指定了不存在的条目 → 404；
3. 负反馈转用例同样支持多选；
4. 评测判定：**全部命中才算 hit**，命中部分要如实记录 matched_count / missing_titles；
5. 老数据（expected_item_ids 为空）按单条目回落，判定行为与改造前一致。
"""
import uuid

import pytest

from tests.helpers import expect, hdr, body, make_item


def _create_case(client, token, question, **kwargs):
    return client.post(
        "/api/evaluation/cases",
        json={"question": question, **kwargs},
        headers=hdr(token),
    )


@pytest.fixture
def search_result(monkeypatch):
    """把检索结果替换成可控值：只关心判定逻辑，不依赖 Qdrant / Embedding"""
    state = {"items": []}

    def fake(user, question, db=None, limit=5, **kwargs):
        return list(state["items"])

    monkeypatch.setattr("app.services.chat_service.search_relevant_items", fake)
    return state


def _mk_items(client, token, n=2):
    tag = uuid.uuid4().hex[:6]
    return [make_item(client, token, f"多期望守护-{tag}-{i}") for i in range(n)]


def test_create_case_with_multiple_expected(client, ctx):
    token = ctx["sa_token"]
    a, b = _mk_items(client, token, 2)

    r = _create_case(client, token, "多期望-一个问题两个知识点", expected_item_ids=[a["id"], b["id"]])
    case = body(expect(r, 201, "多选期望条目创建用例"))

    assert case["expected_item_ids"] == [a["id"], b["id"]]
    assert case["expected_item_titles"] == [a["title"], b["title"]]
    # 兼容字段：取第一个，供旧前端/列表展示
    assert case["expected_item_id"] == a["id"]

    expect(client.delete(f"/api/evaluation/cases/{case['id']}", headers=hdr(token)), 204, "清理用例")


def test_create_case_backward_compatible_single(client, ctx):
    token = ctx["sa_token"]
    a = _mk_items(client, token, 1)[0]

    r = _create_case(client, token, "单期望-兼容旧字段", expected_item_id=a["id"])
    case = body(expect(r, 201, "单条目创建（旧字段）"))

    assert case["expected_item_ids"] == [a["id"]], "旧字段应被规整成列表返回"
    assert case["expected_item_titles"] == [a["title"]]

    expect(client.delete(f"/api/evaluation/cases/{case['id']}", headers=hdr(token)), 204, "清理用例")


def test_create_case_requires_expected(client, ctx):
    token = ctx["sa_token"]
    expect(_create_case(client, token, "没有期望条目"), 400, "至少一个期望条目")


def test_create_case_unknown_item_is_404(client, ctx):
    token = ctx["sa_token"]
    expect(
        _create_case(client, token, "不存在的条目", expected_item_ids=["not-exist-id"]),
        404,
        "期望条目不存在",
    )


def test_convert_feedback_with_multiple_expected(client, ctx, db):
    from tests.helpers import make_chat_pair, clean_chat_messages

    token = ctx["sa_token"]
    uid = ctx["uc"]["user"]["id"]
    clean_chat_messages(db, [uid])
    a, b = _mk_items(client, token, 2)

    msg_id = make_chat_pair(
        db, uid,
        question="多期望-校招出差前要准备什么",
        answer="（AI 回答）只答了一半",
        feedback="useless",
        references=[{"item_id": a["id"], "title": a["title"], "score": 0.8}],
    )

    r = client.post(
        "/api/evaluation/feedback-to-case",
        json={
            "message_id": msg_id,
            "question": "多期望-校招出差前要准备什么",
            "expected_item_ids": [a["id"], b["id"]],
        },
        headers=hdr(token),
    )
    case = body(expect(r, 201, "负反馈多选转用例"))
    assert case["expected_item_ids"] == [a["id"], b["id"]]
    assert case["source_message_id"] == msg_id

    expect(client.delete(f"/api/evaluation/cases/{case['id']}", headers=hdr(token)), 204, "清理用例")


def test_run_evaluation_requires_all_expected(client, ctx, search_result):
    token = ctx["sa_token"]
    a, b = _mk_items(client, token, 2)
    r = _create_case(client, token, "多期望-评测判定", expected_item_ids=[a["id"], b["id"]])
    case = body(expect(r, 201, "创建多期望用例"))

    # 只命中一条 → 不算通过
    search_result["items"] = [{"id": a["id"], "title": a["title"], "score": 0.9}]
    result = body(expect(client.post("/api/evaluation/run", headers=hdr(token)), 200, "运行评测"))
    detail = next(d for d in result["details"] if d["question"] == "多期望-评测判定")
    assert detail["hit"] is False, "少覆盖一个知识点就不算命中"
    assert detail["matched_count"] == 1 and detail["expected_count"] == 2
    assert detail["missing_titles"] == [b["title"]], "应如实报告缺哪一条"

    # 全部命中 → 通过
    search_result["items"] = [
        {"id": a["id"], "title": a["title"], "score": 0.9},
        {"id": b["id"], "title": b["title"], "score": 0.8},
    ]
    result = body(expect(client.post("/api/evaluation/run", headers=hdr(token)), 200, "再次运行评测"))
    detail = next(d for d in result["details"] if d["question"] == "多期望-评测判定")
    assert detail["hit"] is True and detail["matched_count"] == 2

    expect(client.delete(f"/api/evaluation/cases/{case['id']}", headers=hdr(token)), 204, "清理用例")


def test_run_evaluation_legacy_case_falls_back_to_single(client, ctx, db, search_result):
    """老数据：expected_item_ids 为空 → 按 expected_item_id 单条判定（行为与改造前一致）"""
    from app.models import TestCase

    token = ctx["sa_token"]
    a = _mk_items(client, token, 1)[0]
    r = _create_case(client, token, "老数据-单条回落", expected_item_id=a["id"])
    case = body(expect(r, 201, "创建用例"))

    # 模拟改造前的历史数据：多选字段为空
    row = db.query(TestCase).filter(TestCase.id == case["id"]).first()
    row.expected_item_ids = None
    row.expected_item_titles = None
    db.commit()

    search_result["items"] = [{"id": a["id"], "title": a["title"], "score": 0.9}]
    result = body(expect(client.post("/api/evaluation/run", headers=hdr(token)), 200, "运行评测"))
    detail = next(d for d in result["details"] if d["question"] == "老数据-单条回落")
    assert detail["hit"] is True
    assert detail["expected_item_ids"] == [a["id"]], "老数据应在响应里补齐为列表"

    expect(client.delete(f"/api/evaluation/cases/{case['id']}", headers=hdr(token)), 204, "清理用例")
