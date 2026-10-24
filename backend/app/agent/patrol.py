"""主动巡检 Agent（改造 5：从被动到主动）

改造前系统是**纯被动**的：用户不问，它永远不开口——过期知识继续被引用、
反复答不上来的问题没人管、有用率很低的条目没人改、流程描述与条目矛盾没人发现。

本模块把系统翻到"主动侧"：定时（或手动）扫一遍，把四类问题整理成
**报告 + 草稿**，交给人工裁决。

| 检查器 | 规则 | 产出 |
|--------|------|------|
| ① 过期检查 | `status=published` 且 `updated_at` 超 `AGENT_PATROL_STALE_DAYS` 天 | 待复核清单（按"最久未更新"排序） |
| ② 缺口聚类 | `unanswered_questions` 向量化后按余弦 ≥ 阈值贪心归簇，簇内 ≥ N 条 | **草稿条目**（模型生成 title/steps 骨架）+ 缺口报告 |
| ③ 质量异常 | 某条目被引用 ≥ 5 次且有用率 < 50% | 改进建议（指出"引用得多但用户说没用"） |
| ④ 一致性检查 | 同一流程节点的 description 与其关联条目内容矛盾（LLM 判定） | 冲突清单，人工裁决 |

**四条设计红线（与 M3/M4 一脉相承）**

1. **只产出草稿，绝不自动发布**：所有知识产物一律 `status=draft`，
   复用现有「草稿 → 人工复核 → 发布」链路。这是"不引入错误知识"的底线——
   巡检的价值在于**把线索摆到人面前**，不在于替人做决定。
2. **永不抛异常**：四个检查器**分段兜底**，某一段炸了只让这一段缺席，
   其余照常出结果，报告状态降级为 `partial`。巡检是离线批处理，不该因为
   一个区块失败就整份作废。
3. **离线可跑**：规则检查（过期/质量/聚类）零模型调用；只有"生成草稿骨架"、
   "一致性判定"、"总览一句话"三处用模型，且**失败即降级**（无模型时不建草稿、
   总览退回规则文案）。
4. **不碰问答链路**：巡检只在被显式调用（API / 定时任务）时运行，
   与用户请求完全解耦；`AGENT_PATROL_ENABLED=false` 即一键停用。

设计文档：根目录《Agent改造设计方案.md》"改造 5：主动巡检 Agent"
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.roles import is_super_admin

# 一条引用被算作"某条目的引用"时，默认的来源类型
DEFAULT_SOURCE_TYPE = "knowledge_item"

# 生成草稿时回传给模型的未答问题条数上限（簇内问题可能很多，取前 N 条足够）
DRAFT_QUESTION_SAMPLE = 8
# 草稿标题长度上限（与 KnowledgeItem.title 的 200 对齐）
DRAFT_TITLE_MAX = 200


# ---------------------------------------------------------------------------
# 开关与配置
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """巡检总开关（关闭 = 巡检入口拒绝执行，一键回滚）"""
    return bool(getattr(settings, "AGENT_PATROL_ENABLED", True))


def _config_int(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except Exception:  # noqa: BLE001
        return default


def _config_float(name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default))
    except Exception:  # noqa: BLE001
        return default


def _config_bool(name: str, default: bool) -> bool:
    try:
        value = getattr(settings, name, default)
    except Exception:  # noqa: BLE001
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def stale_days() -> int:
    return max(1, _config_int("AGENT_PATROL_STALE_DAYS", 180))


def mode_info() -> Dict[str, Any]:
    """巡检配置摘要（并入 /api/agent/status，便于运维确认与回滚）"""
    return {
        "enabled": enabled(),
        "stale_days": stale_days(),
        "gap_min_cluster": _config_int("AGENT_PATROL_GAP_MIN_CLUSTER", 3),
        "gap_similarity": _config_float("AGENT_PATROL_GAP_SIMILARITY", 0.8),
        "draft_enabled": _config_bool("AGENT_PATROL_DRAFT_ENABLED", True),
        "quality_min_cited": _config_int("AGENT_PATROL_QUALITY_MIN_CITED", 5),
        "quality_max_useful_rate": _config_float("AGENT_PATROL_QUALITY_MAX_USEFUL_RATE", 0.5),
        "consistency_enabled": _config_bool("AGENT_PATROL_CONSISTENCY_ENABLED", False),
        "report_keep": _config_int("AGENT_PATROL_REPORT_KEEP", 200),
    }


def resolve_scope(user: Any, scope: str = "") -> str:
    """解析巡检范围

    - `""`（默认）= 自动：超管 → `all`，其他人 → `me`；
    - `all` = 全局巡检（**仅超管**，非超管会被兜底降级为 `me`）；
    - `me`  = 只巡检自己创建的内容 + 自己的未答问题。

    这里再兜一层权限，是为了让"调用方忘了校验"也不会越权——安全默认值。
    """
    text = (scope or "").strip().lower()
    if text == "all":
        return "all" if is_super_admin(user) else "me"
    if text == "me":
        return "me"
    return "all" if is_super_admin(user) else "me"


def _scope_user_id(user: Any, scope: str) -> Optional[str]:
    """`me` 返回用户 id（用于过滤）；`all` 返回 None（不过滤）"""
    return None if scope == "all" else getattr(user, "id", None)


# ---------------------------------------------------------------------------
# 纯函数：相似度与聚类（无 I/O，便于单测）
# ---------------------------------------------------------------------------

def cosine(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> float:
    """余弦相似度（零向量 / 空向量 / 维度不一致一律返回 0.0，绝不抛异常）"""
    if not a or not b:
        return 0.0
    try:
        if len(a) != len(b):
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b):
            fx = float(x)
            fy = float(y)
            dot += fx * fy
            na += fx * fx
            nb += fy * fy
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / (math.sqrt(na) * math.sqrt(nb))
    except Exception:  # noqa: BLE001
        return 0.0


def cluster_indices(
    vectors: Sequence[Optional[Sequence[float]]], threshold: float = 0.8
) -> List[List[int]]:
    """按余弦相似度做**贪心归簇**（leader-based），返回每个簇的原始下标

    为什么用 leader-based 而不是层次/单链聚类：
    - 单链会把"A 像 B、B 像 C"串成一个大簇（A 与 C 其实不像），缺口报告会被灌水；
    - leader-based 只跟**簇代表**比，结果确定、可预期、便于解释与测试。

    无向量的条目（`None` / 空）单独成簇——它们不该被强行合并进任何簇。
    """
    clusters: List[List[int]] = []
    leaders: List[int] = []
    for idx, vec in enumerate(vectors or []):
        placed = False
        if vec:
            for ci, leader in enumerate(leaders):
                if cosine(vec, (vectors or [])[leader]) >= threshold:
                    clusters[ci].append(idx)
                    placed = True
                    break
        if not placed:
            clusters.append([idx])
            leaders.append(idx)
    return clusters


# ---------------------------------------------------------------------------
# ① 过期检查（纯 DB，零模型调用）
# ---------------------------------------------------------------------------

def check_stale(
    db: Session,
    *,
    scope_user_id: Optional[str] = None,
    days: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """找出"已发布但长期没更新"的条目，按最久未更新排序

    注意：只看 `published`。草稿本来就没进检索，不存在"过期知识被引用"的问题。
    """
    out: List[Dict[str, Any]] = []
    if db is None:
        return out
    window = max(1, days if days is not None else stale_days())
    cap = max(1, limit if limit is not None else _config_int("AGENT_PATROL_MAX_ITEMS", 50))
    try:
        from sqlalchemy import or_

        from app.models import KnowledgeItem

        cutoff = datetime.now() - timedelta(days=window)
        query = db.query(KnowledgeItem).filter(
            KnowledgeItem.status == "published",
            or_(
                KnowledgeItem.updated_at < cutoff,
                # 老数据可能没有 updated_at，用 created_at 兜底判定
                (KnowledgeItem.updated_at.is_(None)) & (KnowledgeItem.created_at < cutoff),
            ),
        )
        if scope_user_id:
            query = query.filter(KnowledgeItem.user_id == scope_user_id)
        rows = query.order_by(KnowledgeItem.updated_at.asc()).limit(cap).all()

        now = datetime.now()
        for item in rows:
            ts = item.updated_at or item.created_at
            age = int((now - ts).total_seconds() // 86400) if ts else None
            out.append(
                {
                    "item_id": item.id,
                    "title": item.title or "",
                    "user_id": item.user_id,
                    "status": item.status,
                    "updated_at": ts.isoformat() if ts else None,
                    "age_days": age,
                    "reason": f"已发布 {age if age is not None else '?'} 天未更新（阈值 {window} 天）",
                }
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 过期检查失败（跳过该区块）: {exc}")
    return out


# ---------------------------------------------------------------------------
# ③ 质量异常（纯 DB，零模型调用）
# ---------------------------------------------------------------------------

def scan_citations(
    db: Session,
    *,
    scope_user_id: Optional[str] = None,
    limit_messages: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """聚合"每个知识条目被引用多少次、其中多少条回答被用户评价过"

    口径说明（很重要，否则指标会被误读）：
    - `cited`    = 该条目出现在引用来源里的**回答条数**（分母）；
    - `feedback` = 其中**用户点了有用/没用**的回答条数；
    - `useful`   = 其中点了「有用」的条数；
    - `useful_rate = useful / feedback`——**只用有反馈的那部分算**，
      否则"用户根本懒得评价"会被误判成"问答质量差"。
    """
    stats: Dict[str, Dict[str, Any]] = {}
    if db is None:
        return stats
    cap = max(100, limit_messages if limit_messages is not None else _config_int(
        "AGENT_PATROL_QUALITY_MAX_MESSAGES", 3000
    ))
    try:
        from app.models import ChatMessage

        query = db.query(ChatMessage).filter(
            ChatMessage.role == "assistant", ChatMessage.references.isnot(None)
        )
        if scope_user_id:
            query = query.filter(ChatMessage.user_id == scope_user_id)
        rows = query.order_by(ChatMessage.created_at.desc()).limit(cap).all()

        for msg in rows:
            refs = msg.references or []
            if not isinstance(refs, list):
                continue
            feedback = (msg.feedback or "").strip().lower()
            reply_user = msg.user_id
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                if (ref.get("source_type") or DEFAULT_SOURCE_TYPE) != DEFAULT_SOURCE_TYPE:
                    continue
                item_id = ref.get("id") or ref.get("item_id")
                if not item_id:
                    continue
                item_id = str(item_id)
                rec = stats.setdefault(
                    item_id,
                    {
                        "item_id": item_id,
                        "title": "",
                        "cited": 0,
                        "feedback": 0,
                        "useful": 0,
                        "users": set(),
                        "last_seen": None,
                    },
                )
                if ref.get("title"):
                    rec["title"] = str(ref["title"])
                rec["cited"] += 1
                rec["users"].add(reply_user)
                if msg.created_at and (rec["last_seen"] is None or msg.created_at > rec["last_seen"]):
                    rec["last_seen"] = msg.created_at
                if feedback in ("useful", "useless"):
                    rec["feedback"] += 1
                    if feedback == "useful":
                        rec["useful"] += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 引用统计失败（跳过该区块）: {exc}")
    return stats


def check_quality(
    db: Session,
    user: Any = None,
    *,
    scope_user_id: Optional[str] = None,
    min_cited: Optional[int] = None,
    max_useful_rate: Optional[float] = None,
    min_feedback: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """找出"被引用得多、但用户评价差"的条目 → 给出复核建议"""
    issues: List[Dict[str, Any]] = []
    if db is None:
        return issues
    floor = max(1, min_cited if min_cited is not None else _config_int(
        "AGENT_PATROL_QUALITY_MIN_CITED", 5
    ))
    ceiling = max_useful_rate if max_useful_rate is not None else _config_float(
        "AGENT_PATROL_QUALITY_MAX_USEFUL_RATE", 0.5
    )
    feedback_floor = max(1, min_feedback if min_feedback is not None else _config_int(
        "AGENT_PATROL_QUALITY_MIN_FEEDBACK", 2
    ))
    cap = max(1, limit if limit is not None else _config_int("AGENT_PATROL_MAX_ITEMS", 50))

    stats = scan_citations(db, scope_user_id=scope_user_id)
    if not stats:
        return issues

    # 批量补条目当前的标题与状态（引用里存的标题可能已过时，或被删/下架）
    titles = _resolve_item_titles(db, list(stats.keys()))

    for item_id, rec in stats.items():
        if rec["cited"] < floor or rec["feedback"] < feedback_floor:
            continue
        rate = rec["useful"] / rec["feedback"] if rec["feedback"] else 0.0
        if rate >= ceiling:
            continue
        title = titles.get(item_id, {}).get("title") or rec["title"] or item_id
        issues.append(
            {
                "item_id": item_id,
                "title": title,
                "status": titles.get(item_id, {}).get("status"),
                "missing": item_id not in titles,
                "cited": rec["cited"],
                "feedback": rec["feedback"],
                "useful": rec["useful"],
                "useless": rec["feedback"] - rec["useful"],
                "useful_rate": round(rate, 4),
                "last_seen": rec["last_seen"].isoformat() if rec["last_seen"] else None,
                "reason": (
                    f"被引用 {rec['cited']} 次，但 {rec['feedback']} 条反馈中仅 "
                    f"{rec['useful']} 条有用（{round(rate * 100, 1)}% < {round(ceiling * 100)}%）；"
                    "建议复核步骤是否已过时、是否答非所问"
                ),
            }
        )

    # 最差的排前面；同样差则引用多的优先（影响面更大）
    issues.sort(key=lambda x: (x["useful_rate"], -x["cited"]))
    return issues[:cap]


def _resolve_item_titles(db: Session, item_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量取条目标题与状态（不存在的不编造，交由调用方按 missing 处理）"""
    out: Dict[str, Dict[str, Any]] = {}
    if db is None or not item_ids:
        return out
    try:
        from app.models import KnowledgeItem

        rows = (
            db.query(KnowledgeItem.id, KnowledgeItem.title, KnowledgeItem.status)
            .filter(KnowledgeItem.id.in_(item_ids[:500]))
            .all()
        )
        for iid, title, status in rows:
            out[str(iid)] = {"title": title or "", "status": status}
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 补全条目标题失败（忽略）: {exc}")
    return out


# ---------------------------------------------------------------------------
# ② 缺口聚类（embedding + 模型生成草稿骨架）
# ---------------------------------------------------------------------------

def collect_unanswered(
    db: Session,
    *,
    scope_user_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """取未答问题（M4 采集的事件流，这里当原料用）"""
    out: List[Dict[str, Any]] = []
    if db is None:
        return out
    cap = max(1, limit if limit is not None else _config_int(
        "AGENT_PATROL_GAP_MAX_QUESTIONS", 200
    ))
    try:
        from app.models import UnansweredQuestion

        query = db.query(UnansweredQuestion)
        if scope_user_id:
            query = query.filter(UnansweredQuestion.user_id == scope_user_id)
        rows = query.order_by(UnansweredQuestion.created_at.desc()).limit(cap).all()
        for row in rows:
            question = (row.question or "").strip()
            if not question:
                continue
            out.append(
                {
                    "id": row.id,
                    "user_id": row.user_id,
                    "question": question,
                    "retrieved_nothing": bool(row.retrieved_nothing),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 未答问题读取失败（跳过该区块）: {exc}")
    return out


def _embed_one(text: str, user: Any, embedder: Optional[Callable[[str], Any]]) -> Optional[List[float]]:
    """把一条问题向量化（失败返回 None，绝不抛异常）

    `embedder` 可注入（测试/离线）：不注入时走真实 `embed_dense`。
    """
    try:
        if embedder is not None:
            vector = embedder(text)
        else:
            from app.services.embedding_service import embed_dense

            vector = embed_dense(text, user)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 缺口聚类 embedding 失败（该条不参与聚类）: {exc}")
        return None
    if not vector:
        return None
    try:
        return [float(x) for x in vector]
    except Exception:  # noqa: BLE001
        return None


def check_gaps(
    db: Session,
    user: Any,
    *,
    scope_user_id: Optional[str] = None,
    embedder: Optional[Callable[[str], Any]] = None,
    llm: Any = None,
    create_drafts: bool = True,
) -> Dict[str, Any]:
    """缺口聚类：把反复答不上来的问题聚成簇，为有效簇生成**草稿**条目

    返回：`{"clusters": [...], "scanned": n, "embedded": n, "error": ""}`

    降级行为（都很重要）：
    - embedding 不可用 → 全部问题各自成簇 → 达不到最小簇大小 → **不产出缺口**（安全方向）；
    - 模型不可用 / 输出不可解析 → 只出"缺口报告"，**绝不建空壳草稿**（草稿要有内容才有复核价值）。
    """
    result: Dict[str, Any] = {"clusters": [], "scanned": 0, "embedded": 0, "error": ""}
    if db is None:
        return result

    questions = collect_unanswered(db, scope_user_id=scope_user_id)
    result["scanned"] = len(questions)
    if not questions:
        return result

    threshold = _config_float("AGENT_PATROL_GAP_SIMILARITY", 0.8)
    min_cluster = max(2, _config_int("AGENT_PATROL_GAP_MIN_CLUSTER", 3))
    max_clusters = max(1, _config_int("AGENT_PATROL_GAP_MAX_CLUSTERS", 10))

    # 同一文本被问多次时只 embed 一次（省调用），向量结果复用
    cache: Dict[str, Optional[List[float]]] = {}
    vectors: List[Optional[List[float]]] = []
    for item in questions:
        text = item["question"]
        if text not in cache:
            cache[text] = _embed_one(text, user, embedder)
        vectors.append(cache[text])
    result["embedded"] = sum(1 for v in vectors if v)

    if result["embedded"] == 0:
        result["error"] = "embedding 不可用，缺口聚类已跳过（不影响其他区块）"
        return result

    clusters = cluster_indices(vectors, threshold)
    valid = [c for c in clusters if len(c) >= min_cluster]
    valid.sort(key=len, reverse=True)
    valid = valid[:max_clusters]

    for members in valid:
        member_questions = [questions[i]["question"] for i in members]
        entry: Dict[str, Any] = {
            "size": len(members),
            "questions": member_questions[:5],
            "sample_questions": member_questions[:3],
            "user_count": len({questions[i]["user_id"] for i in members}),
            "draft_item_id": None,
            "draft_title": None,
            "draft_error": None,
            "reason": f"{len(members)} 条相似问题均未能被知识库回答",
        }
        if create_drafts:
            draft = _generate_draft(db, member_questions, user, llm)
            if draft:
                entry["draft_item_id"] = draft["item_id"]
                entry["draft_title"] = draft["title"]
            else:
                entry["draft_error"] = "模型不可用或输出不可解析，未生成草稿"
        result["clusters"].append(entry)

    return result


DRAFT_SYSTEM_PROMPT = """你是企业知识库编辑。用户反复提出但知识库答不上来的问题已聚类给你。
请据此写一条**草稿知识条目**的骨架，供人工复核后发布。

要求：
1. 标题是"动作 + 对象"的短句（如「校招出差申请提交」），不超过 30 字；
2. 步骤要具体、可执行、按先后顺序，3~6 条；**只写你确信的通用流程**，
   不确定的细节不要编造（宁可少写）；
3. 涉及的系统/物料已知就写，未知留空；
4. 注意事项只写风险点，没有就留空。

只输出 JSON，不要任何解释：
{"title": "...", "steps": ["...", "..."], "system_or_material": "...", "caution": "..."}"""


def _generate_draft(
    db: Session, questions: Sequence[str], user: Any, llm: Any
) -> Optional[Dict[str, Any]]:
    """为一个缺口簇生成草稿条目（模型失败返回 None——不建空壳草稿）

    硬约束：**只能落 `status=draft`**。`knowledge_service.create_item` 只在
    `published` 时才写向量库，所以草稿天然不会进入检索、不会被问答引用。
    """
    if db is None or not llm or not questions:
        return None
    title = ""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        sample = "\n".join(f"- {q}" for q in list(questions)[:DRAFT_QUESTION_SAMPLE])
        messages = [
            SystemMessage(content=DRAFT_SYSTEM_PROMPT),
            HumanMessage(content=f"未答问题（同义聚类）：\n{sample}"),
        ]
        data = _invoke_json(llm, messages, _config_int("AGENT_PATROL_LLM_TIMEOUT", 30))
        if not isinstance(data, dict):
            return None
        title = str(data.get("title") or "").strip()
        if not title:
            return None
        steps = [
            str(s).strip()
            for s in (data.get("steps") or [])
            if isinstance(s, (str, int, float)) and str(s).strip()
        ]
        from app.schemas.knowledge import KnowledgeItemCreate
        from app.services import knowledge_service

        caution_bits = [str(data.get("caution") or "").strip()] if data.get("caution") else []
        caution_bits.append(
            "【主动巡检生成·待人工确认】来源："
            f"{len(questions)} 条相似未答问题（示例：{' / '.join(list(questions)[:2])}）"
        )
        item = knowledge_service.create_item(
            db,
            user,
            KnowledgeItemCreate(
                title=title[:DRAFT_TITLE_MAX],
                steps=steps or None,
                system_or_material=(str(data.get("system_or_material") or "").strip() or None),
                caution="\n".join(caution_bits),
                status="draft",
            ),
        )
        return {"item_id": item.id, "title": item.title, "status": item.status}
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 缺口草稿生成失败（{title or '未命名'}）: {exc}")
        return None


# ---------------------------------------------------------------------------
# ④ 一致性检查（流程节点描述 ↔ 关联条目，需模型判定）
# ---------------------------------------------------------------------------

CONSISTENCY_SYSTEM_PROMPT = """你在做企业知识库的一致性校验。
给定一个流程节点的描述与它关联的知识条目内容，判断两者是否存在**实质性矛盾**
（如步骤顺序相反、要求不一致、条件冲突、物料不同）。

注意：只是"表述详略不同"或"条目更详细"不算矛盾。不要吹毛求疵。

只输出 JSON：{"conflict": true/false, "reason": "一句话说明冲突点（无冲突则留空）"}"""


def check_consistency(
    db: Session,
    user: Any,
    *,
    llm: Any = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """检查流程节点描述与其关联条目是否矛盾（LLM 判定，需人工裁决）"""
    issues: List[Dict[str, Any]] = []
    if db is None or not llm:
        return issues
    cap = max(1, limit if limit is not None else _config_int(
        "AGENT_PATROL_CONSISTENCY_MAX_PAIRS", 20
    ))
    try:
        from app.models import Flow, FlowNode
        from app.services import knowledge_service

        rows = (
            db.query(FlowNode)
            .filter(FlowNode.description.isnot(None), FlowNode.related_item_ids.isnot(None))
            .limit(cap * 3)
            .all()
        )
        checked = 0
        for node in rows:
            if checked >= cap:
                break
            node_desc = (node.description or "").strip()
            related = node.related_item_ids or []
            if not node_desc or not isinstance(related, list) or not related:
                continue
            flow = db.query(Flow).filter(Flow.id == node.flow_id).first()
            for item_id in related[:3]:
                if checked >= cap:
                    break
                item = knowledge_service.get_item(db, str(item_id), user)
                if item is None or item.status != "published":
                    continue
                checked += 1
                verdict = _judge_conflict(llm, node, node_desc, item)
                if not verdict:
                    continue
                issues.append(
                    {
                        "flow_id": node.flow_id,
                        "flow_name": getattr(flow, "name", "") if flow else "",
                        "node_id": node.id,
                        "node_name": node.name or "",
                        "node_description": node_desc[:400],
                        "item_id": item.id,
                        "item_title": item.title or "",
                        "reason": verdict,
                    }
                )
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 一致性检查失败（跳过该区块）: {exc}")
    return issues


def _judge_conflict(llm: Any, node: Any, node_desc: str, item: Any) -> str:
    """让模型判定一处冲突（返回冲突说明；无冲突/失败返回空串）"""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        item_text = ""
        try:
            item_text = item.to_search_text()
        except Exception:  # noqa: BLE001
            item_text = f"标题：{item.title}"
        messages = [
            SystemMessage(content=CONSISTENCY_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"流程节点：{node.name or ''}\n节点描述：{node_desc[:800]}\n\n"
                    f"关联条目：\n{item_text[:1500]}"
                )
            ),
        ]
        data = _invoke_json(llm, messages, _config_int("AGENT_PATROL_LLM_TIMEOUT", 30))
        if not isinstance(data, dict) or not data.get("conflict"):
            return ""
        return str(data.get("reason") or "模型判定存在冲突").strip()[:500]
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 一致性判定失败（忽略该处）: {exc}")
        return ""


# ---------------------------------------------------------------------------
# LLM 调用工具（带软超时 + 容忍 ```json 包裹）
# ---------------------------------------------------------------------------

def _invoke_json(llm: Any, messages: Sequence[Any], timeout: int) -> Optional[Dict[str, Any]]:
    """调模型并解析出 JSON 对象（超时/解析失败一律返回 None）"""
    if llm is None:
        return None
    try:
        response = _invoke_with_timeout(llm, messages, timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 巡检模型调用失败（降级为规则）: {exc}")
        return None
    if response is None:
        return None
    return _extract_json(getattr(response, "content", "") or "")


def _invoke_with_timeout(llm: Any, messages: Sequence[Any], timeout: int):
    """带软超时的 LLM 调用（超时返回 None，不让巡检卡死）"""
    if not timeout or timeout <= 0:
        return llm.invoke(messages)

    box: Dict[str, Any] = {}

    def _run() -> None:
        try:
            box["msg"] = llm.invoke(messages)
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    if "err" in box:
        raise box["err"]
    return box.get("msg")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抽 JSON（容忍 ```json 包裹与前后废话）"""
    if not text:
        return None
    raw = text.strip()
    match = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if match:
        raw = match.group(1).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(raw[start : end + 1])
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# 巡检主流程
# ---------------------------------------------------------------------------

def _resolve_llm(user: Any, llm: Any) -> Any:
    """解析模型对象

    - `llm=False` → 显式禁用（离线/测试：只跑规则检查）
    - `llm=None`  → 自动按用户配置创建（失败返回 None）
    - 其他        → 原样使用（注入的假模型）
    """
    if llm is False:
        return None
    if llm is not None:
        return llm
    try:
        from app.services import llm_service

        return llm_service.get_llm_for_user(user)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 巡检未取得模型（仅做规则检查）: {exc}")
        return None


def _safe(section: str, fn: Callable[[], Any], errors: Dict[str, str], default: Any) -> Any:
    """分区兜底：某一段失败只记录原因并返回该段默认值，绝不冒泡"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        errors[section] = f"{type(exc).__name__}: {exc}"
        print(f"[提示] 巡检区块 {section} 失败: {exc}")
        return default


def run_patrol(
    db: Session,
    user: Any,
    *,
    scope: str = "",
    create_drafts: Optional[bool] = None,
    embedder: Optional[Callable[[str], Any]] = None,
    llm: Any = None,
) -> Dict[str, Any]:
    """跑一轮巡检（手动触发 / 定时任务都调它）

    四个检查器**分段兜底**，产出报告与草稿；任何一段失败只会让该段缺席
    （报告状态降级为 `partial`），不会让整轮巡检失败。
    """
    started = time.perf_counter()
    if not enabled():
        return {
            "ok": False,
            "error": "巡检总开关已关闭（AGENT_PATROL_ENABLED=false），未执行",
            "scope": resolve_scope(user, scope),
        }
    if db is None or user is None:
        return {"ok": False, "error": "巡检缺少数据库会话或用户上下文", "scope": "me"}

    resolved_scope = resolve_scope(user, scope)
    scope_user_id = _scope_user_id(user, resolved_scope)
    do_drafts = (
        _config_bool("AGENT_PATROL_DRAFT_ENABLED", True) if create_drafts is None else bool(create_drafts)
    )
    model = _resolve_llm(user, llm)
    errors: Dict[str, str] = {}

    stale_items = _safe(
        "stale_items",
        lambda: check_stale(db, scope_user_id=scope_user_id),
        errors,
        [],
    )
    gap_result = _safe(
        "gap_clusters",
        lambda: check_gaps(
            db,
            user,
            scope_user_id=scope_user_id,
            embedder=embedder,
            llm=model,
            create_drafts=do_drafts,
        ),
        errors,
        {"clusters": [], "scanned": 0, "embedded": 0, "error": ""},
    )
    if gap_result.get("error"):
        errors.setdefault("gap_clusters", gap_result["error"])
    quality_issues = _safe(
        "quality_issues",
        lambda: check_quality(db, user, scope_user_id=scope_user_id),
        errors,
        [],
    )
    consistency_issues: List[Dict[str, Any]] = []
    # 本轮"实际尝试跑"的区块：某区块整体崩溃时用它算"是不是全挂了"
    attempted = ["stale_items", "gap_clusters", "quality_issues"]
    if _config_bool("AGENT_PATROL_CONSISTENCY_ENABLED", False) and model is not None:
        attempted.append("consistency_issues")
        consistency_issues = _safe(
            "consistency_issues",
            lambda: check_consistency(db, user, llm=model),
            errors,
            [],
        )

    gap_clusters = gap_result.get("clusters") or []
    drafts_created = sum(1 for c in gap_clusters if c.get("draft_item_id"))
    counts = {
        "stale": len(stale_items),
        "gap_clusters": len(gap_clusters),
        "gap_questions_scanned": gap_result.get("scanned", 0),
        "gap_questions_embedded": gap_result.get("embedded", 0),
        "drafts_created": drafts_created,
        "quality": len(quality_issues),
        "consistency": len(consistency_issues),
        "failed_sections": len(errors),
    }
    duration_ms = int((time.perf_counter() - started) * 1000)
    counts["duration_ms"] = duration_ms

    summary = _build_summary(resolved_scope, counts, model)
    status = "error" if len(errors) >= len(attempted) else ("partial" if errors else "ok")

    report_id = _save_report(
        db,
        user_id=getattr(user, "id", None),
        scope=resolved_scope,
        status=status,
        stale_items=stale_items,
        gap_clusters=gap_clusters,
        quality_issues=quality_issues,
        consistency_issues=consistency_issues,
        summary=summary,
        counts=counts,
        duration_ms=duration_ms,
        errors=errors,
    )

    return {
        "ok": True,
        "report_id": report_id,
        "scope": resolved_scope,
        "status": status,
        "summary": summary,
        "counts": counts,
        "stale_items": stale_items,
        "gap_clusters": gap_clusters,
        "quality_issues": quality_issues,
        "consistency_issues": consistency_issues,
        "errors": errors,
        "duration_ms": duration_ms,
    }


def _build_summary(scope: str, counts: Dict[str, Any], llm: Any) -> str:
    """一句话总览：优先让模型写，失败/无模型时退回确定性的规则文案"""
    scope_label = "全站" if scope == "all" else "我的内容"
    rule_text = (
        f"本次巡检（{scope_label}）：过期待复核 {counts['stale']} 条、"
        f"知识缺口 {counts['gap_clusters']} 簇（已生成草稿 {counts['drafts_created']} 条）、"
        f"质量异常 {counts['quality']} 条、一致性冲突 {counts['consistency']} 条。"
    )
    if llm is None:
        return rule_text
    if counts["stale"] + counts["gap_clusters"] + counts["quality"] + counts["consistency"] == 0:
        return rule_text + " 未发现需要处理的问题。"
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [
            SystemMessage(
                content=(
                    "你是知识库运维助手。根据巡检统计写一句不超过 80 字的中文总览，"
                    "指出最该优先处理的一件事，语气克制、不要夸张。只输出这一句话。"
                )
            ),
            HumanMessage(content=json.dumps(counts, ensure_ascii=False)),
        ]
        response = _invoke_with_timeout(llm, messages, _config_int("AGENT_PATROL_LLM_TIMEOUT", 30))
        text = (getattr(response, "content", "") or "").strip() if response is not None else ""
        if text:
            return text[:200]
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 巡检总览生成失败（退回规则文案）: {exc}")
    return rule_text


def _save_report(db: Session, **fields: Any) -> Optional[str]:
    """落巡检报告（失败返回 None，不影响已有结果返回）"""
    try:
        from app.models import PatrolReport

        row = PatrolReport(
            user_id=fields["user_id"],
            scope=fields["scope"],
            status=fields["status"],
            stale_items=fields["stale_items"],
            gap_clusters=fields["gap_clusters"],
            quality_issues=fields["quality_issues"],
            consistency_issues=fields["consistency_issues"],
            summary=fields["summary"],
            counts=fields["counts"],
            duration_ms=fields["duration_ms"],
            error=fields["errors"] or None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        _trim_reports(db)
        return row.id
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[提示] 巡检报告落库失败（结果仍返回给调用方）: {exc}")
        return None


def _trim_reports(db: Session) -> None:
    """只保留最近 N 份报告，防止表无界增长"""
    keep = _config_int("AGENT_PATROL_REPORT_KEEP", 200)
    if keep <= 0:
        return
    try:
        from app.models import PatrolReport

        stale = (
            db.query(PatrolReport.id)
            .order_by(PatrolReport.run_at.desc())
            .offset(keep)
            .all()
        )
        ids = [row[0] for row in stale]
        if ids:
            db.query(PatrolReport).filter(PatrolReport.id.in_(ids)).delete(
                synchronize_session=False
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[提示] 巡检报告清理失败（忽略）: {exc}")


# ---------------------------------------------------------------------------
# 报告查询
# ---------------------------------------------------------------------------

def serialize_report(row: Any, with_detail: bool = True) -> Dict[str, Any]:
    """报告序列化（`with_detail=False` 用于列表页，只给计数与总览）"""
    counts = getattr(row, "counts", None) or {}
    data: Dict[str, Any] = {
        "id": row.id,
        "user_id": row.user_id,
        "scope": row.scope,
        "status": row.status,
        "summary": row.summary or "",
        "counts": counts,
        "duration_ms": row.duration_ms or 0,
        "errors": getattr(row, "error", None) or {},
        "run_at": row.run_at.isoformat() if row.run_at else None,
    }
    if with_detail:
        data["stale_items"] = getattr(row, "stale_items", None) or []
        data["gap_clusters"] = getattr(row, "gap_clusters", None) or []
        data["quality_issues"] = getattr(row, "quality_issues", None) or []
        data["consistency_issues"] = getattr(row, "consistency_issues", None) or []
    return data


def list_reports(
    db: Session, *, skip: int = 0, limit: int = 20, scope: str = ""
) -> Dict[str, Any]:
    """巡检报告列表（新的在前）"""
    try:
        from app.models import PatrolReport

        query = db.query(PatrolReport)
        if scope:
            query = query.filter(PatrolReport.scope == scope)
        total = query.count()
        rows = (
            query.order_by(PatrolReport.run_at.desc())
            .offset(max(0, skip))
            .limit(max(1, limit))
            .all()
        )
        return {"total": total, "items": [serialize_report(r, with_detail=False) for r in rows]}
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 巡检报告列表查询失败: {exc}")
        return {"total": 0, "items": [], "error": str(exc)}


def get_report(db: Session, report_id: str) -> Optional[Dict[str, Any]]:
    """单份报告详情（含四个区块的完整内容）"""
    try:
        from app.models import PatrolReport

        row = db.query(PatrolReport).filter(PatrolReport.id == report_id).first()
        return serialize_report(row, with_detail=True) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 巡检报告详情查询失败: {exc}")
        return None
