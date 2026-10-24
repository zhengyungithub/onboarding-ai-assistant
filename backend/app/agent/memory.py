"""三层记忆（改造 4：短期 → 三层记忆）

改造前只有「最近 4 轮原文拼进 prompt」，这套机制有三个明确的失效场景：

| 失效场景 | 归因 | 本模块的解法 |
|---------|------|-------------|
| 会话开到第 12 轮，第 3 轮定下的结论已经滑出窗口 | L1 窗口太短 | L1 拉到 6 轮 + **L1.5 滚动摘要**（更早的对话压成一段摘要常驻） |
| "20 轮前提过的那个细节"想不起来 | 窗口是"时间近"，不是"语义近" | **L1.6 检索式历史**：历史消息向量化，按当前问题召回 top-3 语义相关片段 |
| 同一个用户反复问同一类业务，系统每次从零开始 | 没有跨会话的用户画像 | **L2 用户长期偏好**：规则抽取常用流程/系统/主题，作为**检索提示**参与查询扩展 |
| 知识库答不上来的问题没沉淀 | 缺组织级记忆 | **L3 未答问题**：定稿时顺手采集（零额外成本） |

四条设计红线（都为了"改造不劣化"）：

1. **关闭即归零**：`AGENT_MEMORY_ENABLED=false` 时短期轮数严格回到 4 轮、
   不注入任何块、不召回、不抽取、不采集——与改造前一字不差（有测试守护）。
2. **永不抛异常**：所有入口 try/except 兜底，任何失败都退化为"这一层没生效"。
   记忆是**加分项**，绝不能反过来弄挂问答。
3. **不阻塞用户**：摘要生成、历史索引、偏好抽取全部丢到守护线程里做；
   读侧（召回/偏好）有硬超时与阈值，读不到就当没有。
4. **L2 只作检索提示，不进 system prompt 全文**：省 token，也避免"用户画像"
   变成新的幻觉源——它只影响**去哪找**，不影响**答案怎么写**。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.config import settings

# 与改造前完全一致的短期轮数：关闭记忆时必须严格用它，保证行为零变化
SHORT_TURNS_LEGACY = 4
# 检索式历史的独立 Qdrant collection（与知识条目解耦，互不影响）
CHAT_HISTORY_COLLECTION = "chat_history"

# 偏好抽取时过滤掉的通用词（这些词出现频率高但没有指向性）
_TOPIC_STOPWORDS = {
    "的", "了", "是", "在", "有", "和", "就", "不", "人", "都", "一", "一个",
    "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
    "什么", "怎么", "如何", "为什么", "可以", "吗", "呢", "吧", "啊", "哦", "嗯",
    "你好", "您好", "谢谢", "感谢", "麻烦", "请问", "帮我", "帮忙", "一下",
    "知道", "了解", "明白", "清楚", "对的", "是的", "没错", "我", "自己",
    "这个", "那个", "这些", "那些", "时候", "地方", "东西", "事情", "问题",
    "需要", "应该", "能不能", "是否", "还有", "以及", "或者", "但是", "如果",
    "今天", "明天", "昨天", "现在", "以后", "之前", "之后", "上边", "下边",
}
# 「XX系统 / XX平台」这类实体名的识别（L2 偏好里最有检索价值的一类词）
_SYSTEM_PATTERN = re.compile(r"([\u4e00-\u9fa5A-Za-z]{2,6}(?:系统|平台|网站|小程序|APP|app))")
# 单条召回片段的最小长度：太短的片段（如"好的""嗯"）没有注入价值
_RECALL_MIN_CHARS = 8


# ---------------------------------------------------------------------------
# 开关与口径
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """记忆总开关（关闭时全链路归零，一键回滚）"""
    return bool(getattr(settings, "AGENT_MEMORY_ENABLED", True))


def short_turns() -> int:
    """L1 短期窗口轮数：关闭记忆时严格返回改造前的 4 轮"""
    if not enabled():
        return SHORT_TURNS_LEGACY
    try:
        value = int(getattr(settings, "AGENT_MEMORY_SHORT_TURNS", SHORT_TURNS_LEGACY))
    except Exception:  # noqa: BLE001
        return SHORT_TURNS_LEGACY
    # 下限 4：比改造前更短的窗口没有意义（那不是升级）
    return max(SHORT_TURNS_LEGACY, value)


def summary_enabled() -> bool:
    return enabled() and bool(getattr(settings, "AGENT_MEMORY_SUMMARY_TRIGGER_TURNS", 12) or 0)


def recall_enabled() -> bool:
    return enabled() and bool(getattr(settings, "AGENT_MEMORY_RECALL_ENABLED", True))


def preference_enabled() -> bool:
    return enabled() and bool(getattr(settings, "AGENT_MEMORY_PREFERENCE_TERMS", 5) or 0)


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


def mode_info() -> Dict[str, Any]:
    """记忆配置摘要（并入 /api/agent/status，便于运维确认与回滚）"""
    return {
        "memory_enabled": enabled(),
        "short_turns": short_turns(),
        "summary_enabled": summary_enabled(),
        "summary_trigger_turns": _config_int("AGENT_MEMORY_SUMMARY_TRIGGER_TURNS", 12),
        "summary_interval_turns": _config_int("AGENT_MEMORY_SUMMARY_INTERVAL_TURNS", 6),
        "recall_enabled": recall_enabled(),
        "recall_top_k": _config_int("AGENT_MEMORY_RECALL_TOP_K", 3),
        "recall_min_score": _config_float("AGENT_MEMORY_RECALL_MIN_SCORE", 0.55),
        "preference_enabled": preference_enabled(),
        "preference_terms": _config_int("AGENT_MEMORY_PREFERENCE_TERMS", 5),
    }


# ---------------------------------------------------------------------------
# L2：用户长期偏好（规则抽取，零 token）
# ---------------------------------------------------------------------------

def _as_data(value: Any) -> Dict[str, List[Dict[str, Any]]]:
    """把 user_memory.data 规范化成 {flows:[], systems:[], topics:[]}"""
    data = value if isinstance(value, dict) else {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key in ("flows", "systems", "topics"):
        items = data.get(key)
        out[key] = [i for i in items if isinstance(i, dict) and i.get("term")] if isinstance(items, list) else []
    return out


def _merge_term(bucket: List[Dict[str, Any]], term: str, cap: int = 30) -> None:
    """把一个词累加进桶里（命中即 count+1 并更新时间）"""
    term = (term or "").strip()
    if not term:
        return
    for item in bucket:
        if item.get("term") == term:
            item["count"] = int(item.get("count") or 0) + 1
            item["last_at"] = datetime.now().isoformat(timespec="seconds")
            return
    bucket.append(
        {"term": term, "count": 1, "last_at": datetime.now().isoformat(timespec="seconds")}
    )
    # 桶内按热度保留前 cap 个，防止无限增长
    bucket.sort(key=lambda x: int(x.get("count") or 0), reverse=True)
    del bucket[cap:]


def topic_terms(question: str) -> List[str]:
    """从问题里抽主题词（jieba 分词 + 停用词过滤，不做同义词扩展）

    刻意不复用 `chat_service._extract_keywords`：那个函数带同义词扩展
    （"工资"→"薪资/薪水/薪酬"），用于检索是对的，但作为"用户偏好词"
    会把同义词一起算成偏好，虚高且没有指向性。
    """
    text = (question or "").strip()
    if not text:
        return []
    try:
        import jieba

        words = jieba.lcut(text)
    except Exception:  # noqa: BLE001 —— 分词不可用时退化为字面切分
        words = re.split(r"[\s，,。.！!？?、；;：:（）()【】\[\]]+", text)
    terms: List[str] = []
    for raw in words:
        word = (raw or "").strip()
        if len(word) < 2 or len(word) > 8 or word in _TOPIC_STOPWORDS:
            continue
        if word.isdigit():
            continue
        if word not in terms:
            terms.append(word)
    return terms[:5]


def _system_terms(question: str) -> List[str]:
    """抽取「XX系统 / XX平台」类实体名（白名单 + 正则双路）"""
    text = question or ""
    found: List[str] = []
    whitelist = [t.strip() for t in (settings.AGENT_MEMORY_SYSTEM_TERMS or "").split(",") if t.strip()]
    for term in whitelist:
        if term in text and term not in found:
            found.append(term)
    for match in _SYSTEM_PATTERN.findall(text):
        if match not in found:
            found.append(match)
    return found[:3]


def _flow_terms(db: Session, question: str) -> List[str]:
    """用知识库里的流程名做识别（流程名是最强的"业务实体"信号）"""
    try:
        from app.models import Flow

        rows = db.query(Flow.name).all()
    except Exception:  # noqa: BLE001
        return []
    found: List[str] = []
    for (name,) in rows:
        name = (name or "").strip()
        # 流程名通常较长（"校园招聘流程"），要求长度 ≥2 且在问题里出现
        if len(name) >= 2 and name in (question or ""):
            found.append(name)
    return found[:3]


def observe(
    db: Session,
    user_id: str,
    question: str,
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """一轮结束后更新用户偏好（同步实现，调用方负责丢线程）

    抽取规则是**纯规则**（分词 + 流程名/系统名匹配），不调模型：
    偏好是每轮都要做的高频动作，任何 LLM 成本在这里都会被放大。
    """
    if not preference_enabled() or db is None or not user_id:
        return
    try:
        from app.models import UserMemory

        flows = _flow_terms(db, question)
        systems = _system_terms(question)
        topics = topic_terms(question)
        if not (flows or systems or topics):
            return

        row = db.query(UserMemory).filter(UserMemory.user_id == user_id).first()
        data = _as_data(row.data if row else None)
        for term in flows:
            _merge_term(data["flows"], term)
        for term in systems:
            _merge_term(data["systems"], term)
        for term in topics:
            _merge_term(data["topics"], term)

        if row is None:
            row = UserMemory(user_id=user_id, data=data)
            db.add(row)
        else:
            row.data = data
            row.updated_at = datetime.now()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[提示] 用户偏好抽取失败（不影响问答）: {exc}")


def observe_async(user_id: str, question: str) -> None:
    """把偏好抽取丢到守护线程（不阻塞用户响应）"""
    if not preference_enabled() or not user_id:
        return

    def _worker() -> None:
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            observe(db, user_id, question)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 偏好抽取线程失败: {exc}")
        finally:
            db.close()

    _spawn(_worker, "agent-memory-observe")


def preference_terms(db: Session, user_id: str, limit: Optional[int] = None) -> List[str]:
    """取出可作为**检索提示**的偏好词（只取流程名与系统名两类）

    为什么排除 topics：主题词（"出差""报销"）太泛，参与查询扩展会把
    检索范围拉散、降低精度；流程名/系统名是强实体，扩展它们是净收益。
    """
    if not preference_enabled() or db is None or not user_id:
        return []
    cap = limit if limit is not None else _config_int("AGENT_MEMORY_PREFERENCE_TERMS", 5)
    min_count = max(1, _config_int("AGENT_MEMORY_PREFERENCE_MIN_COUNT", 2))
    try:
        from app.models import UserMemory

        row = db.query(UserMemory).filter(UserMemory.user_id == user_id).first()
        data = _as_data(row.data if row else None)
        terms: List[str] = []
        for key in ("flows", "systems"):
            for item in sorted(data[key], key=lambda x: int(x.get("count") or 0), reverse=True):
                if int(item.get("count") or 0) < min_count:
                    continue
                term = str(item.get("term") or "")
                if term and term not in terms:
                    terms.append(term)
        return terms[: max(0, cap)]
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 读取用户偏好失败（忽略）: {exc}")
        return []


def preference_snapshot(db: Session, user_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """完整偏好（供 API 展示，含 topics）"""
    if db is None or not user_id:
        return {"flows": [], "systems": [], "topics": []}
    try:
        from app.models import UserMemory

        row = db.query(UserMemory).filter(UserMemory.user_id == user_id).first()
        return _as_data(row.data if row else None)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 读取用户记忆失败: {exc}")
        return {"flows": [], "systems": [], "topics": []}


def clear_preference(db: Session, user_id: str) -> bool:
    """清空某用户的长期偏好（用户可自助）"""
    try:
        from app.models import UserMemory

        deleted = (
            db.query(UserMemory).filter(UserMemory.user_id == user_id).delete(synchronize_session=False)
        )
        db.commit()
        return bool(deleted)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[提示] 清空用户记忆失败: {exc}")
        return False


# ---------------------------------------------------------------------------
# L1.5：会话摘要（滚动 + 增量）
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """你是一个对话记忆压缩器。请把「此前的摘要」与「新增对话」合并成一份新的对话摘要。

要求：
1. 保留**事实性结论**：用户确认过的业务结论、关键数字、时间点、涉及的系统/流程名称
2. 保留**用户诉求与未解决的问题**（用户问过但没得到明确答案的）
3. 删除寒暄、重复表达、以及过程性的试探
4. 用第三人称记录（"用户…"），不要写"我"
5. 直接输出摘要正文，不要标题、不要任何解释或引号，控制在 {max_chars} 字以内

此前的摘要：
{previous}

新增对话：
{dialog}

请输出合并后的摘要："""


def _history_rows(db: Session, user_id: str, limit: int) -> List[Any]:
    """按时间正序取最近的对话消息（user/assistant，不含系统消息）"""
    from app.models import ChatMessage

    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.user_id == user_id,
            ChatMessage.role.in_(("user", "assistant")),
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return rows


def count_messages(db: Session, user_id: str) -> int:
    """该用户累计的对话消息条数（user + assistant）"""
    from app.models import ChatMessage

    return (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user_id, ChatMessage.role.in_(("user", "assistant")))
        .count()
    )


def _recent_message_ids(db: Session, user_id: str, limit: int) -> set:
    """最近 N 条消息的 id（用于召回时排除"已经在原文窗口里"的片段）"""
    if db is None or not user_id or limit <= 0:
        return set()
    try:
        return {str(getattr(row, "id", "")) for row in _history_rows(db, user_id, limit) if getattr(row, "id", None)}
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 读取最近消息 id 失败（忽略）: {exc}")
        return set()


def _render_dialog(rows: List[Any], per_message: int = 400) -> str:
    lines = []
    for row in rows:
        who = "用户" if row.role == "user" else "助手"
        content = (row.content or "").replace("\n", " ").strip()
        if len(content) > per_message:
            content = content[:per_message] + "…"
        lines.append(f"{who}：{content}")
    return "\n".join(lines)


def refresh_summary(db: Session, user: Any, force: bool = False) -> Optional[Any]:
    """按需生成/增量刷新会话摘要（同步实现，调用方负责丢线程）

    触发规则（两道闸，保证正常会话零成本）：
    - 累计消息数 < `AGENT_MEMORY_SUMMARY_TRIGGER_TURNS × 2` → 完全不触发；
    - 距上次摘要未新增 `AGENT_MEMORY_SUMMARY_INTERVAL_TURNS × 2` 条 → 不刷新。

    只摘要「最近 `short_turns` 轮之前」的部分——窗口内的原文仍然原样进 prompt，
    摘要负责的是更早的结论，两者不重叠、不重复喂给模型。
    """
    from app.models import ChatMessage, ConversationSummary

    if not summary_enabled() or db is None or user is None:
        return None
    user_id = getattr(user, "id", None)
    if not user_id:
        return None

    trigger_msgs = _config_int("AGENT_MEMORY_SUMMARY_TRIGGER_TURNS", 12) * 2
    interval_msgs = _config_int("AGENT_MEMORY_SUMMARY_INTERVAL_TURNS", 6) * 2
    keep_raw = short_turns() * 2
    max_chars = _config_int("AGENT_MEMORY_SUMMARY_MAX_CHARS", 800)

    total = count_messages(db, user_id)
    if total < trigger_msgs:
        return None

    end = total - keep_raw
    if end <= 0:
        return None

    row = db.query(ConversationSummary).filter(ConversationSummary.user_id == user_id).first()
    covered = int(row.covered_message_count or 0) if row else 0
    if covered >= end:
        return row  # 窗口滑动导致水位线超过了当前边界，没什么可摘要的
    if not force and (end - covered) < interval_msgs:
        return row

    # 取 [0, end) 区间里"尚未被摘要覆盖"的那一段
    rows = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.user_id == user_id,
            ChatMessage.role.in_(("user", "assistant")),
        )
        .order_by(ChatMessage.created_at.asc())
        .offset(covered)
        .limit(end - covered)
        .all()
    )
    if not rows:
        return row

    previous = (row.summary if row else "") or "（无，这是第一次摘要）"
    prompt = SUMMARY_PROMPT.format(
        max_chars=max_chars, previous=previous, dialog=_render_dialog(rows)
    )

    text = ""
    model_name = ""
    error = ""
    try:
        from app.services.llm_service import get_llm_for_user

        llm = get_llm_for_user(user)
        model_name = str(getattr(llm, "model_name", "") or getattr(llm, "model", "") or "")
        resp = llm.invoke(prompt)
        text = (getattr(resp, "content", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 —— 摘要失败只是"这层没生效"，不影响问答
        error = f"{type(exc).__name__}: {exc}"
        print(f"[提示] 会话摘要生成失败（沿用旧摘要）: {error}")
        text = ""

    if not text:
        # 生成失败不动水位线：下次有新增消息时再试一次
        return row

    if len(text) > max_chars:
        text = text[:max_chars] + "…"

    if row is None:
        row = ConversationSummary(user_id=user_id, summary=text, covered_message_count=end)
        db.add(row)
    else:
        row.summary = text
        row.covered_message_count = end
    row.model = model_name or None
    row.error = error or None
    row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)
    return row


def summary_text(db: Session, user_id: str) -> str:
    """读取当前会话摘要（读侧，失败返回空串）"""
    if not summary_enabled() or db is None or not user_id:
        return ""
    try:
        from app.models import ConversationSummary

        row = db.query(ConversationSummary).filter(ConversationSummary.user_id == user_id).first()
        return (row.summary or "") if row else ""
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 读取会话摘要失败（忽略）: {exc}")
        return ""


# ---------------------------------------------------------------------------
# L1.6：检索式历史（Qdrant chat_history）
# ---------------------------------------------------------------------------

def _spawn(target, name: str) -> None:
    """守护线程（失败不影响主流程）"""
    try:
        threading.Thread(target=target, daemon=True, name=name).start()
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 后台线程启动失败（忽略）: {exc}")


def _ensure_chat_history_collection(dense_size: int) -> None:
    """幂等创建 chat_history collection（维度跟随实际 embedding 模型）

    与知识条目集合完全解耦：这里只放 dense 向量（历史召回是"语义相似"，
    不需要 BM25 精确匹配），出问题也污染不到知识库检索。
    """
    from qdrant_client import models

    from app.services import qdrant_service

    client = qdrant_service.get_qdrant_client()
    names = {c.name for c in client.get_collections().collections}
    if CHAT_HISTORY_COLLECTION not in names:
        client.create_collection(
            collection_name=CHAT_HISTORY_COLLECTION,
            vectors_config={
                "dense": models.VectorParams(size=dense_size, distance=models.Distance.COSINE)
            },
        )
    try:
        client.create_payload_index(
            collection_name=CHAT_HISTORY_COLLECTION,
            field_name="user_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
    except Exception:  # noqa: BLE001 —— 已存在时忽略
        pass


def index_messages(db: Session, user: Any, message_ids: List[str]) -> int:
    """把指定消息向量化写入 chat_history（同步实现，调用方负责丢线程）

    只在**回答已持久化之后**调用，因此 payload 里的内容就是用户真正看到的文本。
    """
    if not recall_enabled() or db is None or user is None or not message_ids:
        return 0
    try:
        from qdrant_client import models

        from app.models import ChatMessage
        from app.services import qdrant_service
        from app.services.embedding_service import embed_dense

        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.id.in_(list(message_ids)), ChatMessage.user_id == user.id)
            .all()
        )
        if not rows:
            return 0

        indexed = 0
        ensured = False
        for row in rows:
            content = (row.content or "").strip()
            if len(content) < _RECALL_MIN_CHARS:
                continue
            try:
                vector = embed_dense(content, user)
            except Exception as exc:  # noqa: BLE001 —— 没配 embedding 就静默跳过
                print(f"[提示] 历史消息向量化失败（跳过召回）: {exc}")
                return indexed
            if not vector:
                return indexed
            if not ensured:  # 集合只需确保一次（维度取实际向量长度）
                _ensure_chat_history_collection(len(vector))
                ensured = True
            qdrant_service.get_qdrant_client().upsert(
                collection_name=CHAT_HISTORY_COLLECTION,
                points=[
                    models.PointStruct(
                        id=row.id,
                        vector={"dense": vector},
                        payload={
                            "user_id": user.id,
                            "message_id": row.id,
                            "role": row.role,
                            "content": content[:1000],
                            "created_at": row.created_at.isoformat() if row.created_at else "",
                        },
                    )
                ],
            )
            indexed += 1
        return indexed
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 历史索引失败（不影响问答）: {exc}")
        return 0


def index_messages_async(user_id: str, message_ids: List[str]) -> None:
    """把历史索引丢到守护线程"""
    if not recall_enabled() or not user_id or not message_ids:
        return

    def _worker() -> None:
        from app.db.session import SessionLocal
        from app.models import User

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if user is not None:
                index_messages(db, user, message_ids)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 历史索引线程失败: {exc}")
        finally:
            db.close()

    _spawn(_worker, "agent-memory-index")


def recall(
    db: Session,
    user: Any,
    question: str,
    *,
    exclude_message_ids: Optional[List[str]] = None,
    top_k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """按当前问题召回语义相关的历史片段（读侧，任何失败都返回空）

    `exclude_message_ids` 用于剔除"已经在窗口原文里"的消息，避免同一段
    对话既以原文出现、又以召回片段出现，白烧 token 还容易让模型重复。
    不传时自动排除"最近 `short_turns` 轮"——那部分本来就会以原文进 prompt。
    """
    if not recall_enabled() or db is None or user is None or not question:
        return []
    cap = top_k if top_k is not None else _config_int("AGENT_MEMORY_RECALL_TOP_K", 3)
    if cap <= 0:
        return []
    min_score = _config_float("AGENT_MEMORY_RECALL_MIN_SCORE", 0.55)
    exclude = {str(i) for i in (exclude_message_ids or []) if i}
    if not exclude:
        exclude = _recent_message_ids(db, getattr(user, "id", ""), short_turns() * 2)

    try:
        from qdrant_client import models

        from app.services import qdrant_service
        from app.services.embedding_service import embed_dense

        vector = embed_dense(question, user)
        if not vector:
            return []
        client = qdrant_service.get_qdrant_client()
        if CHAT_HISTORY_COLLECTION not in {c.name for c in client.get_collections().collections}:
            return []  # 还没索引过任何历史 → 没什么可召回
        resp = client.query_points(
            collection_name=CHAT_HISTORY_COLLECTION,
            query=vector,
            using="dense",
            limit=max(cap * 2, cap),  # 多取一些，剔除已在窗口内的之后再截断
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(key="user_id", match=models.MatchValue(value=user.id))
                ]
            ),
            score_threshold=min_score,
            with_payload=True,
        )
    except Exception as exc:  # noqa: BLE001 —— 召回是加分项，失败就当没有
        print(f"[提示] 历史召回失败（忽略）: {exc}")
        return []

    out: List[Dict[str, Any]] = []
    for point in getattr(resp, "points", None) or []:
        payload = point.payload or {}
        message_id = str(payload.get("message_id") or point.id)
        if message_id in exclude:
            continue
        content = (payload.get("content") or "").strip()
        if len(content) < _RECALL_MIN_CHARS:
            continue
        out.append(
            {
                "message_id": message_id,
                "role": payload.get("role") or "user",
                "content": content,
                "score": round(float(getattr(point, "score", 0) or 0), 4),
                "created_at": payload.get("created_at") or "",
            }
        )
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# 遗忘：删除对话时同步清理向量与摘要
# ---------------------------------------------------------------------------
#
# 为什么必须有这一段（真实链路冒烟才发现）：
# `chat_history` 里存的是**历史消息的向量**，而用户删掉的对话如果只从
# `chat_messages` 删掉、向量留在 Qdrant 里，下一次提问就会把"已经删掉的内容"
# 当作历史片段召回回来——既是数据残留，也是隐私问题。
#
# 边界（刻意这样切）：
# - 删单条/批量删消息 → 只删对应的向量点；
# - 清空对话 → 删该用户全部历史向量 + 会话摘要（L1 / L1.5 / L1.6 都是"会话级"记忆）；
# - **不动** L2 长期偏好与 L3 未答问题：前者是用户画像（有独立的自助清空接口），
#   后者是组织级缺口台账，都不属于"这一段对话"。

def forget_messages(
    db: Session, user: Any, message_ids: Optional[List[str]] = None, *, all_for_user: bool = False
) -> bool:
    """删除历史向量（同步实现，调用方负责丢线程）

    `all_for_user=True` 时按 `user_id` 过滤删除该用户的全部历史向量；
    否则只删 `message_ids` 对应的点——并且**必须同时匹配 user_id**，
    防止别人拿到 message id 就能删掉他人向量（越权保护）。
    返回是否真的执行了删除动作。
    """
    if not enabled() or user is None:
        return False
    user_id = getattr(user, "id", None)
    if not user_id:
        return False

    ids: List[str] = []
    if not all_for_user:
        ids = [str(i) for i in (message_ids or []) if i]
        if not ids:
            return False

    try:
        from qdrant_client import models as qmodels

        from app.services import qdrant_service

        client = qdrant_service.get_qdrant_client()
        if CHAT_HISTORY_COLLECTION not in {c.name for c in client.get_collections().collections}:
            return False

        must = [qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id))]
        if not all_for_user:
            must.append(qmodels.HasIdCondition(has_id=ids))
        client.delete(
            collection_name=CHAT_HISTORY_COLLECTION,
            points_selector=qmodels.FilterSelector(filter=qmodels.Filter(must=must)),
        )
        return True
    except Exception as exc:  # noqa: BLE001 —— 清理失败不能影响删消息本身
        print(f"[提示] 历史向量清理失败（忽略）: {exc}")
        return False


def forget_messages_async(
    user_id: str, message_ids: Optional[List[str]] = None, *, all_for_user: bool = False
) -> None:
    """把向量清理丢到守护线程（删消息是用户操作，不能等网络）"""
    if not enabled() or not user_id:
        return
    if not all_for_user and not message_ids:
        return

    def _worker() -> None:
        from app.db.session import SessionLocal
        from app.models import User

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if user is not None:
                forget_messages(db, user, message_ids, all_for_user=all_for_user)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 历史向量清理线程失败: {exc}")
        finally:
            db.close()

    _spawn(_worker, "agent-memory-forget")


def forget_summary(db: Session, user_id: str) -> bool:
    """删除会话摘要（清空对话时调用；摘要记的是"这一段对话"，不该留下来）"""
    if not enabled() or not user_id:
        return False
    try:
        from app.models import ConversationSummary

        deleted = (
            db.query(ConversationSummary)
            .filter(ConversationSummary.user_id == user_id)
            .delete(synchronize_session=False)
        )
        db.commit()
        return bool(deleted)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[提示] 会话摘要清理失败（忽略）: {exc}")
        return False


def clear_conversation_memory(db: Session, user_id: str) -> None:
    """清空一段对话带来的记忆（L1.5 摘要 + L1.6 向量）

    与"清空对话"这个用户操作绑定：用户点清空，就真的什么都不留。
    L2 偏好与 L3 缺口台账刻意保留（见本节开头的边界说明）。
    """
    if not enabled() or not user_id:
        return
    forget_summary(db, user_id)
    forget_messages_async(user_id, all_for_user=True)


# ---------------------------------------------------------------------------
# 记忆块组装（写进 prompt 的唯一出口）
# ---------------------------------------------------------------------------

MEMORY_HEADER = "【长期记忆·仅供参考】"
MEMORY_FOOTER = (
    "（以上长期记忆只用于理解上下文与你的用词习惯；"
    "回答内容必须以「知识库内容」为准，不得据此编造）"
)


def memory_block(
    db: Session,
    user: Any,
    question: str,
    *,
    exclude_message_ids: Optional[List[str]] = None,
) -> str:
    """组装要注入 prompt 的长期记忆块（关闭/为空时返回空串）

    三个来源按"信息密度"排序：会话摘要（更早的结论）→ 相关历史片段
    （语义命中的旧对话）→ 用户偏好（习惯用词）。

    每一层**各自** try/except：某一层炸了只少一段，不会连带把整块丢掉，
    更不会冒泡到问答链路上（记忆是加分项，不是失败点）。
    """
    if not enabled() or db is None or user is None:
        return ""
    user_id = getattr(user, "id", None)
    if not user_id:
        return ""

    sections: List[str] = []

    # ① 会话摘要（L1.5）
    try:
        summary = summary_text(db, user_id)
        if summary:
            sections.append(f"- 更早的对话摘要：{summary}")
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 注入会话摘要失败（忽略）: {exc}")

    # ② 相关历史片段（L1.6）
    try:
        snippets = recall(db, user, question, exclude_message_ids=exclude_message_ids)
        if snippets:
            max_chars = _config_int("AGENT_MEMORY_RECALL_MAX_CHARS", 300)
            lines = []
            for item in snippets:
                who = "你（用户）" if item.get("role") == "user" else "助手"
                content = item.get("content") or ""
                if len(content) > max_chars:
                    content = content[:max_chars] + "…"
                lines.append(f"  · {who}：{content}")
            sections.append("- 与当前问题相关的历史对话片段：\n" + "\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 注入历史片段失败（忽略）: {exc}")

    # ③ 用户偏好（L2）
    try:
        if preference_enabled():
            data = preference_snapshot(db, user_id)
            parts = []
            flows = [i["term"] for i in data["flows"][:3]]
            systems = [i["term"] for i in data["systems"][:3]]
            if flows:
                parts.append("常用流程：" + "、".join(flows))
            if systems:
                parts.append("关注系统：" + "、".join(systems))
            if parts:
                sections.append("- 你的一贯关注点：" + "；".join(parts))
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 注入用户偏好失败（忽略）: {exc}")

    if not sections:
        return ""
    return "\n".join([MEMORY_HEADER, *sections, MEMORY_FOOTER])


# ---------------------------------------------------------------------------
# L3：未答问题采集
# ---------------------------------------------------------------------------

def record_unanswered(
    db: Session,
    user_id: str,
    question: str,
    *,
    retrieved_nothing: bool = True,
    source: str = "agent",
) -> bool:
    """记录一条"知识库答不上来"的问题（失败静默）

    刻意**不做文本去重**：保持事件流形态，改造 5 的缺口聚类可以直接按行数取频率。
    """
    if not enabled() or db is None or not user_id:
        return False
    question = (question or "").strip()
    if not question:
        return False
    try:
        from app.models import UnansweredQuestion

        db.add(
            UnansweredQuestion(
                user_id=user_id,
                question=question[:1000],
                retrieved_nothing=bool(retrieved_nothing),
                source=source,
            )
        )
        db.commit()
        _trim_unanswered(db, user_id)
        return True
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[提示] 未答问题记录失败（不影响问答）: {exc}")
        return False


def _trim_unanswered(db: Session, user_id: str) -> None:
    """每个用户只保留最近 N 条，防止表无界增长"""
    keep = _config_int("AGENT_MEMORY_UNANSWERED_KEEP", 500)
    if keep <= 0:
        return
    try:
        from app.models import UnansweredQuestion

        stale = (
            db.query(UnansweredQuestion.id)
            .filter(UnansweredQuestion.user_id == user_id)
            .order_by(UnansweredQuestion.created_at.desc())
            .offset(keep)
            .all()
        )
        ids = [row[0] for row in stale]
        if ids:
            db.query(UnansweredQuestion).filter(UnansweredQuestion.id.in_(ids)).delete(
                synchronize_session=False
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[提示] 未答问题清理失败（忽略）: {exc}")


def list_unanswered(
    db: Session, *, user_id: Optional[str] = None, skip: int = 0, limit: int = 50
) -> Dict[str, Any]:
    """未答问题列表（管理员看全站，普通用户看自己的）"""
    try:
        from app.models import UnansweredQuestion

        query = db.query(UnansweredQuestion)
        if user_id:
            query = query.filter(UnansweredQuestion.user_id == user_id)
        total = query.count()
        rows = (
            query.order_by(UnansweredQuestion.created_at.desc()).offset(max(0, skip)).limit(limit).all()
        )
        return {
            "total": total,
            "items": [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "question": r.question,
                    "retrieved_nothing": bool(r.retrieved_nothing),
                    "source": r.source,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 未答问题查询失败: {exc}")
        return {"total": 0, "items": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# 一轮问答结束后的记忆维护（runtime / api 调用的统一入口）
# ---------------------------------------------------------------------------

def after_answer(
    db: Session,
    user: Any,
    question: str,
    *,
    message_ids: Optional[List[str]] = None,
    answered: bool = True,
    source: str = "agent",
) -> None:
    """一轮问答结束后的记忆维护（全部异步、全部静默）

    - L1.6：把这轮消息向量化（供后续召回）
    - L1.5：会话够长了就刷新摘要
    - L2：抽取偏好
    - L3：答不上来的问题入库

    这里刻意**不 throw 任何异常**，也不等待任何结果——它是"顺手做的事"，
    绝不能成为问答链路的失败点。
    """
    if not enabled() or user is None:
        return
    user_id = getattr(user, "id", None)
    if not user_id:
        return

    # 每一层各自兜底：任何一层炸掉都不影响其余，更不影响用户
    if not answered:
        try:
            record_unanswered(db, user_id, question, retrieved_nothing=True, source=source)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 未答问题采集失败（忽略）: {exc}")

    if message_ids:
        try:
            index_messages_async(user_id, list(message_ids))
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 历史索引调度失败（忽略）: {exc}")
    if summary_enabled():
        try:
            refresh_summary_async(user_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 摘要刷新调度失败（忽略）: {exc}")
    try:
        observe_async(user_id, question)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 偏好抽取调度失败（忽略）: {exc}")


def refresh_summary_async(user_id: str, force: bool = False) -> None:
    """把摘要刷新丢到守护线程（生成摘要是模型调用，绝不能阻塞问答）"""
    if not summary_enabled() or not user_id:
        return

    def _worker() -> None:
        from app.db.session import SessionLocal
        from app.models import User

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if user is not None:
                refresh_summary(db, user, force=force)
        except Exception as exc:  # noqa: BLE001
            print(f"[提示] 摘要刷新线程失败: {exc}")
        finally:
            db.close()

    _spawn(_worker, "agent-memory-summary")


def rebuild(db: Session, user: Any, *, force: bool = True) -> Dict[str, Any]:
    """重建某用户的记忆：摘要 + 历史索引（管理员用，可离线校准）"""
    from app.models import ChatMessage, ConversationSummary

    result: Dict[str, Any] = {"summary": False, "indexed": 0}
    if not enabled() or db is None or user is None:
        return result
    try:
        if force:
            db.query(ConversationSummary).filter(ConversationSummary.user_id == user.id).delete(
                synchronize_session=False
            )
            db.commit()
        row = refresh_summary(db, user, force=force)
        result["summary"] = bool(row and row.summary)
        ids = [
            r[0]
            for r in (
                db.query(ChatMessage.id)
                .filter(ChatMessage.user_id == user.id, ChatMessage.role.in_(("user", "assistant")))
                .order_by(ChatMessage.created_at.desc())
                .limit(200)
                .all()
            )
        ]
        ids.reverse()
        result["indexed"] = index_messages(db, user, ids)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 记忆重建失败: {exc}")
        result["error"] = str(exc)
    return result


def stats(db: Session, user_id: str) -> Dict[str, Any]:
    """记忆状态摘要（供 API/前端展示）"""
    from app.models import ConversationSummary, UnansweredQuestion

    out: Dict[str, Any] = {
        "enabled": enabled(),
        "short_turns": short_turns(),
        "messages": 0,
        "summary": "",
        "summary_covered": 0,
        "summary_updated_at": None,
        "preference": {"flows": [], "systems": [], "topics": []},
        "unanswered": 0,
        "recall_enabled": recall_enabled(),
    }
    try:
        out["messages"] = count_messages(db, user_id)
        row = db.query(ConversationSummary).filter(ConversationSummary.user_id == user_id).first()
        if row is not None:
            out["summary"] = row.summary or ""
            out["summary_covered"] = int(row.covered_message_count or 0)
            out["summary_updated_at"] = row.updated_at.isoformat() if row.updated_at else None
        out["preference"] = preference_snapshot(db, user_id)
        out["unanswered"] = (
            db.query(UnansweredQuestion).filter(UnansweredQuestion.user_id == user_id).count()
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out
