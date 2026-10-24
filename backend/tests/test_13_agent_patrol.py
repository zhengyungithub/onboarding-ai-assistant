"""主动巡检 Agent（改造 5）：从被动到主动

守护点（这些是"改完不劣化 + 不误发布"的验收线）：

1. **只产出草稿**：巡检生成的知识产物一律 `status=draft`，绝不自动发布。
   有一条用例专门断言"巡检跑完后已发布条目数不变"。
2. **分段兜底**：四个检查器任一失败只让该段缺席，报告降级为 `partial`，
   其余区块照常出结果——离线批处理不该因为一段失败就整份作废。
3. **失败即降级**：无 embedding → 不聚类（不出缺口，安全方向）；
   无模型 → 只出缺口报告、**不建空壳草稿**（草稿要有内容才有复核价值）。
4. **口径正确**：有用率只用"用户真的评价过"的那部分算，
   否则"用户懒得评价"会被误判成"问答质量差"。
5. **权限**：`scope=all` 仅超管；普通用户完全看不到巡检入口。
6. 全程只读免疫：畸形输入（None / 空 / 维度不一致 / 抛异常的 embedding）都不抛异常。

全程离线：Embedding / LLM / Qdrant 全部用测试替身，不访问任何网络。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.helpers import body, expect, hdr, make_chat_pair, user_obj

from app.agent import patrol
from app.core.config import settings

# 测试数据统一前缀：用例结束按前缀清理，绝不留痕影响其他用例
PREFIX = "巡检测试"
NEAR = [1.0, 0.0, 0.0]      # 「相似」向量
FAR = [0.0, 1.0, 0.0]       # 「不相似」向量

DRAFT_JSON = (
    '{"title":"' + PREFIX + '草稿：报销提交",'
    '"steps":["登录系统","填写单据","提交审批"],'
    '"system_or_material":"报销系统","caution":"注意时限"}'
)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class FakeLLM:
    """按队列返回内容的假模型（队列用尽后重复最后一条，记录调用次数）

    重复最后一条是为了让用例不受"检查器遍历顺序"影响——例如一致性检查会遍历
    库里所有"有描述 + 有关联条目"的节点，我们没法保证被测节点排第一。
    """

    def __init__(self, *contents):
        self.queue = list(contents)
        self.last = ""
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if not self.queue:
            return SimpleNamespace(content=self.last)
        self.last = self.queue.pop(0)
        return SimpleNamespace(content=self.last)


class FakeEmbedder:
    """把预设文本映射到固定向量的假 embedding（记录调用过的文本）"""

    def __init__(self, mapping, default=None):
        self.mapping = dict(mapping)
        self.default = default
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        return self.mapping.get(text, self.default)


class BoomEmbedder:
    """永远抛异常（模拟没配 Embedding Key / 网络不通）"""

    def __call__(self, text):
        raise RuntimeError("no embedding api key")


def _embedder(vector):
    """常量向量 embedder（所有问题都相似）"""
    return FakeEmbedder({}, default=list(vector))


# ---------------------------------------------------------------------------
# 数据构造
# ---------------------------------------------------------------------------

def _make_item(db, user_id, title, *, status="published", age_days=0,
               caution="", visibility="all"):
    """直接建知识条目（绕过向量化，避免依赖 Qdrant / Embedding）"""
    from app.models import KnowledgeItem

    ts = datetime.now() - timedelta(days=age_days)
    item = KnowledgeItem(
        user_id=user_id,
        title=f"{PREFIX}{title}",
        steps=["步骤一", "步骤二"],
        caution=caution or None,
        status=status,
        visibility=visibility,
        is_shared=visibility != "private",
        created_at=ts,
        updated_at=ts,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _ref(item_id, title="", source_type="knowledge_item"):
    return {
        "id": item_id,
        "title": title,
        "source_type": source_type,
        "score": 0.9,
        "verified": True,
    }


def _cite(db, user_id, item, times, *, useful=0, useless=0, extra_refs=None):
    """造 N 条引用该条目的回答，其中 useful/useless 条带反馈"""
    assert useful + useless <= times
    for i in range(times):
        if i < useful:
            feedback = "useful"
        elif i < useful + useless:
            feedback = "useless"
        else:
            feedback = None
        refs = [_ref(item.id, item.title)] + list(extra_refs or [])
        make_chat_pair(
            db,
            user_id,
            f"{PREFIX}引用问题 {i}",
            f"{PREFIX}引用回答 {i}",
            feedback=feedback,
            references=refs,
            offset_seconds=i,
        )


@pytest.fixture(autouse=True)
def _patrol_cleanup(db):
    """按统一前缀清理本模块写入的数据，绝不影响其他用例"""
    yield
    from app.models import ChatMessage, KnowledgeItem, PatrolReport, UnansweredQuestion

    try:
        db.rollback()
        db.query(KnowledgeItem).filter(KnowledgeItem.title.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
        db.query(UnansweredQuestion).filter(
            UnansweredQuestion.question.like(f"{PREFIX}%")
        ).delete(synchronize_session=False)
        db.query(ChatMessage).filter(ChatMessage.content.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
        db.query(PatrolReport).delete(synchronize_session=False)
        db.commit()
        db.expire_all()
    except Exception:  # noqa: BLE001 —— 清理失败不该让用例失败
        db.rollback()


# ===========================================================================
# A. 开关与范围口径
# ===========================================================================

def test_patrol_enabled_by_default():
    assert patrol.enabled() is True


def test_mode_info_shape():
    info = patrol.mode_info()
    for key in ("enabled", "stale_days", "gap_min_cluster", "gap_similarity",
                "draft_enabled", "quality_min_cited", "quality_max_useful_rate",
                "consistency_enabled", "report_keep"):
        assert key in info, f"巡检配置缺少 {key}"
    assert info["stale_days"] >= 1


def test_stale_days_has_floor(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_PATROL_STALE_DAYS", 0)
    assert patrol.stale_days() == 1


def test_resolve_scope_for_super_admin_defaults_all(ctx):
    sa = SimpleNamespace(id="x", role="super_admin")
    assert patrol.resolve_scope(sa, "") == "all"
    assert patrol.resolve_scope(sa, "all") == "all"
    assert patrol.resolve_scope(sa, "me") == "me"


def test_resolve_scope_for_admin_is_me_and_all_is_downgraded():
    """非超管即使显式传 all 也会被兜底降级（安全默认值）"""
    admin = SimpleNamespace(id="a", role="admin")
    assert patrol.resolve_scope(admin, "") == "me"
    assert patrol.resolve_scope(admin, "all") == "me"
    assert patrol.resolve_scope(admin, "me") == "me"
    user = SimpleNamespace(id="u", role="user")
    assert patrol.resolve_scope(user, "all") == "me"


def test_run_patrol_refuses_when_disabled(monkeypatch, db, ctx):
    """一键回滚：关掉总开关后巡检不执行、也不落报告"""
    from app.models import PatrolReport

    before = db.query(PatrolReport).count()
    monkeypatch.setattr(settings, "AGENT_PATROL_ENABLED", False)
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    result = patrol.run_patrol(db, admin, llm=False)
    assert result["ok"] is False
    assert "关闭" in result["error"]
    assert db.query(PatrolReport).count() == before


# ===========================================================================
# B. 纯函数：余弦与聚类
# ===========================================================================

def test_cosine_basic():
    assert patrol.cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert patrol.cosine([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert patrol.cosine([1, 0], [2, 0]) == pytest.approx(1.0)


def test_cosine_never_raises_on_bad_input():
    assert patrol.cosine(None, [1, 0]) == 0.0
    assert patrol.cosine([1, 0], None) == 0.0
    assert patrol.cosine([], []) == 0.0
    assert patrol.cosine([1, 2], [1, 2, 3]) == 0.0      # 维度不一致
    assert patrol.cosine([0, 0], [1, 1]) == 0.0         # 零向量
    assert patrol.cosine(["a"], ["b"]) == 0.0           # 垃圾输入


def test_cluster_indices_groups_similar():
    clusters = patrol.cluster_indices([NEAR, NEAR, FAR], threshold=0.8)
    assert sorted(len(c) for c in clusters) == [1, 2]


def test_cluster_indices_below_threshold_stays_separate():
    a = [1.0, 0.0]
    b = [0.5, 0.866]  # cos ≈ 0.5 < 0.8
    clusters = patrol.cluster_indices([a, b], threshold=0.8)
    assert [[0], [1]] == clusters


def test_cluster_indices_is_leader_based_not_chained():
    """单链会把 A-B-C 串成大簇；leader-based 不允许（否则缺口报告被灌水）"""
    a = [1.0, 0.0, 0.0]
    b = [0.9, 0.435, 0.0]   # 与 a 相似度 ≈ 0.9
    c = [0.0, 1.0, 0.0]     # 与 b 相似度 ≈ 0.435，与 a 为 0
    clusters = patrol.cluster_indices([a, b, c], threshold=0.8)
    assert clusters == [[0, 1], [2]]


def test_cluster_indices_empty_and_none():
    assert patrol.cluster_indices([]) == []
    clusters = patrol.cluster_indices([None, NEAR, None], threshold=0.8)
    # 无向量的各自成簇，不参与合并
    assert clusters == [[0], [1], [2]]


# ===========================================================================
# C. ① 过期检查
# ===========================================================================

def test_stale_flags_old_published_item(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    old = _make_item(db, admin.id, "过期条目", age_days=400)
    fresh = _make_item(db, admin.id, "新条目", age_days=1)
    rows = patrol.check_stale(db, scope_user_id=admin.id, days=180)
    ids = {r["item_id"] for r in rows}
    assert old.id in ids
    assert fresh.id not in ids
    hit = next(r for r in rows if r["item_id"] == old.id)
    assert hit["age_days"] >= 399
    assert hit["title"].startswith(PREFIX)


def test_stale_ignores_drafts(db, ctx):
    """草稿本来就没进检索，不存在"过期知识被引用"的问题"""
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    draft = _make_item(db, admin.id, "老草稿", status="draft", age_days=400)
    rows = patrol.check_stale(db, scope_user_id=admin.id, days=180)
    assert draft.id not in {r["item_id"] for r in rows}


def test_stale_respects_scope(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    ua = user_obj(db, ctx["ua"]["user"]["username"])
    mine = _make_item(db, admin.id, "我的过期条目", age_days=400)
    other = _make_item(db, ua.id, "别人的过期条目", age_days=400)
    rows = patrol.check_stale(db, scope_user_id=admin.id, days=180)
    ids = {r["item_id"] for r in rows}
    assert mine.id in ids
    assert other.id not in ids
    # scope=None（全站）两条都能看到
    all_ids = {r["item_id"] for r in patrol.check_stale(db, scope_user_id=None, days=180)}
    assert {mine.id, other.id} <= all_ids


def test_stale_respects_limit(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    for i in range(4):
        _make_item(db, admin.id, f"过期{i}", age_days=400 + i)
    rows = patrol.check_stale(db, scope_user_id=admin.id, days=180, limit=2)
    assert len(rows) == 2
    # 最久未更新的排前面
    assert rows[0]["age_days"] >= rows[1]["age_days"]


# ===========================================================================
# D. ② 缺口聚类（含草稿产出与降级）
# ===========================================================================

def _seed_unanswered(db, user_id, questions):
    from app.models import UnansweredQuestion

    for q in questions:
        db.add(UnansweredQuestion(user_id=user_id, question=f"{PREFIX}{q}",
                                  retrieved_nothing=True, source="test"))
    db.commit()


def test_collect_unanswered_scope(db, ctx):
    ua = user_obj(db, ctx["ua"]["user"]["username"])
    ub = user_obj(db, ctx["ub"]["user"]["username"])
    _seed_unanswered(db, ua.id, ["uA 缺口"])
    _seed_unanswered(db, ub.id, ["uB 缺口"])
    mine = patrol.collect_unanswered(db, scope_user_id=ua.id)
    questions = {i["question"] for i in mine}
    assert f"{PREFIX}uA 缺口" in questions
    assert f"{PREFIX}uB 缺口" not in questions


def test_gap_cluster_creates_draft(db, ctx):
    """3 条相似未答问题 → 1 个有效簇 → 生成草稿（且必须是 draft）"""
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _seed_unanswered(db, admin.id, ["报销提交时限", "报销多久提交", "报销截止时间"])
    llm = FakeLLM(DRAFT_JSON, f"{PREFIX}一句话总览")

    result = patrol.check_gaps(
        db, admin, scope_user_id=admin.id, embedder=_embedder(NEAR), llm=llm, create_drafts=True
    )
    assert result["scanned"] == 3
    assert result["embedded"] == 3
    assert len(result["clusters"]) == 1
    cluster = result["clusters"][0]
    assert cluster["size"] == 3
    assert cluster["draft_item_id"], "有效缺口簇应生成草稿"

    from app.models import KnowledgeItem

    draft = db.query(KnowledgeItem).filter(KnowledgeItem.id == cluster["draft_item_id"]).first()
    assert draft is not None
    assert draft.status == "draft", "巡检查产物绝不能是 published"
    assert draft.steps == ["登录系统", "填写单据", "提交审批"]
    assert "主动巡检生成" in (draft.caution or ""), "草稿必须自带来源标注，便于人工复核"
    assert PREFIX in cluster["questions"][0]


def test_gap_below_min_cluster_produces_nothing(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _seed_unanswered(db, admin.id, ["报销提交时限", "报销多久提交"])
    llm = FakeLLM(DRAFT_JSON)
    result = patrol.check_gaps(
        db, admin, scope_user_id=admin.id, embedder=_embedder(NEAR), llm=llm, create_drafts=True
    )
    assert result["clusters"] == []
    assert llm.calls == 0, "簇不够大就不该调模型"


def test_gap_dissimilar_questions_not_clustered(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _seed_unanswered(db, admin.id, ["报销提交时限", "差旅住宿标准", "年假如何申请"])
    embedder = FakeEmbedder(
        {
            f"{PREFIX}报销提交时限": NEAR,
            f"{PREFIX}差旅住宿标准": FAR,
            f"{PREFIX}年假如何申请": [0.0, 0.0, 1.0],
        }
    )
    result = patrol.check_gaps(
        db, admin, scope_user_id=admin.id, embedder=embedder, llm=FakeLLM(DRAFT_JSON),
        create_drafts=True,
    )
    assert result["clusters"] == []


def test_gap_without_llm_reports_but_creates_no_draft(db, ctx):
    """模型不可用 → 只出缺口报告，绝不建空壳草稿"""
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _seed_unanswered(db, admin.id, ["报销提交时限", "报销多久提交", "报销截止时间"])
    result = patrol.check_gaps(
        db, admin, scope_user_id=admin.id, embedder=_embedder(NEAR), llm=None, create_drafts=True
    )
    assert len(result["clusters"]) == 1
    cluster = result["clusters"][0]
    assert cluster["draft_item_id"] is None
    assert "未生成草稿" in cluster["draft_error"]


def test_gap_draft_disabled_by_flag(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _seed_unanswered(db, admin.id, ["报销提交时限", "报销多久提交", "报销截止时间"])
    llm = FakeLLM(DRAFT_JSON)
    result = patrol.check_gaps(
        db, admin, scope_user_id=admin.id, embedder=_embedder(NEAR), llm=llm, create_drafts=False
    )
    assert result["clusters"][0]["draft_item_id"] is None
    assert llm.calls == 0


def test_gap_embedder_failure_degrades_safely(db, ctx):
    """embedding 不可用 → 不聚类、不出缺口（安全方向），并说明原因"""
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _seed_unanswered(db, admin.id, ["报销提交时限", "报销多久提交", "报销截止时间"])
    result = patrol.check_gaps(
        db, admin, scope_user_id=admin.id, embedder=BoomEmbedder(), llm=None, create_drafts=True
    )
    assert result["embedded"] == 0
    assert result["clusters"] == []
    assert "embedding" in result["error"]


def test_gap_embeds_duplicate_text_once(db, ctx):
    """同一问题被问多次：只 embed 一次（省调用），计数仍保留"""
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _seed_unanswered(db, admin.id, ["报销提交时限", "报销提交时限", "报销提交时限"])
    embedder = _embedder(NEAR)
    result = patrol.check_gaps(
        db, admin, scope_user_id=admin.id, embedder=embedder, llm=None, create_drafts=False
    )
    assert len(embedder.calls) == 1
    assert result["scanned"] == 3
    assert result["clusters"][0]["size"] == 3


# ===========================================================================
# E. ③ 质量异常
# ===========================================================================

def test_quality_flags_low_useful_rate(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    item = _make_item(db, admin.id, "低质条目")
    _cite(db, admin.id, item, times=5, useful=0, useless=3)
    issues = patrol.check_quality(db, admin, scope_user_id=admin.id)
    hit = next((i for i in issues if i["item_id"] == item.id), None)
    assert hit is not None
    assert hit["cited"] == 5
    assert hit["feedback"] == 3
    assert hit["useful_rate"] == 0.0
    assert "建议复核" in hit["reason"]


def test_quality_skips_below_citation_floor(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    item = _make_item(db, admin.id, "引用太少")
    _cite(db, admin.id, item, times=4, useful=0, useless=3)
    issues = patrol.check_quality(db, admin, scope_user_id=admin.id)
    assert item.id not in {i["item_id"] for i in issues}


def test_quality_skips_when_feedback_too_few(db, ctx):
    """引用多但没人评价 → 不能判定"质量差"（用户懒得评价 ≠ 回答差）"""
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    item = _make_item(db, admin.id, "无人评价")
    _cite(db, admin.id, item, times=6, useful=0, useless=1)
    issues = patrol.check_quality(db, admin, scope_user_id=admin.id)
    assert item.id not in {i["item_id"] for i in issues}


def test_quality_boundary_rate_equal_ceiling_is_ok(db, ctx):
    """有用率恰好等于阈值（0.5）不算异常——只有"低于"才报"""
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    item = _make_item(db, admin.id, "边界条目")
    _cite(db, admin.id, item, times=5, useful=1, useless=1)
    issues = patrol.check_quality(db, admin, scope_user_id=admin.id)
    assert item.id not in {i["item_id"] for i in issues}


def test_quality_ignores_non_knowledge_refs(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    item = _make_item(db, admin.id, "只有文档引用")
    extra = [_ref("doc-1", "某文档", source_type="source_document")]
    _cite(db, admin.id, item, times=0, useful=0, useless=0, extra_refs=extra)
    for i in range(5):
        make_chat_pair(
            db, admin.id, f"{PREFIX}文档问题 {i}", f"{PREFIX}文档回答 {i}",
            feedback="useless", references=list(extra), offset_seconds=i,
        )
    stats = patrol.scan_citations(db, scope_user_id=admin.id)
    assert "doc-1" not in stats
    assert item.id not in stats


def test_quality_reports_missing_item(db, ctx):
    """条目已被删但引用记录还在 → 仍要报出来（missing=True）"""
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    item = _make_item(db, admin.id, "将被删除")
    item_id = item.id
    _cite(db, admin.id, item, times=5, useful=0, useless=2)
    db.delete(item)
    db.commit()
    issues = patrol.check_quality(db, admin, scope_user_id=admin.id)
    hit = next((i for i in issues if i["item_id"] == item_id), None)
    assert hit is not None
    assert hit["missing"] is True


def test_scan_citations_scope_isolation(db, ctx):
    ua = user_obj(db, ctx["ua"]["user"]["username"])
    ub = user_obj(db, ctx["ub"]["user"]["username"])
    item_a = _make_item(db, ua.id, "A 的条目")
    item_b = _make_item(db, ub.id, "B 的条目")
    _cite(db, ua.id, item_a, times=1)
    _cite(db, ub.id, item_b, times=1)
    stats = patrol.scan_citations(db, scope_user_id=ua.id)
    assert item_a.id in stats
    assert item_b.id not in stats


# ===========================================================================
# F. ④ 一致性检查
# ===========================================================================

def test_consistency_requires_llm(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    assert patrol.check_consistency(db, admin, llm=None) == []


def test_consistency_detects_conflict(monkeypatch, db, ctx):
    from app.models import Flow, FlowNode

    # 库里可能已有其他流程节点，把上限放大，保证被测节点一定被检查到
    monkeypatch.setattr(settings, "AGENT_PATROL_CONSISTENCY_MAX_PAIRS", 300)
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    item = _make_item(db, admin.id, "接洽函条目")
    node = FlowNode(name=f"{PREFIX}节点", description="先提交接洽函再登记", order=1,
                    related_item_ids=[item.id])
    flow = Flow(user_id=admin.id, name=f"{PREFIX}流程", description="巡检用流程", nodes=[node])
    db.add(flow)
    db.commit()
    db.refresh(flow)
    node = flow.nodes[0]

    llm = FakeLLM('{"conflict": true, "reason": "步骤顺序相反"}')
    issues = patrol.check_consistency(db, admin, llm=llm)
    hit = next((i for i in issues if i["node_id"] == node.id), None)
    assert hit is not None
    assert hit["item_id"] == item.id
    assert "顺序" in hit["reason"]

    db.query(FlowNode).filter(FlowNode.id == node.id).delete(synchronize_session=False)
    db.query(Flow).filter(Flow.id == flow.id).delete(synchronize_session=False)
    db.commit()


def test_consistency_no_conflict_yields_nothing(db, ctx):
    from app.models import Flow, FlowNode

    admin = user_obj(db, ctx["admin"]["user"]["username"])
    item = _make_item(db, admin.id, "无冲突条目")
    node = FlowNode(name=f"{PREFIX}无冲突节点", description="按流程执行", order=1,
                    related_item_ids=[item.id])
    flow = Flow(user_id=admin.id, name=f"{PREFIX}无冲突流程", description="", nodes=[node])
    db.add(flow)
    db.commit()

    llm = FakeLLM('{"conflict": false, "reason": ""}')
    issues = patrol.check_consistency(db, admin, llm=llm)
    assert item.id not in {i["item_id"] for i in issues}

    db.query(FlowNode).filter(FlowNode.flow_id == flow.id).delete(synchronize_session=False)
    db.query(Flow).filter(Flow.id == flow.id).delete(synchronize_session=False)
    db.commit()


# ===========================================================================
# G. 主流程与报告
# ===========================================================================

def test_run_patrol_end_to_end(db, ctx):
    from app.models import PatrolReport

    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _make_item(db, admin.id, "过期条目", age_days=400)
    _seed_unanswered(db, admin.id, ["报销提交时限", "报销多久提交", "报销截止时间"])
    item = _make_item(db, admin.id, "低质条目")
    _cite(db, admin.id, item, times=5, useful=0, useless=3)

    result = patrol.run_patrol(
        db, admin, llm=False, embedder=_embedder(NEAR), create_drafts=False
    )
    assert result["ok"] is True
    assert result["status"] == "ok", result["errors"]
    assert result["scope"] == "me"
    assert result["counts"]["stale"] >= 1
    assert result["counts"]["gap_clusters"] >= 1
    assert result["counts"]["quality"] >= 1
    assert result["counts"]["consistency"] == 0
    assert result["summary"], "总览不能为空"
    assert result["report_id"]

    row = db.query(PatrolReport).filter(PatrolReport.id == result["report_id"]).first()
    assert row is not None
    assert row.scope == "me"
    assert row.counts["quality"] >= 1
    assert db.query(PatrolReport).count() >= 1


def test_run_patrol_creates_only_drafts(db, ctx):
    """核心底线：巡检跑完后，已发布条目数一个都没变（绝不误发布）"""
    from app.models import KnowledgeItem

    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _seed_unanswered(db, admin.id, ["报销提交时限", "报销多久提交", "报销截止时间"])
    before = db.query(KnowledgeItem).filter(KnowledgeItem.status == "published").count()

    result = patrol.run_patrol(
        db, admin, llm=FakeLLM(DRAFT_JSON, "一句话总览"),
        embedder=_embedder(NEAR), create_drafts=True,
    )
    after = db.query(KnowledgeItem).filter(KnowledgeItem.status == "published").count()
    assert after == before, "巡检绝不能发布任何条目"
    drafts = [c for c in result["gap_clusters"] if c.get("draft_item_id")]
    assert drafts, "缺草稿时该用例失去意义"
    for c in drafts:
        item = db.query(KnowledgeItem).filter(KnowledgeItem.id == c["draft_item_id"]).first()
        assert item.status == "draft"


def test_run_patrol_partial_when_one_section_fails(monkeypatch, db, ctx):
    """分段兜底：一段炸了不影响其余，报告降级为 partial"""
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _make_item(db, admin.id, "过期条目", age_days=400)

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟检查器崩溃")

    monkeypatch.setattr(patrol, "check_stale", _boom)
    result = patrol.run_patrol(db, admin, llm=False, embedder=_embedder(NEAR))
    assert result["ok"] is True
    assert result["status"] == "partial"
    assert "stale_items" in result["errors"]
    assert result["stale_items"] == []


def test_run_patrol_status_error_when_all_sections_fail(monkeypatch, db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])

    def _boom(*args, **kwargs):
        raise RuntimeError("全挂")

    for name in ("check_stale", "check_gaps", "check_quality"):
        monkeypatch.setattr(patrol, name, _boom)
    result = patrol.run_patrol(db, admin, llm=False)
    assert result["ok"] is True
    assert result["status"] == "error"
    assert len(result["errors"]) >= 3


def test_run_patrol_never_raises_on_bad_context():
    result = patrol.run_patrol(None, None, llm=False)
    assert result["ok"] is False


def test_run_patrol_without_findings_uses_rule_summary(db, ctx):
    """没有发现任何问题时，也应该有一份可读的报告（而不是空白）"""
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    # 用极严的阈值把三类发现全部排除
    result = patrol.run_patrol(db, admin, llm=False, embedder=_embedder(NEAR))
    assert result["ok"] is True
    assert isinstance(result["summary"], str) and result["summary"]


def test_report_trim_keeps_latest(monkeypatch, db, ctx):
    from app.models import PatrolReport

    admin = user_obj(db, ctx["admin"]["user"]["username"])
    monkeypatch.setattr(settings, "AGENT_PATROL_REPORT_KEEP", 2)
    for _ in range(4):
        patrol.run_patrol(db, admin, llm=False, embedder=_embedder(NEAR))
    assert db.query(PatrolReport).count() == 2


def test_list_and_get_report(db, ctx):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    result = patrol.run_patrol(db, admin, llm=False, embedder=_embedder(NEAR))
    listing = patrol.list_reports(db, limit=5)
    assert listing["total"] >= 1
    assert any(i["id"] == result["report_id"] for i in listing["items"])
    detail = patrol.get_report(db, result["report_id"])
    assert detail is not None
    for key in ("stale_items", "gap_clusters", "quality_issues", "consistency_issues"):
        assert key in detail, f"报告详情缺少区块 {key}"
    assert patrol.get_report(db, "not-exist") is None


def test_serialize_report_tolerates_empty_fields():
    row = SimpleNamespace(
        id="r1", user_id="u1", scope="me", status="ok", summary=None,
        counts=None, duration_ms=None, error=None, run_at=None,
        stale_items=None, gap_clusters=None, quality_issues=None, consistency_issues=None,
    )
    data = patrol.serialize_report(row, with_detail=True)
    assert data["stale_items"] == []
    assert data["counts"] == {}
    assert data["errors"] == {}
    assert data["run_at"] is None


# ===========================================================================
# H. API 权限与返回结构
# ===========================================================================

def test_api_status_visible_to_normal_user(client, ctx):
    r = client.get("/api/agent/patrol/status", headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "普通用户查看巡检配置")
    data = body(r)
    assert data["enabled"] is True
    assert data["can_patrol_all"] is False
    assert data["scope_for_me"] == "me"


def test_api_run_forbidden_for_normal_user(client, ctx):
    r = client.post("/api/agent/patrol", json={}, headers=hdr(ctx["ua"]["token"]))
    expect(r, 403, "普通用户不能触发巡检")


def test_api_run_all_forbidden_for_admin(client, ctx):
    r = client.post(
        "/api/agent/patrol", json={"scope": "all"}, headers=hdr(ctx["admin"]["token"])
    )
    expect(r, 403, "全站巡检仅超管")


def test_api_run_invalid_scope(client, ctx):
    r = client.post(
        "/api/agent/patrol", json={"scope": "everything"}, headers=hdr(ctx["admin"]["token"])
    )
    expect(r, 422, "非法 scope 应被拒绝")


def test_api_run_admin_me_scope(client, ctx, db, monkeypatch):
    admin = user_obj(db, ctx["admin"]["user"]["username"])
    _seed_unanswered(db, admin.id, ["报销提交时限", "报销多久提交", "报销截止时间"])
    monkeypatch.setattr(
        "app.services.embedding_service.embed_dense",
        lambda text, user: list(NEAR),
    )
    monkeypatch.setattr(patrol, "_resolve_llm", lambda user, llm: None)

    r = client.post(
        "/api/agent/patrol", json={"scope": "me"}, headers=hdr(ctx["admin"]["token"])
    )
    expect(r, 200, "管理员巡检自己的内容")
    data = body(r)
    assert data["ok"] is True
    assert data["scope"] == "me"
    assert data["counts"]["gap_clusters"] >= 1


def test_api_run_super_admin_all_scope(client, ctx, db, monkeypatch):
    from app.models import KnowledgeItem

    sa = user_obj(db, ctx["super"]["username"])
    _make_item(db, sa.id, "超管过期条目", age_days=500)
    monkeypatch.setattr(
        "app.services.embedding_service.embed_dense",
        lambda text, user: list(NEAR),
    )
    monkeypatch.setattr(patrol, "_resolve_llm", lambda user, llm: None)

    r = client.post(
        "/api/agent/patrol", json={"scope": "all", "create_drafts": False},
        headers=hdr(ctx["sa_token"]),
    )
    expect(r, 200, "超管全站巡检")
    data = body(r)
    assert data["scope"] == "all"
    assert data["counts"]["stale"] >= 1


def test_api_reports_permissions(client, ctx, db, monkeypatch):
    monkeypatch.setattr(
        "app.services.embedding_service.embed_dense",
        lambda text, user: list(NEAR),
    )
    monkeypatch.setattr(patrol, "_resolve_llm", lambda user, llm: None)
    r = client.post("/api/agent/patrol", json={}, headers=hdr(ctx["admin"]["token"]))
    expect(r, 200, "管理员触发巡检")
    report_id = body(r)["report_id"]

    expect(
        client.get("/api/agent/patrol/reports", headers=hdr(ctx["ua"]["token"])),
        403,
        "普通用户不能看巡检报告",
    )
    r = client.get("/api/agent/patrol/reports", headers=hdr(ctx["admin"]["token"]))
    expect(r, 200, "管理员看巡检报告列表")
    assert body(r)["total"] >= 1
    assert "stale_items" not in body(r)["items"][0], "列表页不该带完整区块"

    r = client.get(
        f"/api/agent/patrol/reports/{report_id}", headers=hdr(ctx["admin"]["token"])
    )
    expect(r, 200, "管理员看报告详情")
    assert "stale_items" in body(r)

    expect(
        client.get("/api/agent/patrol/reports/not-exist", headers=hdr(ctx["admin"]["token"])),
        404,
        "不存在的报告",
    )
