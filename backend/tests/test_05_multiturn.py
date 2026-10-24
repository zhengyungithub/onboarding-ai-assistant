"""多轮对话上下文回归（追问识别 / 历史加载 / 问题改写降级 / 追问检索命中）

守护点：
1. 追问识别启发式：「漏了一条」等短句或含指代/补充词的输入识别为追问，完整问题不误判；
2. 历史加载：按时间正序、只取 user/assistant、条数受 turns 限制；
3. LLM 不可用时，改写降级为「原问题 + 上一轮提问」拼接，不带任何网络依赖；
4. 降级改写后的检索问题能命中上一轮遗漏的知识条目（关键词降级路径，离线可跑）；
5. 检索 limit 已从 3 扩到 5（5 个节点的流程可全部召回）。
"""
from app.models import User, KnowledgeItem
from app.services import chat_service
from app.services import chat_message_service
from tests.helpers import user_obj

# 与上下文主题强相关的词，保证降级检索能稳定命中
TOKEN = "校招出发"


# ===== 1. 追问识别 =====

def test_followup_detection(client, ctx):
    """短句与指代/补充类特征词识别为追问；完整独立问题不误判"""
    assert chat_service._looks_like_followup("漏了一条") is True
    assert chat_service._looks_like_followup("还有吗") is True
    assert chat_service._looks_like_followup("补充一下刚才的") is True
    assert chat_service._looks_like_followup("不对，少了一个") is True
    assert chat_service._looks_like_followup("详细点") is True  # 极短输入按追问处理

    assert chat_service._looks_like_followup("请问在校招出发前我需要做哪些工作") is False
    assert chat_service._looks_like_followup("请问请假流程是什么") is False  # 独立短问题不误判
    assert chat_service._looks_like_followup("") is False


# ===== 2. 历史加载 =====

def test_recent_history_order_and_limit(client, ctx, db):
    """历史按时间正序返回、只含 user/assistant、turns 控制轮数"""
    user = db.query(User).filter(User.id == ctx["ub"]["user"]["id"]).first()
    chat_message_service.clear_messages(db, user.id)

    chat_message_service.create_message(db, user.id, "user", "在校招出发前我需要做哪些工作")
    chat_message_service.create_message(db, user.id, "assistant", "共 4 项：预订宣讲场地；完成进校申请；物色校园大使；邮寄物料")
    chat_message_service.create_message(db, user.id, "user", "漏了一条")

    hist = chat_service.get_recent_history(db, user.id, turns=4)
    assert len(hist) == 3, f"应有 3 条历史，实际 {len(hist)}"
    assert hist[0]["role"] == "user" and hist[0]["content"].startswith("在校招出发前")
    assert hist[1]["role"] == "assistant"
    assert hist[-1]["content"] == "漏了一条", "最近一条消息应在末尾"

    # turns=1 只取最近 2 条
    hist1 = chat_service.get_recent_history(db, user.id, turns=1)
    assert len(hist1) == 2
    assert hist1[-1]["content"] == "漏了一条"

    chat_message_service.clear_messages(db, user.id)


# ===== 3. 问题改写降级（LLM 不可用） =====

def test_condense_fallback_without_llm(client, ctx, db, monkeypatch):
    """没有 API Key 时改写降级为拼接：结果同时包含原问题与上一轮提问主题"""
    def _no_llm(_user):
        raise ValueError("请先在「API Key 配置」中设置 LLM API Key")

    monkeypatch.setattr(chat_service, "get_llm_for_user", _no_llm)

    user = db.query(User).filter(User.id == ctx["uc"]["user"]["id"]).first()
    chat_message_service.clear_messages(db, user.id)
    chat_message_service.create_message(db, user.id, "user", "在校招出发前我需要做哪些工作")
    chat_message_service.create_message(db, user.id, "assistant", "共 4 项：预订宣讲场地；完成进校申请；物色校园大使；邮寄物料")

    history = chat_service.get_recent_history(db, user.id)
    rewritten = chat_service.condense_question(user, "漏了一条", history)

    assert "漏了一条" in rewritten, "降级改写应保留原问题"
    assert "校招出发" in rewritten, "降级改写应拼上一轮提问作为上下文"

    # 独立问题 + 有历史 → 不需要改写（_prepare_question 只对追问改写）
    search_q, _ = chat_service._prepare_question(user, "请问请假流程是什么", db)
    assert search_q == "请问请假流程是什么"

    chat_message_service.clear_messages(db, user.id)


# ===== 4. 追问检索命中上一轮遗漏的条目 =====

def test_followup_search_hits_missed_item(client, ctx, db, monkeypatch):
    """「漏了一条」降级改写后检索，能召回上一轮回答遗漏的知识条目"""
    def _no_llm(_user):
        raise ValueError("no api key in tests")

    monkeypatch.setattr(chat_service, "get_llm_for_user", _no_llm)

    admin = db.query(User).filter(User.id == ctx["admin"]["user"]["id"]).first()
    user = db.query(User).filter(User.id == ctx["ua"]["user"]["id"]).first()

    item = KnowledgeItem(
        user_id=admin.id,
        title=f"{TOKEN}前的工作清单·联系站点面试官",
        steps=["确定站点", "联系站点面试官（线上、线下面试）"],
        system_or_material="校招出发前需要完成的工作之一：联系站点面试官",
        status="published",
        visibility="all",
        is_shared=True,
        shared_user_ids=[],
    )
    db.add(item)
    db.commit()

    chat_message_service.clear_messages(db, user.id)
    chat_message_service.create_message(db, user.id, "user", "在校招出发前我需要做哪些工作")
    chat_message_service.create_message(db, user.id, "assistant", "共 4 项：预订宣讲场地；完成进校申请；物色校园大使；邮寄物料")

    history = chat_service.get_recent_history(db, user.id)
    search_question = chat_service.condense_question(user, "漏了一条", history)
    refs = chat_service.search_relevant_items(user, search_question, db, limit=5)

    assert refs, "追问改写后应能检索到内容，而不是返回空"
    assert any("联系站点面试官" in r["title"] for r in refs), \
        f"应命中遗漏条目，实际: {[r['title'] for r in refs]}"

    # 清理
    db.delete(item)
    chat_message_service.clear_messages(db, user.id)
    db.commit()


# ===== 5. 检索条数上限 =====
# （默认 limit 已随「检索引用质量」改造升到 8，断言移至 test_06_retrieval_quality.py）
