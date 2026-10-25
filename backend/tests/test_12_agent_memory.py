"""三层记忆（改造 4）：短期 → 三层记忆

守护点（这些是"改完不劣化"的验收线）：

1. **关闭即归零**：`AGENT_MEMORY_ENABLED=false` 时短期轮数严格回到 4 轮、
   组装出的消息列表与改造前**逐字一致**、不产生任何记忆数据（三张表全空）。
   这是"不影响其他功能模块"的硬证明，也是灰度回滚的依据。
2. L1 短期窗口：开启时默认 6 轮，且**永远不会短于**改造前的 4 轮。
3. L1.5 会话摘要：低于阈值完全不触发（正常会话零成本）；
   到点生成、按间隔增量刷新、失败不推进水位线、窗口内原文不被摘要吃掉。
4. L1.6 检索式历史：embedding/向量库不可用一律静默返回空（记忆是加分项）；
   召回要按 `min_score` 过滤、要排除"已经在原文窗口里"的消息、要在 `top_k` 截断。
5. L2 用户偏好：纯规则抽取（不调模型）；**只有流程名/系统名能参与查询扩展**，
   主题词不行（太泛，会把检索带偏）；扩展词排在问题关键词之后且最多补 2 个
   （绝不能把用户这次真正问的词挤出关键词表）。
6. L3 未答问题：答不上来时自动采集，答上了不采集，超出上限清理最早的。
7. 全程只读免疫：任何畸形输入（None/int/异常客户端）都不抛异常。

全程离线：LLM / Embedding / Qdrant 全部用测试替身，不访问任何网络。
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from tests.helpers import body, expect, hdr, user_obj

from app.agent import memory
from app.core.config import settings
from app.services import chat_service


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class FakeEmbedding:
    """固定向量（维度 4），避免任何 Embedding API 调用"""

    def __init__(self, vector=None):
        self.vector = vector if vector is not None else [0.1, 0.2, 0.3, 0.4]

    def __call__(self, text, user):
        return list(self.vector)


class _Point:
    def __init__(self, pid, score, payload):
        self.id = pid
        self.score = score
        self.payload = payload


class _Resp:
    def __init__(self, points):
        self.points = points


class _Collection:
    def __init__(self, name):
        self.name = name


class _Collections:
    def __init__(self, names):
        self.collections = [_Collection(n) for n in names]


class FakeQdrant:
    """假向量库：可控集合存在性、可控召回结果，并记录写入与删除"""

    def __init__(self, collections=("chat_history",), points=None):
        self.names = list(collections)
        self.points = list(points or [])
        self.upserted = []
        self.created = []
        self.deleted = []

    def get_collections(self):
        return _Collections(self.names)

    def create_collection(self, collection_name, **kwargs):
        self.names.append(collection_name)
        self.created.append(collection_name)

    def create_payload_index(self, **kwargs):
        return None

    def upsert(self, collection_name, points):
        self.upserted.extend(points)

    def delete(self, collection_name, points_selector):
        self.deleted.append(points_selector)

    def query_points(self, **kwargs):
        return _Resp(self.points)


class FakeLLM:
    """假模型：返回编排好的摘要文本"""

    def __init__(self, reply="摘要：用户关注校招出差，已确认标准为每天 300 元。", boom=False):
        self.reply = reply
        self.boom = boom
        self.model_name = "fake-summary-model"
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        if self.boom:
            raise RuntimeError("模型不可用")
        return AIMessage(content=self.reply)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

MEMORY_MODELS = ("ChatMessage", "ConversationSummary", "UserMemory", "UnansweredQuestion")


def _wipe(db, user_id: str) -> None:
    """清空某用户的对话与三层记忆（保证用例间互不污染）"""
    from app.models import ChatMessage, ConversationSummary, UnansweredQuestion, UserMemory

    db.query(ChatMessage).filter(ChatMessage.user_id == user_id).delete(synchronize_session=False)
    db.query(ConversationSummary).filter(ConversationSummary.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(UserMemory).filter(UserMemory.user_id == user_id).delete(synchronize_session=False)
    db.query(UnansweredQuestion).filter(UnansweredQuestion.user_id == user_id).delete(
        synchronize_session=False
    )
    db.commit()


@pytest.fixture
def mem_user(db, ctx):
    """专属测试用户（每个用例前后清空对话与记忆）"""
    user = user_obj(db, ctx["ua"]["user"]["username"])
    _wipe(db, user.id)
    try:
        yield user
    finally:
        db.expire_all()
        _wipe(db, user.id)


def _seed_turns(db, user_id: str, turns: int, tag: str = "seed") -> None:
    """写入 N 轮对话（user/assistant 各一条）"""
    from app.models import ChatMessage

    for i in range(turns):
        db.add(ChatMessage(user_id=user_id, role="user", content=f"{tag}-问题-{i} 校招出差准备"))
        db.add(ChatMessage(user_id=user_id, role="assistant", content=f"{tag}-回答-{i} 需提前提交审批"))
    db.commit()


# ===========================================================================
# G1 总开关与口径
# ===========================================================================

def test_memory_enabled_by_default():
    assert memory.enabled() is True
    assert memory.short_turns() == settings.AGENT_MEMORY_SHORT_TURNS


def test_short_turns_is_six_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SHORT_TURNS", 6)
    assert memory.short_turns() == 6


def test_short_turns_falls_back_to_legacy_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SHORT_TURNS", 6)
    assert memory.short_turns() == memory.SHORT_TURNS_LEGACY == 4


def test_short_turns_never_below_legacy(monkeypatch):
    """配置成比 4 还小也不接受——更短的窗口不是升级，是退化"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SHORT_TURNS", 2)
    assert memory.short_turns() == 4


def test_short_turns_survives_bad_config(monkeypatch):
    """配置写成非数字时退回改造前的 4 轮，不抛异常"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SHORT_TURNS", "六轮")
    assert memory.short_turns() == 4


def test_memory_block_empty_when_disabled(monkeypatch, db, mem_user):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    _seed_turns(db, mem_user.id, 4)
    assert memory.memory_block(db, mem_user, "校招出差准备") == ""


def test_get_recent_history_uses_memory_turns(monkeypatch, db, mem_user):
    """开启记忆时短期窗口拉长到 6 轮（12 条消息）"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SHORT_TURNS", 6)
    _seed_turns(db, mem_user.id, 10)
    history = chat_service.get_recent_history(db, mem_user.id)
    assert len(history) == 12


def test_get_recent_history_legacy_when_disabled(monkeypatch, db, mem_user):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    _seed_turns(db, mem_user.id, 10)
    history = chat_service.get_recent_history(db, mem_user.id)
    assert len(history) == 8  # 4 轮 × 2，与改造前一致


def test_get_recent_history_explicit_turns_wins(monkeypatch, db, mem_user):
    """显式传 turns 时不受记忆配置影响（保留原有调用口径）"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SHORT_TURNS", 6)
    _seed_turns(db, mem_user.id, 10)
    assert len(chat_service.get_recent_history(db, mem_user.id, turns=2)) == 4


# ===========================================================================
# G2 关闭时与改造前逐字一致（最关键的回滚守护）
# ===========================================================================

def test_disabled_answer_messages_identical_to_legacy(monkeypatch):
    """关闭记忆时组装出的消息列表必须与改造前的实现逐字一致"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    history = [{"role": "user", "content": "上一轮问题"}, {"role": "assistant", "content": "上一轮回答"}]
    context = "【参考1·知识条目】校招出差需提交审批单"
    question = "校招出差前要做什么"

    actual = chat_service._build_answer_messages(question, context, history)
    legacy = [SystemMessage(content=chat_service.SYSTEM_PROMPT)]
    for h in history:
        legacy.append(HumanMessage(content=h["content"]) if h["role"] == "user" else AIMessage(content=h["content"]))
    legacy.append(HumanMessage(content=f"知识库内容：\n{context}\n\n用户问题：{question}"))

    assert [m.content for m in actual] == [m.content for m in legacy]


def test_disabled_no_memory_block_injected(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    msgs = chat_service._build_answer_messages("问题", "上下文", [], memory_block="")
    assert "长期记忆" not in msgs[-1].content


def test_enabled_memory_block_is_injected(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    msgs = chat_service._build_answer_messages("问题", "上下文", [], memory_block="【长期记忆·仅供参考】x")
    assert "长期记忆" in msgs[-1].content
    # 注入在 HumanMessage 头部，System 提示词不受污染
    assert msgs[0].content == chat_service.SYSTEM_PROMPT


def test_recall_terms_empty_when_disabled(monkeypatch, db, mem_user):
    from app.models import UserMemory

    db.add(UserMemory(user_id=mem_user.id, data={"flows": [{"term": "校园招聘", "count": 9}]}))
    db.commit()
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    assert chat_service._recall_terms(db, mem_user) == []


def test_after_answer_noop_when_disabled(monkeypatch, db, mem_user):
    """关闭时一轮问答结束不留任何记忆数据"""
    from app.models import ConversationSummary, UnansweredQuestion, UserMemory

    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    _seed_turns(db, mem_user.id, 20)
    memory.after_answer(db, mem_user, "一个知识库答不上来的问题", answered=False)
    db.expire_all()
    assert db.query(ConversationSummary).filter(ConversationSummary.user_id == mem_user.id).count() == 0
    assert db.query(UserMemory).filter(UserMemory.user_id == mem_user.id).count() == 0
    assert db.query(UnansweredQuestion).filter(UnansweredQuestion.user_id == mem_user.id).count() == 0


def test_extract_keywords_unchanged_without_extra_terms(monkeypatch):
    """不传扩展词时关键词提取结果与改造前一致"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    assert chat_service._extract_keywords("校招出差前需要准备什么") == chat_service._extract_keywords(
        "校招出差前需要准备什么", extra_terms=None
    )


# ===========================================================================
# G3 L1.5 会话摘要
# ===========================================================================

def test_summary_not_triggered_below_threshold(monkeypatch, db, mem_user):
    """正常会话（20 条消息 = 10 轮 < 12 轮）完全不触发摘要，零成本"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    _seed_turns(db, mem_user.id, 10)
    assert memory.refresh_summary(db, mem_user) is None
    assert memory.summary_text(db, mem_user.id) == ""


def test_summary_generated_at_threshold(monkeypatch, db, mem_user):
    from app.models import ConversationSummary

    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SUMMARY_TRIGGER_TURNS", 12)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SUMMARY_INTERVAL_TURNS", 6)
    fake = FakeLLM()
    monkeypatch.setattr("app.services.llm_service.get_llm_for_user", lambda user: fake)

    _seed_turns(db, mem_user.id, 14)  # 28 条 > 24 条阈值
    row = memory.refresh_summary(db, mem_user)
    assert row is not None
    assert row.summary == fake.reply
    # 只摘要窗口外的部分：28 - 6轮×2 = 16
    assert row.covered_message_count == 16
    assert db.query(ConversationSummary).filter(ConversationSummary.user_id == mem_user.id).count() == 1
    assert fake.calls == 1


def test_summary_not_refreshed_within_interval(monkeypatch, db, mem_user):
    """距上次摘要未新增足够消息时不重复调模型（省 token 的关键）"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SUMMARY_INTERVAL_TURNS", 6)
    fake = FakeLLM()
    monkeypatch.setattr("app.services.llm_service.get_llm_for_user", lambda user: fake)

    _seed_turns(db, mem_user.id, 14)
    memory.refresh_summary(db, mem_user)
    assert fake.calls == 1
    _seed_turns(db, mem_user.id, 2, tag="more")  # 只多 4 条，未达 12 条间隔
    memory.refresh_summary(db, mem_user)
    assert fake.calls == 1


def test_summary_incremental_update(monkeypatch, db, mem_user):
    """新增足够消息后增量刷新，水位线推进"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SUMMARY_INTERVAL_TURNS", 6)
    fake = FakeLLM()
    monkeypatch.setattr("app.services.llm_service.get_llm_for_user", lambda user: fake)

    _seed_turns(db, mem_user.id, 14)
    first = memory.refresh_summary(db, mem_user)
    first_watermark = int(first.covered_message_count)  # 同一 ORM 实例会被 refresh 改写，先取值
    _seed_turns(db, mem_user.id, 8, tag="more")
    second = memory.refresh_summary(db, mem_user)
    assert fake.calls == 2
    assert int(second.covered_message_count) > first_watermark


def test_summary_failure_does_not_advance_watermark(monkeypatch, db, mem_user):
    """模型调用失败时不动水位线（下次有新增消息还能再试），且不抛异常"""
    from app.models import ConversationSummary

    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(
        "app.services.llm_service.get_llm_for_user", lambda user: FakeLLM(boom=True)
    )
    _seed_turns(db, mem_user.id, 14)
    assert memory.refresh_summary(db, mem_user) is None
    assert db.query(ConversationSummary).filter(ConversationSummary.user_id == mem_user.id).count() == 0


def test_summary_empty_reply_keeps_old_summary(monkeypatch, db, mem_user):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr("app.services.llm_service.get_llm_for_user", lambda user: FakeLLM(reply="   "))
    _seed_turns(db, mem_user.id, 14)
    assert memory.refresh_summary(db, mem_user) is None


def test_summary_force_ignores_interval(monkeypatch, db, mem_user):
    """force=True（管理员重建）时忽略间隔直接生成"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SUMMARY_INTERVAL_TURNS", 99)
    fake = FakeLLM()
    monkeypatch.setattr("app.services.llm_service.get_llm_for_user", lambda user: fake)
    _seed_turns(db, mem_user.id, 14)
    assert memory.refresh_summary(db, mem_user, force=True) is not None
    assert fake.calls == 1


def test_summary_max_chars_truncated(monkeypatch, db, mem_user):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SUMMARY_MAX_CHARS", 20)
    monkeypatch.setattr(
        "app.services.llm_service.get_llm_for_user", lambda user: FakeLLM(reply="摘" * 200)
    )
    _seed_turns(db, mem_user.id, 14)
    row = memory.refresh_summary(db, mem_user)
    assert len(row.summary) <= 21  # 20 + 省略号


def test_summary_text_empty_when_disabled(monkeypatch, db, mem_user):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    _seed_turns(db, mem_user.id, 20)
    assert memory.summary_text(db, mem_user.id) == ""
    assert memory.refresh_summary(db, mem_user) is None


# ===========================================================================
# G4 L1.6 检索式历史
# ===========================================================================

def test_recall_empty_when_embedding_fails(monkeypatch, db, mem_user):
    def _boom(text, user):
        raise RuntimeError("未配置 Embedding API Key")

    monkeypatch.setattr("app.services.embedding_service.embed_dense", _boom)
    assert memory.recall(db, mem_user, "校招出差准备") == []


def test_recall_empty_when_collection_missing(monkeypatch, db, mem_user):
    monkeypatch.setattr("app.services.embedding_service.embed_dense", FakeEmbedding())
    monkeypatch.setattr(
        "app.services.qdrant_service.get_qdrant_client", lambda: FakeQdrant(collections=())
    )
    assert memory.recall(db, mem_user, "校招出差准备") == []


def test_recall_filters_by_min_score(monkeypatch, db, mem_user):
    points = [_Point("m-1", 0.9, {"message_id": "m-1", "role": "user", "content": "校招出差的准备清单"})]
    monkeypatch.setattr(settings, "AGENT_MEMORY_RECALL_MIN_SCORE", 0.95)
    monkeypatch.setattr("app.services.embedding_service.embed_dense", FakeEmbedding())
    fake = FakeQdrant(points=points)
    monkeypatch.setattr("app.services.qdrant_service.get_qdrant_client", lambda: fake)
    # min_score 由 query_points 的 score_threshold 参数下推给向量库；
    # 这里断言阈值确实被传下去了（真实过滤由 Qdrant 执行）
    captured = {}
    original = fake.query_points

    def _capture(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    fake.query_points = _capture
    memory.recall(db, mem_user, "校招出差准备")
    assert captured.get("score_threshold") == 0.95


def test_recall_excludes_recent_window(monkeypatch, db, mem_user):
    """排除最近窗口内的消息（它们本来就会以原文进 prompt）"""
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_MEMORY_SHORT_TURNS", 6)
    _seed_turns(db, mem_user.id, 5)
    from app.models import ChatMessage

    recent_ids = [
        r[0]
        for r in db.query(ChatMessage.id)
        .filter(ChatMessage.user_id == mem_user.id)
        .order_by(ChatMessage.created_at.desc())
        .limit(12)
        .all()
    ]
    points = [
        _Point(mid, 0.9, {"message_id": mid, "role": "user", "content": "历史片段内容足够长"})
        for mid in recent_ids
    ]
    monkeypatch.setattr("app.services.embedding_service.embed_dense", FakeEmbedding())
    monkeypatch.setattr(
        "app.services.qdrant_service.get_qdrant_client", lambda: FakeQdrant(points=points)
    )
    assert memory.recall(db, mem_user, "校招出差准备") == []


def test_recall_truncates_to_top_k(monkeypatch, db, mem_user):
    points = [
        _Point(f"m-{i}", 0.9, {"message_id": f"m-{i}", "role": "user", "content": "历史片段内容足够长"})
        for i in range(8)
    ]
    monkeypatch.setattr(settings, "AGENT_MEMORY_RECALL_TOP_K", 3)
    monkeypatch.setattr("app.services.embedding_service.embed_dense", FakeEmbedding())
    monkeypatch.setattr(
        "app.services.qdrant_service.get_qdrant_client", lambda: FakeQdrant(points=points)
    )
    assert len(memory.recall(db, mem_user, "校招出差准备", exclude_message_ids=["none"])) == 3


def test_recall_skips_too_short_snippets(monkeypatch, db, mem_user):
    points = [_Point("m-1", 0.9, {"message_id": "m-1", "role": "user", "content": "嗯"})]
    monkeypatch.setattr("app.services.embedding_service.embed_dense", FakeEmbedding())
    monkeypatch.setattr(
        "app.services.qdrant_service.get_qdrant_client", lambda: FakeQdrant(points=points)
    )
    assert memory.recall(db, mem_user, "校招出差准备", exclude_message_ids=["none"]) == []


def test_recall_empty_when_disabled(monkeypatch, db, mem_user):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    assert memory.recall(db, mem_user, "校招出差准备") == []


def test_index_messages_writes_points(monkeypatch, db, mem_user):
    """索引写入独立 collection，payload 带 user_id 与 message_id"""
    from app.models import ChatMessage

    monkeypatch.setattr("app.services.embedding_service.embed_dense", FakeEmbedding())
    fake = FakeQdrant(collections=())  # 集合不存在 → 走创建分支
    monkeypatch.setattr("app.services.qdrant_service.get_qdrant_client", lambda: fake)

    db.add(ChatMessage(user_id=mem_user.id, role="user", content="校招出差前需要准备哪些材料"))
    db.commit()
    msg_id = db.query(ChatMessage.id).filter(ChatMessage.user_id == mem_user.id).first()[0]

    assert memory.index_messages(db, mem_user, [msg_id]) == 1
    assert memory.CHAT_HISTORY_COLLECTION in fake.created
    assert fake.upserted[0].payload["user_id"] == mem_user.id
    assert fake.upserted[0].payload["message_id"] == msg_id


def test_index_messages_skips_short_content(monkeypatch, db, mem_user):
    from app.models import ChatMessage

    monkeypatch.setattr("app.services.embedding_service.embed_dense", FakeEmbedding())
    fake = FakeQdrant()
    monkeypatch.setattr("app.services.qdrant_service.get_qdrant_client", lambda: fake)
    db.add(ChatMessage(user_id=mem_user.id, role="user", content="嗯"))
    db.commit()
    msg_id = db.query(ChatMessage.id).filter(ChatMessage.user_id == mem_user.id).first()[0]
    assert memory.index_messages(db, mem_user, [msg_id]) == 0
    assert fake.upserted == []


def test_index_messages_ignores_other_users_messages(monkeypatch, db, mem_user):
    """越权保护：只能索引属于该用户的消息"""
    from app.models import ChatMessage

    monkeypatch.setattr("app.services.embedding_service.embed_dense", FakeEmbedding())
    fake = FakeQdrant()
    monkeypatch.setattr("app.services.qdrant_service.get_qdrant_client", lambda: fake)
    db.add(ChatMessage(user_id="someone-else", role="user", content="别人的很长的消息内容"))
    db.commit()
    other_id = db.query(ChatMessage.id).filter(ChatMessage.user_id == "someone-else").first()[0]
    assert memory.index_messages(db, mem_user, [other_id]) == 0
    db.query(ChatMessage).filter(ChatMessage.user_id == "someone-else").delete(synchronize_session=False)
    db.commit()


def test_index_messages_disabled_returns_zero(monkeypatch, db, mem_user):
    from app.models import ChatMessage

    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    db.add(ChatMessage(user_id=mem_user.id, role="user", content="校招出差前需要准备哪些材料"))
    db.commit()
    msg_id = db.query(ChatMessage.id).filter(ChatMessage.user_id == mem_user.id).first()[0]
    assert memory.index_messages(db, mem_user, [msg_id]) == 0


# ===========================================================================
# G5 L2 用户偏好（规则抽取）
# ===========================================================================

def test_topic_terms_filters_stopwords_and_short():
    terms = memory.topic_terms("请问校招出差前需要准备什么材料")
    assert "校招" in terms or "出差" in terms
    for bad in ("请问", "什么", "需要", "怎么", "的"):
        assert bad not in terms
    assert all(len(t) >= 2 for t in terms)


def test_topic_terms_handles_empty():
    assert memory.topic_terms("") == []
    assert memory.topic_terms(None) == []


def test_system_terms_matches_pattern(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MEMORY_SYSTEM_TERMS", "")
    terms = memory._system_terms("在格慧招系统里怎么提交校招需求")
    assert any("系统" in t for t in terms)


def test_system_terms_respects_whitelist(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MEMORY_SYSTEM_TERMS", "格慧招,OERP")
    terms = memory._system_terms("格慧招里怎么导出报表")
    assert "格慧招" in terms


def test_flow_terms_matches_db_flow(db, mem_user):
    """流程名是最强的业务实体信号，能直接命中数据库里的流程"""
    from app.models import Flow

    flow = Flow(name="校园招聘流程", user_id=mem_user.id, visibility="private")
    db.add(flow)
    db.commit()
    try:
        assert memory._flow_terms(db, "校园招聘流程的下一步是什么") == ["校园招聘流程"]
        assert memory._flow_terms(db, "完全无关的提问") == []
    finally:
        db.delete(flow)
        db.commit()


def test_observe_accumulates_counts(db, mem_user):
    memory.observe(db, mem_user.id, "校招出差前需要准备什么")
    memory.observe(db, mem_user.id, "校招出差的材料清单")
    data = memory.preference_snapshot(db, mem_user.id)
    assert data["topics"], "应至少抽到主题词"
    top = max(data["topics"], key=lambda x: x["count"])
    assert int(top["count"]) >= 2


def test_observe_skipped_when_disabled(monkeypatch, db, mem_user):
    from app.models import UserMemory

    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    memory.observe(db, mem_user.id, "校招出差前需要准备什么")
    db.expire_all()
    assert db.query(UserMemory).filter(UserMemory.user_id == mem_user.id).count() == 0


def test_preference_terms_excludes_topics(db, mem_user):
    """关键设计守护：主题词太泛，不能参与查询扩展（会把检索带偏）"""
    from app.models import UserMemory

    db.add(
        UserMemory(
            user_id=mem_user.id,
            data={
                "flows": [{"term": "校园招聘流程", "count": 5}],
                "systems": [{"term": "格慧招", "count": 3}],
                "topics": [{"term": "出差", "count": 9}, {"term": "报销", "count": 8}],
            },
        )
    )
    db.commit()
    terms = memory.preference_terms(db, mem_user.id)
    assert "校园招聘流程" in terms and "格慧招" in terms
    assert "出差" not in terms and "报销" not in terms


def test_preference_terms_respects_min_count(db, mem_user):
    from app.models import UserMemory

    db.add(
        UserMemory(
            user_id=mem_user.id,
            data={
                "flows": [{"term": "只问过一次的流程", "count": 1}, {"term": "常问的流程", "count": 4}],
                "systems": [],
                "topics": [],
            },
        )
    )
    db.commit()
    terms = memory.preference_terms(db, mem_user.id)
    assert terms == ["常问的流程"]


def test_preference_terms_respects_limit(db, mem_user):
    from app.models import UserMemory

    db.add(
        UserMemory(
            user_id=mem_user.id,
            data={
                "flows": [{"term": f"流程{i}", "count": 5} for i in range(8)],
                "systems": [],
                "topics": [],
            },
        )
    )
    db.commit()
    assert len(memory.preference_terms(db, mem_user.id, limit=2)) == 2


def test_preference_terms_survives_bad_data(db, mem_user):
    """data 是畸形 JSON 时不抛异常"""
    from app.models import UserMemory

    db.add(UserMemory(user_id=mem_user.id, data={"flows": "不是列表", "systems": [None, 7]}))
    db.commit()
    assert memory.preference_terms(db, mem_user.id) == []
    assert memory.preference_snapshot(db, mem_user.id) == {"flows": [], "systems": [], "topics": []}


def test_clear_preference(db, mem_user):
    memory.observe(db, mem_user.id, "校招出差前需要准备什么")
    assert memory.clear_preference(db, mem_user.id) is True
    assert memory.preference_snapshot(db, mem_user.id) == {"flows": [], "systems": [], "topics": []}


# ===========================================================================
# G6 查询扩展（L2 的用法）
# ===========================================================================

def test_extract_keywords_appends_extra_terms():
    base = chat_service._extract_keywords("校招出差前需要准备什么")
    extended = chat_service._extract_keywords("校招出差前需要准备什么", extra_terms=["格慧招系统"])
    assert "格慧招系统" in extended
    # 问题自身的关键词必须全部保留（偏好只做增量补充）
    for kw in base:
        assert kw in extended


def test_extract_keywords_caps_extra_terms():
    """偏好最多补 2 个，上限 12，硬收口"""
    extra = [f"偏好词{i}" for i in range(10)]
    extended = chat_service._extract_keywords("校招出差前需要准备什么", extra_terms=extra)
    assert len(extended) <= 12
    assert sum(1 for w in extended if w.startswith("偏好词")) <= 2


def test_extract_keywords_dedupes_extra_terms():
    base = chat_service._extract_keywords("校园招聘流程怎么做")
    extended = chat_service._extract_keywords("校园招聘流程怎么做", extra_terms=base[:1])
    assert len(extended) == len(set(extended))


def test_extract_keywords_ignores_blank_extra_terms():
    base = chat_service._extract_keywords("校招出差前需要准备什么")
    assert chat_service._extract_keywords("校招出差前需要准备什么", extra_terms=["", "  ", None]) == base


# ===========================================================================
# G7 L3 未答问题采集
# ===========================================================================

def test_record_unanswered_inserts_row(db, mem_user):
    from app.models import UnansweredQuestion

    assert memory.record_unanswered(db, mem_user.id, "知识库里没有的问题", source="agent") is True
    row = db.query(UnansweredQuestion).filter(UnansweredQuestion.user_id == mem_user.id).first()
    assert row.question == "知识库里没有的问题"
    assert row.retrieved_nothing is True
    assert row.source == "agent"


def test_record_unanswered_skipped_when_disabled(monkeypatch, db, mem_user):
    from app.models import UnansweredQuestion

    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)
    assert memory.record_unanswered(db, mem_user.id, "问题") is False
    db.expire_all()
    assert db.query(UnansweredQuestion).filter(UnansweredQuestion.user_id == mem_user.id).count() == 0


def test_record_unanswered_ignores_empty_question(db, mem_user):
    assert memory.record_unanswered(db, mem_user.id, "   ") is False


def test_record_unanswered_keeps_event_stream(db, mem_user):
    """刻意不去重：同一问题问两次就是两行（改造 5 靠行数取频率）"""
    memory.record_unanswered(db, mem_user.id, "重复的问题")
    memory.record_unanswered(db, mem_user.id, "重复的问题")
    data = memory.list_unanswered(db, user_id=mem_user.id)
    assert data["total"] == 2


def test_record_unanswered_trim(db, mem_user, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MEMORY_UNANSWERED_KEEP", 3)
    for i in range(6):
        memory.record_unanswered(db, mem_user.id, f"缺口问题-{i}")
    data = memory.list_unanswered(db, user_id=mem_user.id)
    assert data["total"] == 3
    # 保留的是最近的
    assert any("缺口问题-5" == i["question"] for i in data["items"])


def test_list_unanswered_survives_bad_db(db, mem_user):
    """查询异常时返回空结构而不是抛异常"""
    result = memory.list_unanswered(db, user_id=mem_user.id, limit=1)
    assert set(result) >= {"total", "items"}


def test_after_answer_records_unanswered_when_not_answered(monkeypatch, db, mem_user):
    from app.models import UnansweredQuestion

    # 屏蔽异步部分，避免起真线程
    monkeypatch.setattr(memory, "index_messages_async", lambda *a, **k: None)
    monkeypatch.setattr(memory, "refresh_summary_async", lambda *a, **k: None)
    monkeypatch.setattr(memory, "observe_async", lambda *a, **k: None)

    memory.after_answer(db, mem_user, "答不上来的问题", answered=False, source="agent")
    db.expire_all()
    assert db.query(UnansweredQuestion).filter(UnansweredQuestion.user_id == mem_user.id).count() == 1


def test_after_answer_no_unanswered_when_answered(monkeypatch, db, mem_user):
    from app.models import UnansweredQuestion

    monkeypatch.setattr(memory, "index_messages_async", lambda *a, **k: None)
    monkeypatch.setattr(memory, "refresh_summary_async", lambda *a, **k: None)
    monkeypatch.setattr(memory, "observe_async", lambda *a, **k: None)

    memory.after_answer(db, mem_user, "能答上来的问题", answered=True, source="agent")
    db.expire_all()
    assert db.query(UnansweredQuestion).filter(UnansweredQuestion.user_id == mem_user.id).count() == 0


def test_after_answer_never_raises(monkeypatch, db, mem_user):
    """任何内部异常都不能冒泡（记忆是加分项，不是失败点）"""
    def _boom(*a, **k):
        raise RuntimeError("内部炸了")

    monkeypatch.setattr(memory, "index_messages_async", _boom)
    monkeypatch.setattr(memory, "refresh_summary_async", _boom)
    monkeypatch.setattr(memory, "observe_async", _boom)
    monkeypatch.setattr(memory, "record_unanswered", _boom)
    memory.after_answer(db, mem_user, "问题", answered=False)  # 不抛即通过


# ===========================================================================
# G8 记忆块组装 + API
# ===========================================================================

def test_memory_block_includes_summary_recall_and_preference(monkeypatch, db, mem_user):
    from app.models import ConversationSummary, UserMemory

    db.add(ConversationSummary(user_id=mem_user.id, summary="用户已确认出差标准为每天 300 元", covered_message_count=10))
    db.add(
        UserMemory(
            user_id=mem_user.id,
            data={"flows": [{"term": "校园招聘流程", "count": 5}], "systems": [{"term": "格慧招", "count": 3}], "topics": []},
        )
    )
    db.commit()
    monkeypatch.setattr(
        memory,
        "recall",
        lambda *a, **k: [{"message_id": "m-1", "role": "assistant", "content": "上次说过要提前 5 个工作日提交", "score": 0.8}],
    )
    block = memory.memory_block(db, mem_user, "校招出差准备")
    assert "更早的对话摘要" in block and "每天 300 元" in block
    assert "相关的历史对话片段" in block and "提前 5 个工作日" in block
    assert "常用流程" in block and "校园招聘流程" in block
    assert "关注系统" in block and "格慧招" in block


def test_memory_block_carries_readonly_constraint(monkeypatch, db, mem_user):
    """记忆块必须自带"仅供参考"约束，避免成为新的幻觉源"""
    from app.models import ConversationSummary

    db.add(ConversationSummary(user_id=mem_user.id, summary="摘要内容", covered_message_count=1))
    db.commit()
    block = memory.memory_block(db, mem_user, "问题")
    assert "仅供参考" in block
    assert "必须以" in block and "为准" in block


def test_memory_block_empty_without_any_source(monkeypatch, db, mem_user):
    monkeypatch.setattr(memory, "recall", lambda *a, **k: [])
    assert memory.memory_block(db, mem_user, "问题") == ""


def test_memory_block_never_raises_on_bad_input(monkeypatch, db, mem_user):
    monkeypatch.setattr(memory, "recall", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸")))
    assert memory.memory_block(db, mem_user, "问题") == ""


def test_memory_block_truncates_recall(monkeypatch, db, mem_user):
    long_text = "很长的历史片段" * 100
    monkeypatch.setattr(
        memory,
        "recall",
        lambda *a, **k: [{"message_id": "m", "role": "user", "content": long_text, "score": 0.9}],
    )
    monkeypatch.setattr(settings, "AGENT_MEMORY_RECALL_MAX_CHARS", 20)
    block = memory.memory_block(db, mem_user, "问题")
    assert "…" in block
    assert long_text not in block


def test_stats_shape(db, mem_user):
    stats = memory.stats(db, mem_user.id)
    assert stats["enabled"] is True
    assert stats["short_turns"] >= 4
    assert set(stats["preference"]) == {"flows", "systems", "topics"}


def test_graph_draft_node_injects_memory(monkeypatch, db, mem_user):
    """Agent 路径的 draft 节点也必须注入长期记忆（两路口径一致）"""
    from app.agent import graph as agent_graph
    from app.agent.context import agent_context
    from app.agent.router import INTENT_KB_QA
    from app.agent.state import new_state

    monkeypatch.setattr(
        chat_service, "_memory_block", lambda user, question, db_: "【长期记忆·仅供参考】注入成功"
    )
    state = new_state(question="校招出差准备", search_question="校招出差准备")
    state["intent"] = INTENT_KB_QA
    state["evidence"] = [
        {"id": "it-1", "title": "出差准备", "content": "需提交审批单", "source_type": "knowledge_item", "score": 0.9}
    ]
    with agent_context(mem_user, db, source="test"):
        out = agent_graph.node_draft(state)
    last = out["answer_messages"][-1]
    assert "长期记忆" in last.content
    assert "知识库内容" in last.content


def test_status_api_exposes_memory_config(client, ctx):
    r = client.get("/api/agent/status", headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "Agent 状态")
    data = body(r)
    assert "memory" in data
    assert data["memory"]["memory_enabled"] is True
    assert data["memory"]["short_turns"] >= 4


def test_api_memory_me_requires_login(client):
    expect(client.get("/api/memory/me"), 401, "未登录访问记忆状态")


def test_api_memory_me_returns_stats(client, ctx):
    r = client.get("/api/memory/me", headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "我的记忆状态")
    data = body(r)
    assert "short_turns" in data and "preference" in data


def test_api_memory_summary(client, ctx):
    r = client.get("/api/memory/summary", headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "我的会话摘要")
    assert "summary" in body(r)


def test_api_clear_preference(client, ctx):
    r = client.delete("/api/memory/me/preference", headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "清空我的偏好")
    assert body(r)["preference"] == {"flows": [], "systems": [], "topics": []}


def test_api_unanswered_scope_all_requires_super_admin(client, ctx):
    expect(
        client.get("/api/memory/unanswered", params={"scope": "all"}, headers=hdr(ctx["ua"]["token"])),
        403,
        "普通用户看全站未答问题",
    )
    expect(
        client.get("/api/memory/unanswered", params={"scope": "all"}, headers=hdr(ctx["sa_token"])),
        200,
        "超管看全站未答问题",
    )


def test_api_unanswered_me_isolated(client, ctx, db):
    """普通用户只能看到自己的未答问题"""
    from app.models import UnansweredQuestion
    from tests.helpers import user_obj

    ua = user_obj(db, ctx["ua"]["user"]["username"])
    ub = user_obj(db, ctx["ub"]["user"]["username"])
    memory.record_unanswered(db, ua.id, "ua 的缺口问题")
    memory.record_unanswered(db, ub.id, "ub 的缺口问题")
    try:
        r = client.get("/api/memory/unanswered", headers=hdr(ctx["ua"]["token"]))
        expect(r, 200, "我的未答问题")
        questions = {i["question"] for i in body(r)["items"]}
        assert "ua 的缺口问题" in questions
        assert "ub 的缺口问题" not in questions
    finally:
        db.query(UnansweredQuestion).filter(
            UnansweredQuestion.question.in_(["ua 的缺口问题", "ub 的缺口问题"])
        ).delete(synchronize_session=False)
        db.commit()


def test_api_unanswered_requires_login(client):
    expect(client.get("/api/memory/unanswered"), 401, "未登录访问未答问题")


def test_api_memory_stats_requires_staff(client, ctx):
    expect(client.get("/api/memory/stats", headers=hdr(ctx["ua"]["token"])), 403, "普通用户看记忆统计")
    expect(client.get("/api/memory/stats", headers=hdr(ctx["admin"]["token"])), 200, "管理员看记忆统计")


def test_api_rebuild_other_user_requires_super_admin(client, ctx):
    other = ctx["ub"]["user"]["id"]
    expect(
        client.post("/api/memory/rebuild", json={"user_id": other}, headers=hdr(ctx["ua"]["token"])),
        403,
        "普通用户重建他人记忆",
    )
    expect(
        client.post("/api/memory/rebuild", json={"user_id": other}, headers=hdr(ctx["sa_token"])),
        200,
        "超管重建他人记忆",
    )


# ===========================================================================
# G9 遗忘：删消息 / 清空对话时同步清理记忆
# （真实链路冒烟发现的缺口：只删 chat_messages 会让已删内容被重新召回）
# ===========================================================================

def _filter_dump(points_selector):
    """把 FilterSelector 拍平成便于断言的结构"""
    flt = getattr(points_selector, "filter", None)
    must = list(getattr(flt, "must", None) or [])
    fields, has_ids = {}, []
    for cond in must:
        key = getattr(cond, "key", None)
        if key:
            match = getattr(cond, "match", None)
            fields[key] = getattr(match, "value", None)
        ids = getattr(cond, "has_id", None)
        if ids:
            has_ids.extend(ids)
    return fields, has_ids


def test_forget_messages_deletes_scoped_points(monkeypatch, db, mem_user):
    """删指定消息时：过滤条件必须同时带 user_id 与 has_id（防越权删他人向量）"""
    fake = FakeQdrant()
    monkeypatch.setattr("app.services.qdrant_service.get_qdrant_client", lambda: fake)
    assert memory.forget_messages(db, mem_user, ["m-1", "m-2"]) is True
    assert len(fake.deleted) == 1
    fields, has_ids = _filter_dump(fake.deleted[0])
    assert fields.get("user_id") == mem_user.id
    assert sorted(has_ids) == ["m-1", "m-2"]


def test_forget_messages_all_for_user(monkeypatch, db, mem_user):
    """清空对话时按 user_id 全量删，不带 has_id"""
    fake = FakeQdrant()
    monkeypatch.setattr("app.services.qdrant_service.get_qdrant_client", lambda: fake)
    assert memory.forget_messages(db, mem_user, all_for_user=True) is True
    fields, has_ids = _filter_dump(fake.deleted[0])
    assert fields.get("user_id") == mem_user.id
    assert has_ids == []


def test_forget_messages_noop_without_ids(monkeypatch, db, mem_user):
    fake = FakeQdrant()
    monkeypatch.setattr("app.services.qdrant_service.get_qdrant_client", lambda: fake)
    assert memory.forget_messages(db, mem_user, []) is False
    assert fake.deleted == []


def test_forget_messages_noop_when_collection_missing(monkeypatch, db, mem_user):
    fake = FakeQdrant(collections=())
    monkeypatch.setattr("app.services.qdrant_service.get_qdrant_client", lambda: fake)
    assert memory.forget_messages(db, mem_user, ["m-1"]) is False


def test_forget_messages_skipped_when_disabled(monkeypatch, db, mem_user):
    monkeypatch.setattr(settings, "AGENT_MEMORY_ENABLED", False)

    def _boom():
        raise AssertionError("关闭记忆时不该去动向量库")

    monkeypatch.setattr("app.services.qdrant_service.get_qdrant_client", _boom)
    assert memory.forget_messages(db, mem_user, ["m-1"]) is False


def test_forget_messages_never_raises(monkeypatch, db, mem_user):
    def _boom():
        raise RuntimeError("向量库炸了")

    monkeypatch.setattr("app.services.qdrant_service.get_qdrant_client", _boom)
    assert memory.forget_messages(db, mem_user, ["m-1"]) is False  # 不抛即通过


def test_forget_summary(db, mem_user):
    from app.models import ConversationSummary

    db.add(ConversationSummary(user_id=mem_user.id, summary="旧摘要", covered_message_count=3))
    db.commit()
    assert memory.forget_summary(db, mem_user.id) is True
    assert memory.summary_text(db, mem_user.id) == ""


def test_clear_conversation_memory_keeps_profile_and_gap_log(monkeypatch, db, mem_user):
    """清空对话只清"会话级"记忆，长期偏好与缺口台账要留下"""
    from app.models import ConversationSummary, UnansweredQuestion, UserMemory

    db.add(ConversationSummary(user_id=mem_user.id, summary="旧摘要", covered_message_count=3))
    db.add(UserMemory(user_id=mem_user.id, data={"flows": [{"term": "校园招聘流程", "count": 5}], "systems": [], "topics": []}))
    db.add(UnansweredQuestion(user_id=mem_user.id, question="历史缺口"))
    db.commit()

    captured = {}
    monkeypatch.setattr(
        memory, "forget_messages_async",
        lambda uid, ids=None, all_for_user=False: captured.update(uid=uid, all_for_user=all_for_user),
    )
    memory.clear_conversation_memory(db, mem_user.id)
    db.expire_all()

    assert captured == {"uid": mem_user.id, "all_for_user": True}
    assert memory.summary_text(db, mem_user.id) == ""
    assert db.query(UserMemory).filter(UserMemory.user_id == mem_user.id).count() == 1
    assert db.query(UnansweredQuestion).filter(UnansweredQuestion.user_id == mem_user.id).count() == 1


def test_service_delete_message_schedules_forget(monkeypatch, db, mem_user):
    """服务层删消息时必须调度向量清理（否则删掉的内容还能被召回）"""
    from app.models import ChatMessage
    from app.services import chat_message_service

    db.add(ChatMessage(user_id=mem_user.id, role="user", content="要删掉的内容"))
    db.commit()
    msg_id = db.query(ChatMessage.id).filter(ChatMessage.user_id == mem_user.id).first()[0]

    captured = {}
    monkeypatch.setattr(
        memory, "forget_messages_async",
        lambda uid, ids=None, all_for_user=False: captured.update(uid=uid, ids=list(ids or []), all=all_for_user),
    )
    assert chat_message_service.delete_message(db, msg_id, mem_user.id) is True
    assert captured == {"uid": mem_user.id, "ids": [msg_id], "all": False}


def test_service_batch_delete_schedules_forget(monkeypatch, db, mem_user):
    from app.models import ChatMessage
    from app.services import chat_message_service

    db.add(ChatMessage(user_id=mem_user.id, role="user", content="批量删-1"))
    db.add(ChatMessage(user_id=mem_user.id, role="assistant", content="批量删-2"))
    db.commit()
    ids = [r[0] for r in db.query(ChatMessage.id).filter(ChatMessage.user_id == mem_user.id).all()]

    captured = {}
    monkeypatch.setattr(
        memory, "forget_messages_async",
        lambda uid, ids=None, all_for_user=False: captured.update(ids=sorted(ids or [])),
    )
    assert chat_message_service.batch_delete_messages(db, ids, mem_user.id) == 2
    assert captured["ids"] == sorted(ids)


def test_service_clear_messages_purges_session_memory(monkeypatch, db, mem_user):
    from app.models import ChatMessage, ConversationSummary
    from app.services import chat_message_service

    _seed_turns(db, mem_user.id, 3)
    db.add(ConversationSummary(user_id=mem_user.id, summary="旧摘要", covered_message_count=2))
    db.commit()

    captured = {}
    monkeypatch.setattr(
        memory, "forget_messages_async",
        lambda uid, ids=None, all_for_user=False: captured.update(all=all_for_user),
    )
    assert chat_message_service.clear_messages(db, mem_user.id) == 6
    db.expire_all()
    assert captured.get("all") is True
    assert db.query(ConversationSummary).filter(ConversationSummary.user_id == mem_user.id).count() == 0


def test_service_delete_message_survives_memory_failure(monkeypatch, db, mem_user):
    """记忆层炸了也不能让"删消息"失败"""
    from app.models import ChatMessage
    from app.services import chat_message_service

    db.add(ChatMessage(user_id=mem_user.id, role="user", content="要删掉的内容"))
    db.commit()
    msg_id = db.query(ChatMessage.id).filter(ChatMessage.user_id == mem_user.id).first()[0]

    def _boom(*a, **k):
        raise RuntimeError("记忆层炸了")

    monkeypatch.setattr(memory, "forget_messages_async", _boom)
    assert chat_message_service.delete_message(db, msg_id, mem_user.id) is True


def test_api_rebuild_self_allowed(client, ctx):
    r = client.post("/api/memory/rebuild", json={"force": False}, headers=hdr(ctx["admin"]["token"]))
    expect(r, 200, "管理员重建自己的记忆")
    assert "indexed" in body(r)
    assert body(r)["user_id"] == ctx["admin"]["user"]["id"]
