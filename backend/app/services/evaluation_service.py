"""评测服务：测试用例 CRUD + 批量评测 + 统计 + 负反馈转用例"""
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models import TestCase, EvaluationResult, ChatFeedback, KnowledgeItem, ChatMessage, User
from app.schemas.evaluation import TestCaseCreate
from app.core.roles import is_staff


class FeedbackToCaseError(Exception):
    """负反馈转测试用例的业务异常"""

    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def get_test_cases(db: Session, user_id: str, skip: int = 0, limit: int = 100) -> tuple[int, List[TestCase]]:
    query = db.query(TestCase).filter(TestCase.user_id == user_id)
    total = query.count()
    cases = query.order_by(TestCase.created_at.desc()).offset(skip).limit(limit).all()
    return total, cases


def _resolve_expected_items(
    db: Session, item_ids: Optional[List[str]], fallback_id: Optional[str] = None
) -> tuple[List[str], List[str]]:
    """校验并规整「期望条目」：去重保序、逐个确认存在，返回 (ids, titles)"""
    raw = [i for i in (item_ids or []) if i]
    if not raw and fallback_id:
        raw = [fallback_id]
    deduped: List[str] = []
    for i in raw:
        if i not in deduped:
            deduped.append(i)
    if not deduped:
        raise FeedbackToCaseError("请至少指定一个期望命中的知识条目")

    items = db.query(KnowledgeItem).filter(KnowledgeItem.id.in_(deduped)).all()
    by_id = {it.id: it for it in items}
    missing = [i for i in deduped if i not in by_id]
    if missing:
        raise FeedbackToCaseError("期望的知识条目不存在", 404)
    return deduped, [by_id[i].title or "" for i in deduped]


def case_expected(case: TestCase) -> tuple[List[str], List[str]]:
    """读取一道题的期望条目：优先多条目字段，老数据回落到单条目"""
    ids = list(case.expected_item_ids or [])
    titles = list(case.expected_item_titles or [])
    if not ids and case.expected_item_id:
        ids = [case.expected_item_id]
        titles = [case.expected_item_title or ""]
    if len(titles) < len(ids):
        titles = titles + [""] * (len(ids) - len(titles))
    return ids, titles


def create_test_case(db: Session, user_id: str, data: TestCaseCreate) -> TestCase:
    ids, titles = _resolve_expected_items(db, data.expected_item_ids, data.expected_item_id)
    case = TestCase(
        user_id=user_id,
        question=data.question,
        expected_item_id=ids[0],
        expected_item_title=titles[0],
        expected_item_ids=ids,
        expected_item_titles=titles,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


def delete_test_case(db: Session, case_id: str, user_id: str) -> bool:
    case = db.query(TestCase).filter(TestCase.id == case_id, TestCase.user_id == user_id).first()
    if not case:
        return False
    db.delete(case)
    db.commit()
    return True


# 评测检索上限：比单轮聊天的引用数宽，用于覆盖「一个问题对多个知识点」的聚合型大题
EVAL_RETRIEVAL_LIMIT = 10


def run_evaluation(db: Session, user_id: str, user) -> dict:
    """运行评测：批量跑测试集，比对命中"""
    from app.services.chat_service import search_relevant_items

    cases = db.query(TestCase).filter(
        TestCase.user_id == user_id, TestCase.enabled == True
    ).all()

    total = len(cases)
    hit_count = 0
    details = []

    for case in cases:
        exp_ids, exp_titles = case_expected(case)
        try:
            results = search_relevant_items(user, case.question, db=db, limit=EVAL_RETRIEVAL_LIMIT)
            actual_ids = [r["id"] for r in results]
            # 多期望条目：必须**全部命中**才算通过（少一个知识点就是没答全）
            matched = [i for i in exp_ids if i in actual_ids]
            missing = [i for i in exp_ids if i not in actual_ids]
            hit = not missing
            if hit:
                hit_count += 1
            details.append({
                "question": case.question,
                "expected_item_id": exp_ids[0] if exp_ids else case.expected_item_id,
                "expected_item_title": exp_titles[0] if exp_titles else (case.expected_item_title or ""),
                "expected_item_ids": exp_ids,
                "expected_item_titles": exp_titles,
                "matched_count": len(matched),
                "expected_count": len(exp_ids),
                "missing_titles": [t for i, t in zip(exp_ids, exp_titles) if i in missing],
                "actual_items": [{"id": r["id"], "title": r["title"], "score": round(r["score"], 4)} for r in results],
                "hit": hit,
            })
        except Exception as e:
            details.append({
                "question": case.question,
                "expected_item_id": exp_ids[0] if exp_ids else case.expected_item_id,
                "expected_item_title": exp_titles[0] if exp_titles else (case.expected_item_title or ""),
                "expected_item_ids": exp_ids,
                "expected_item_titles": exp_titles,
                "matched_count": 0,
                "expected_count": len(exp_ids),
                "missing_titles": exp_titles,
                "actual_items": [],
                "hit": False,
                "error": str(e),
            })

    hit_rate = hit_count / total if total > 0 else 0.0

    # 持久化评测结果
    result = EvaluationResult(
        user_id=user_id,
        total_cases=total,
        hit_count=hit_count,
        hit_rate=hit_rate,
        details=details,
    )
    db.add(result)
    db.commit()

    return {
        "total_cases": total,
        "hit_count": hit_count,
        "hit_rate": round(hit_rate, 4),
        "details": details,
    }


def get_stats(db: Session, user_id: str) -> dict:
    """获取统计数据"""
    total_items = db.query(KnowledgeItem).filter(KnowledgeItem.user_id == user_id).count()
    published_items = db.query(KnowledgeItem).filter(
        KnowledgeItem.user_id == user_id, KnowledgeItem.status == "published"
    ).count()

    total_cases = db.query(TestCase).filter(TestCase.user_id == user_id).count()

    latest_eval = db.query(EvaluationResult).filter(
        EvaluationResult.user_id == user_id
    ).order_by(EvaluationResult.created_at.desc()).first()
    latest_hit_rate = latest_eval.hit_rate if latest_eval else None

    # 反馈统计：来源为 ChatMessage 表的 feedback 字段（问答页用户点的有用/没用）。
    # 评测页属后台功能，管理员/超管看到的是「全站反馈量」，与下方「待处理负反馈」
    # 列表口径保持一致；普通用户（无该页面权限）仍只统计自己的反馈。
    operator = db.query(User).filter(User.id == user_id).first()
    fb_query = db.query(ChatMessage)
    if operator is not None and not is_staff(operator):
        fb_query = fb_query.filter(ChatMessage.user_id == user_id)
    feedback_helpful = fb_query.filter(ChatMessage.feedback == "useful").count()
    feedback_unhelpful = fb_query.filter(ChatMessage.feedback == "useless").count()

    return {
        "total_items": total_items,
        "published_items": published_items,
        "total_cases": total_cases,
        "latest_hit_rate": latest_hit_rate,
        "feedback_helpful": feedback_helpful,
        "feedback_unhelpful": feedback_unhelpful,
    }


def create_feedback(db: Session, user_id: str, question: str, answer: str, is_helpful: bool) -> ChatFeedback:
    fb = ChatFeedback(
        user_id=user_id,
        question=question,
        answer=answer,
        is_helpful=is_helpful,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return fb


# ===== 负反馈转测试用例（闭环） =====

def _find_paired_question(db: Session, msg: ChatMessage) -> Optional[str]:
    """找出同一用户在该条 AI 回答之前最近的一次提问"""
    prev = db.query(ChatMessage).filter(
        ChatMessage.user_id == msg.user_id,
        ChatMessage.role == "user",
        ChatMessage.created_at <= msg.created_at,
    ).order_by(ChatMessage.created_at.desc()).first()
    return prev.content if prev else None


def get_pending_feedback(db: Session, limit: int = 100) -> List[dict]:
    """列出「没用」反馈且尚未转为测试用例的 AI 回答（含配对提问与当时引用来源）"""
    used_ids = {
        row[0]
        for row in db.query(TestCase.source_message_id)
        .filter(TestCase.source_message_id.isnot(None))
        .all()
    }

    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.role == "assistant", ChatMessage.feedback == "useless")
        .order_by(ChatMessage.created_at.desc())
        .all()
    )
    pending = [m for m in msgs if m.id not in used_ids][:limit]

    uids = {m.user_id for m in pending}
    usernames = {}
    if uids:
        usernames = {
            u.id: u.username for u in db.query(User).filter(User.id.in_(uids)).all()
        }

    return [
        {
            "message_id": m.id,
            "user_id": m.user_id,
            "username": usernames.get(m.user_id, "未知用户"),
            "question": _find_paired_question(db, m) or "",
            "answer": m.content or "",
            "reason": (m.feedback_reason or "") if m.feedback == "useless" else "",
            "references": m.references or [],
            "created_at": m.created_at,
        }
        for m in pending
    ]


def create_case_from_feedback(
    db: Session,
    user_id: str,
    message_id: str,
    question: str,
    expected_item_id: Optional[str] = None,
    expected_item_ids: Optional[List[str]] = None,
) -> TestCase:
    """把一条「没用」反馈经人工确认后转为测试用例

    纯人工确认：期望条目由管理员指定（可多选，全部命中才算通过），不做任何自动推断，避免误伤。
    """
    msg = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
    if not msg:
        raise FeedbackToCaseError("反馈消息不存在", 404)
    if msg.role != "assistant" or msg.feedback != "useless":
        raise FeedbackToCaseError("该消息不是「没用」的 AI 回答，无法转换")

    if db.query(TestCase).filter(TestCase.source_message_id == message_id).first():
        raise FeedbackToCaseError("该反馈已转为测试用例，请勿重复转换", 409)

    ids, titles = _resolve_expected_items(db, expected_item_ids, expected_item_id)

    question = (question or "").strip()
    if not question:
        raise FeedbackToCaseError("问题不能为空")

    case = TestCase(
        user_id=user_id,
        question=question,
        expected_item_id=ids[0],
        expected_item_title=titles[0],
        expected_item_ids=ids,
        expected_item_titles=titles,
        source_message_id=message_id,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


def citation_stats(db: Session, user_id: Optional[str], limit: int = 200) -> dict:
    """引用质量统计（改造 3 的度量出口）

    两个互补口径，都取最近 `limit` 条 AI 回答：

    1. **`citation_support_rate`（引用支撑率，主指标）**
       留下的引用 ÷ (留下的 + 被剔除的)。回答没用到、被校验剔除的来源就是"不相关引用"。
       它**不依赖回答里有没有硬信息**，因此在中文业务问答里总有值、最有判别力。
    2. **`avg_coverage`（硬信息覆盖率，辅助指标）**
       回答里的数字/天数/系统名等硬信息，能在引用原文里找到的比例。
       回答完全不含硬信息时该值为空——此时不会拉低均值（`verified` 与 `total` 分开统计，
       "覆盖率低"和"根本没校验"不会被混为一谈）。

    - `user_id=None`（管理员/超管）→ 全站口径，与评测页其他卡片一致。
    """
    query = db.query(ChatMessage).filter(ChatMessage.role == "assistant")
    if user_id is not None:
        query = query.filter(ChatMessage.user_id == user_id)
    rows = query.order_by(ChatMessage.created_at.desc()).limit(max(1, int(limit))).all()

    total = len(rows)
    scored = [(row, float(row.citation_coverage)) for row in rows if row.citation_coverage is not None]
    verified = len(scored)

    avg_coverage = round(sum(v for _r, v in scored) / verified, 4) if verified else None
    pass_count = sum(1 for _r, v in scored if v >= 0.85)
    degraded_count = sum(1 for _r, v in scored if v < 0.55)

    # 引用支撑率（不依赖硬信息覆盖率，因此样本更多）
    rated = [row for row in rows if row.citation_dropped is not None]
    kept_total = sum(len(row.references or []) for row in rated)
    dropped_total = sum(int(row.citation_dropped or 0) for row in rated)
    denom = kept_total + dropped_total
    support_rate = round(kept_total / denom, 4) if denom else None

    recent = []
    for row in rows[:10]:
        if row.citation_coverage is None and row.citation_dropped is None:
            continue
        refs = row.references or []
        partial = sum(1 for ref in refs if isinstance(ref, dict) and ref.get("verified") == "partial")
        dropped = int(row.citation_dropped or 0)
        row_denom = len(refs) + dropped
        recent.append(
            {
                "message_id": row.id,
                "coverage": row.citation_coverage,
                "support_rate": round(len(refs) / row_denom, 4) if row_denom else None,
                "references": len(refs),
                "partial": partial,
                "dropped": dropped,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )

    return {
        "total": total,
        "verified": verified,
        # 主指标：引用支撑率（"引用不相关"的直接度量）
        "citation_support_rate": support_rate,
        # 辅助指标：硬信息覆盖率（回答含硬信息时才有值）
        "avg_coverage": avg_coverage,
        "pass_rate": round(pass_count / verified, 4) if verified else None,
        "degraded_rate": round(degraded_count / verified, 4) if verified else None,
        "kept_references": kept_total,
        "dropped_references": dropped_total,
        "recent": recent,
    }
