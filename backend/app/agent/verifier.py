"""引用校验（Verifier）—— 把「引用不相关」从体感问题变成可度量指标

这是 Agent 化改造里优先级最高的一环：项目最大的业务痛点是
"回答引用了不相关的资料"，而改进它的先决条件是**先能量化它**。

两级校验，代价从低到高：

1. **规则校验（零成本、确定性、永远执行）**
   从文本里抽「强事实」——数字带单位、百分比、日期、三位以上数字、英文缩写、
   引号内的专名——再看它们是否出现在证据原文里。命中率即 coverage。
   这类事实恰好是幻觉的高发区（金额、天数、系统名、编号），
   而它们也能被确定性规则捕获，因此不需要动用模型。

2. **LLM 校验（仅规则"存疑"时调用，可开关）**
   规则覆盖率落在中间地带（既不算通过、也不算不合格）时，
   才请模型逐句判断陈述是否被证据支撑。

**两个使用时机**（对应两件不同的事，故意分开，不要混在一起）：

- `verify_evidence(question, evidence)` —— **生成前**。问题里的硬信息在证据中缺失，
  说明检索可能跑偏了，这时"回炉换词再检索一次"才有意义（这是图里 repair 的依据）。
- `verify_answer(answer, evidence)` —— **生成后**。答案里出现证据没有的事实 → 幻觉；
  同时按"答案是否真的用到了这条证据"给引用打 `verified` 三态，
  **没被用到的引用直接不返回**（宁少勿错，与项目既有的"文档严格过滤"取向一致）。

设计约束（很重要）：

- 本模块**只读、永不抛异常**。校验失败必须退化为"放行 + 标记未校验"，
  而不是让问答失败——校验是质量增强，不是关键路径。
- **不改写答案正文**。答案已经流式发给用户了，事后偷偷改文本是不诚实的；
  校验结论通过"引用三态 + 降级提示 + 覆盖率"三个可观测出口表达。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import settings

# 引用相关性的判定阈值：特征词命中率 ≥ 该值算"确实用到"，> 0 算"部分支持"
CITATION_PARTIAL_THRESHOLD = 0.25

# 规则校验里"强事实"的抽取规则（顺序不重要，最后统一去重）
# 引号只收书名号与中文直角引号 —— 这两种基本只用于专名/术语；
# 而双引号（"…" / “…”）在模型输出里常被用来**强调**，收进来会产生大量假阳性
# （实测："“校招出差前”的准备工作"这类转述会被误判成"未被支撑的事实"，把覆盖率压到 25%）。
_QUOTED = re.compile(r"《([^》\n]{2,24})》|「([^」\n]{2,24})」")
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s*%")
_QUANTITY = re.compile(
    r"\d+(?:\.\d+)?\s*(?:个)?(?:工作日|小时|分钟|天|日|周|月|年|元|万元|万|次|份|张|条|人|台|套)"
)
_DATE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]")
# 三位以上数字（编号/金额等）。后面紧跟量词的不在此收——避免与 _QUANTITY 重复计数
_BIG_NUMBER = re.compile(
    r"\d{3,}(?!\s*(?:个)?(?:工作日|小时|分钟|天|日|周|月|年|元|万元|万|次|份|张|条|人|台|套))"
)
# 英文缩写/系统名（前后不能紧跟字母数字，但允许中文字符紧邻）
_ACRONYM = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,7}(?![A-Za-z0-9])")

# 关于"中文业务专名"（XX系统 / XX接洽函 …）为何不做正则抽取 —— 实测教训：
# 用 `[\u4e00-\u9fff]{2,6}(系统|接洽函|…)` 这类模式，会把"但涉及接洽函""向学校发送的接洽函"
# 整段前缀吞进来，产生大量假阳性。假阳性会让覆盖率虚低并错误触发降级，
# 直接污染度量指标——比"漏判"更糟（漏判只是少校验一层，假阳性是给出错误结论）。
# 因此规则层只做它**能可靠判断**的事：数字/单位/百分比/日期/英文缩写/书名号专名。

# "知识库答不上来"的话术特征：这类回答不该再配引用（否则用户会看到自相矛盾的组合）
_NO_CONTENT_MARKERS = ("暂无相关内容", "未收录", "没有找到", "暂无相关", "未找到")
_NO_CONTENT_MAX_LEN = 80

# 特征词抽取时忽略的词（只影响"引用是否被用到"的判断，不影响事实校验）
_STOPWORDS = {
    "的", "了", "是", "在", "有", "和", "就", "不", "人", "都", "一", "一个", "我们", "你们",
    "上", "下", "也", "很", "到", "说", "要", "去", "你", "我", "他", "会", "着", "没有",
    "什么", "怎么", "如何", "为什么", "可以", "可以了", "吗", "呢", "吧", "啊", "哦", "嗯",
    "这个", "那个", "这些", "那些", "如果", "因为", "所以", "但是", "而且", "以及", "或者",
    "需要", "进行", "通过", "根据", "按照", "相关", "情况", "内容", "工作", "问题", "要求",
    "以下", "上述", "如下", "注意", "事项", "步骤", "方法", "方式", "时间", "时候",
}


# ---------------------------------------------------------------------------
# 文本归一化与事实抽取
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """归一化：全角转半角、去空白、转小写（用于确定性的包含判断）

    中文业务文本里"300 元"和"300元"、"HR系统"和"hr系统"都会被归一到同一形态，
    这样规则校验不需要关心排版差异。

    非字符串输入一律按空处理——校验是只读增强，必须对畸形输入免疫。
    """
    if not isinstance(text, str) or not text:
        return ""
    buf: List[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:            # 全角空格
            code = 0x20
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            code -= 0xFEE0
        buf.append(chr(code))
    return re.sub(r"\s+", "", "".join(buf)).lower()


def _dedupe_normalized(items: Sequence[str]) -> List[str]:
    """按归一化结果去重，并丢弃单字符噪声"""
    seen = set()
    out: List[str] = []
    for raw in items:
        val = (raw or "").strip()
        if not val:
            continue
        key = _normalize(val)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(val)
    return out


def extract_facts(text: str) -> List[str]:
    """抽取文本中的「强事实」——可被确定性验证的硬信息

    刻意只收高置信度的形态：数字带单位、百分比、日期、三位以上数字、
    英文缩写、引号内的专名。

    **不收中文数词**（"三"、"两次"）：中文数词与阿拉伯数字的等价改写
    （"三天" vs "3 天"）需要语义判断，规则层面强行匹配会引入大量误判。
    这会导致少量漏判（→ 覆盖率偏低 → 多回炉一次检索），代价可控且方向安全。

    也不收双引号里的内容：模型常用引号做**强调**（“校招出差前”的准备工作），
    把它当专名会制造大量假阳性。
    """
    if not isinstance(text, str) or not text:
        return []
    found: List[str] = []
    found += [m.group(1) or m.group(2) for m in _QUOTED.finditer(text)]
    found += _PERCENT.findall(text)
    found += _QUANTITY.findall(text)
    found += _DATE.findall(text)
    found += _BIG_NUMBER.findall(text)
    found += _ACRONYM.findall(text)
    return _dedupe_normalized(found)


def is_no_content_answer(answer: str) -> bool:
    """判断回答是不是"知识库答不上来"的话术

    这类回答不该再配引用：用户会看到"抱歉，知识库中暂无相关内容"旁边挂着一条来源，
    观感上自相矛盾。长度限制用于排除"先答一部分、末尾补一句未收录"的长回答。
    """
    if not isinstance(answer, str):
        return False
    text = answer.strip()
    if not text or len(text) > _NO_CONTENT_MAX_LEN:
        return False
    return any(marker in text for marker in _NO_CONTENT_MARKERS)


def _evidence_text(ref: Dict[str, Any]) -> str:
    """把一条证据（或引用）拼成用于校验的文本"""
    if not isinstance(ref, dict):
        return ""
    title = ref.get("title") or ""
    content = ref.get("content") or ""
    return f"{title}\n{content}".strip()


def _feature_terms(text: str, limit: int = 12) -> List[str]:
    """抽取文本的「特征词」（归一化后），用于判断答案是否用到了这条证据

    与检索用的关键词抽取不同：这里不需要同义词扩展，也不需要召回排序，
    只要"有辨识度、且出现在答案里就说明被用到"的词。
    取最长的前 N 个，避免长证据因词多而被稀释（分母越大越难命中）。
    """
    if not isinstance(text, str) or not text:
        return []
    try:
        import jieba
    except Exception:  # noqa: BLE001 —— jieba 不可用时退化为按非字分割
        candidates = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}", text)
    else:
        candidates = [w.strip() for w in jieba.lcut(re.sub(r"\s+", " ", text))]

    terms: List[str] = []
    seen = set()
    for word in candidates:
        if len(word) < 2 or word in _STOPWORDS:
            continue
        if re.fullmatch(r"[\W_]+", word):   # 纯符号
            continue
        key = _normalize(word)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        terms.append(key)
    terms.sort(key=len, reverse=True)
    return terms[:limit]


# ---------------------------------------------------------------------------
# 一级：规则校验
# ---------------------------------------------------------------------------

def _fact_supported(fact: str, haystack_norm: str) -> bool:
    key = _normalize(fact)
    return bool(key) and key in haystack_norm


def _rule_check(
    claim_text: str, evidence_texts: Sequence[str], pass_threshold: float, repair_threshold: float
) -> Dict[str, Any]:
    """规则校验：claim 里的强事实有多少能在证据里找到"""
    facts = extract_facts(claim_text)
    if not facts:
        return {
            "decision": "uncertain",
            "coverage": None,
            "checked": 0,
            "missing": [],
            "mode": "rule",
            "note": "无可校验的强事实（文本中没有数字、系统名等硬信息）",
        }

    haystack = _normalize("\n".join(t for t in evidence_texts or [] if t))
    missing = [f for f in facts if not _fact_supported(f, haystack)]
    coverage = (len(facts) - len(missing)) / len(facts)

    if coverage >= pass_threshold:
        decision = "pass"
    elif coverage < repair_threshold:
        decision = "fail"
    else:
        decision = "uncertain"

    return {
        "decision": decision,
        "coverage": round(coverage, 4),
        "checked": len(facts),
        "missing": missing[:12],
        "mode": "rule",
    }


# ---------------------------------------------------------------------------
# 二级：LLM 校验（仅规则存疑时）
# ---------------------------------------------------------------------------

VERIFY_SYSTEM_PROMPT = """你是严格的引用校验员。你只做一件事：判断给定文本中的事实性陈述能否被证据支持。

判定原则：
1. 证据里没有明确写到的，一律判 unsupported —— 不要脑补，不要因为"看起来合理"就放行。
2. 数字、金额、天数、系统名、时间、人名、流程顺序，错一处即算 unsupported。
3. 纯过渡句（"以下是操作步骤"）与主观建议不算事实陈述，忽略不计。
4. 只输出 JSON，不要任何解释文字。"""

VERIFY_PROMPT = """待校验文本：
{claim}

证据：
{evidence}

请逐句判断，输出 JSON：
{{"claims":[{{"text":"...","supported_by":[1],"supported":true}}],
  "unsupported":["..."],
  "coverage":0.0}}

其中 coverage 为被支持的陈述数 ÷ 事实性陈述总数（0.0~1.0）。"""


def _numbered_evidence(
    texts: Sequence[str], per_item: int = 800, max_items: int = 8
) -> str:
    """把证据编号后压平（给 LLM 引用编号用）"""
    lines: List[str] = []
    for i, text in enumerate(list(texts)[:max_items], 1):
        block = re.sub(r"\s+", " ", text or "").strip()
        if len(block) > per_item:
            block = block[:per_item] + "…"
        lines.append(f"[{i}] {block}")
    return "\n".join(lines) or "（无证据）"


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抽 JSON（容忍 ```json 包裹与前后废话）"""
    if not text:
        return None
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
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


def _invoke_with_timeout(llm, messages, timeout: int):
    """带软超时的 LLM 调用（超时返回 None，不让校验拖住问答）"""
    if not timeout or timeout <= 0:
        return llm.invoke(messages)

    import threading

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


def _skipped(reason: str, stage: str = "") -> Dict[str, Any]:
    """放行且标记未校验（校验失败/跳过时的统一出口）"""
    return {
        "decision": "pass",
        "coverage": None,
        "checked": 0,
        "missing": [],
        "mode": "skipped",
        "note": reason,
        "stage": stage,
    }


def _llm_check(
    claim_text: str, evidence_texts: Sequence[str], user, timeout: int
) -> Dict[str, Any]:
    """LLM 复核：逐句判断事实性陈述是否被证据支撑"""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.services import llm_service

        llm = llm_service.get_llm_for_user(user)
        messages = [
            SystemMessage(content=VERIFY_SYSTEM_PROMPT),
            HumanMessage(
                content=VERIFY_PROMPT.format(
                    claim=(claim_text or "")[:3000],
                    evidence=_numbered_evidence(evidence_texts),
                )
            ),
        ]
        response = _invoke_with_timeout(llm, messages, timeout)
        if response is None:
            return _skipped("LLM 复核超时")

        data = _extract_json(getattr(response, "content", "") or "")
        if not isinstance(data, dict):
            return _skipped("LLM 复核输出无法解析为 JSON")

        raw_coverage = data.get("coverage")
        try:
            coverage = max(0.0, min(1.0, float(raw_coverage)))
        except (TypeError, ValueError):
            return _skipped("LLM 复核未给出有效的 coverage")

        pass_threshold = float(settings.AGENT_VERIFIER_PASS_THRESHOLD)
        repair_threshold = float(settings.AGENT_VERIFIER_REPAIR_THRESHOLD)
        if coverage >= pass_threshold:
            decision = "pass"
        elif coverage < repair_threshold:
            decision = "fail"
        else:
            decision = "uncertain"

        unsupported = [str(x) for x in (data.get("unsupported") or []) if str(x).strip()]
        return {
            "decision": decision,
            "coverage": round(coverage, 4),
            "checked": len(data.get("claims") or []),
            "missing": unsupported[:12],
            "mode": "llm",
        }
    except Exception as exc:  # noqa: BLE001 —— 校验绝不能反过来弄挂问答
        return _skipped(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def verify(
    claim_text: str,
    evidence_texts: Sequence[str],
    user=None,
    *,
    use_llm: Optional[bool] = None,
    stage: str = "answer",
) -> Dict[str, Any]:
    """两级校验统一入口

    `stage` 只用于标注与观测（`evidence` = 生成前校验问题与证据的匹配，
    `answer` = 生成后校验答案的事实支撑度），不影响算法。
    """
    evidence_texts = [t for t in (evidence_texts or []) if isinstance(t, str) and t.strip()]
    if not evidence_texts:
        # 没有证据可校验：不判"不合格"（否则"无引用"场景会被误伤），交由调用方处理
        return _skipped("无证据可供校验", stage)

    pass_threshold = float(settings.AGENT_VERIFIER_PASS_THRESHOLD)
    repair_threshold = float(settings.AGENT_VERIFIER_REPAIR_THRESHOLD)

    rule = _rule_check(claim_text, evidence_texts, pass_threshold, repair_threshold)
    rule["stage"] = stage
    if rule["decision"] != "uncertain":
        return rule

    if use_llm is None:
        use_llm = bool(settings.AGENT_VERIFIER_USE_LLM)
    if not use_llm:
        # 规则落在中间地带：放行（不阻塞用户），但保留真实覆盖率与不确定标记，
        # 便于度量时把"规则确定通过"和"规则拿不准但放行"区分开
        return {**rule, "decision": "pass", "mode": "rule_lenient"}

    llm_result = _llm_check(
        claim_text, evidence_texts, user, timeout=int(settings.AGENT_VERIFIER_LLM_TIMEOUT)
    )
    if llm_result.get("coverage") is None:
        # LLM 复核没给出有效结论：退回规则结论（放行）
        return {
            **rule,
            "decision": "pass",
            "mode": "rule_lenient",
            "note": llm_result.get("note", ""),
        }
    llm_result["stage"] = stage
    return llm_result


def verify_evidence(
    question: str, evidence: Sequence[Dict[str, Any]], user=None, *, use_llm: Optional[bool] = None
) -> Dict[str, Any]:
    """生成前：问题里的硬信息能否在证据中找到 → 决定是否回炉补检索

    只在真正缺关键信息时才会判 fail；问题本身不含硬信息（如"出差前要做什么"）
    会返回 uncertain → 放行，不会无谓地触发回炉。
    """
    refs = evidence if isinstance(evidence, (list, tuple)) else []
    texts = [_evidence_text(ref) for ref in refs]
    return verify(question, texts, user, use_llm=use_llm, stage="evidence")


# ---------------------------------------------------------------------------
# 引用过滤（verified 三态）
# ---------------------------------------------------------------------------

def _citation_relevance(answer_norm: str, ref: Dict[str, Any]) -> float:
    """一条证据被答案"用到"的程度 = 特征词命中率"""
    terms = _feature_terms(_evidence_text(ref))
    if not terms or not answer_norm:
        return 0.0
    hits = sum(1 for term in terms if term in answer_norm)
    return round(hits / len(terms), 4)


def to_citation(ref: Dict[str, Any], verified: Any = None) -> Dict[str, Any]:
    """转成前端引用结构（与固定管线 references 字段一致的字段 + verified 增量）

    对畸形输入免疫：非 dict 按空字典处理，score 非数字按 0 处理。
    """
    if not isinstance(ref, dict):
        ref = {}
    try:
        score = round(float(ref.get("score") or 0), 4)
    except (TypeError, ValueError):
        score = 0.0
    item: Dict[str, Any] = {
        "item_id": ref.get("id"),
        "title": ref.get("title") or "",
        "score": score,
        "source_type": ref.get("source_type") or "knowledge_item",
    }
    if verified is not None:
        item["verified"] = verified
    return item


def filter_citations(
    answer: str, evidence: Sequence[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按"答案是否真的用到这条证据"过滤引用，并标注 verified 三态

    返回 `(保留的引用, 被剔除的引用)`。剔除是"宁少勿错"的取向：
    引用列表只放对回答有实际贡献的来源，避免"答案与引用对不上"的观感问题——
    这正是用户反复反馈的那个痛点。

    verified 取值：`True` 已核验 / `"partial"` 部分支持 / `False` 无贡献（被剔除）。
    """
    evidence = list(evidence or []) if isinstance(evidence, (list, tuple)) else []
    if not evidence:
        return [], []

    # "知识库暂无相关内容"这类回答不配引用（否则观感自相矛盾）
    if is_no_content_answer(answer):
        return [], [to_citation(ref, verified=False) for ref in evidence]

    answer_norm = _normalize(answer or "")
    scored = [(idx, ref, _citation_relevance(answer_norm, ref)) for idx, ref in enumerate(evidence)]

    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for _idx, ref, rel in scored:
        if rel <= 0 or not answer_norm:
            item = to_citation(ref, verified=False)
            dropped.append(item)
            continue
        item = to_citation(ref, verified=True if rel >= CITATION_PARTIAL_THRESHOLD else "partial")
        item["relevance"] = rel
        kept.append(item)

    # 保底：至少保留 N 条，避免"答案有内容但引用列表空空如也"（用户会以为系统出错）
    min_keep = max(0, int(settings.AGENT_VERIFIER_MIN_CITATIONS))
    while len(kept) < min_keep and dropped:
        top = max(dropped, key=lambda c: float(c.get("score") or 0))
        dropped.remove(top)
        top["verified"] = "partial"
        top["rescued"] = True   # 标记"本无贡献，为保底而保留"，度量时可排除
        kept.append(top)

    kept.sort(key=lambda c: float(c.get("score") or 0), reverse=True)
    return kept, dropped


def verify_answer(
    answer: str,
    evidence: Sequence[Dict[str, Any]],
    user=None,
    *,
    use_llm: Optional[bool] = None,
) -> Dict[str, Any]:
    """生成后：答案的事实支撑度 + 引用三态过滤

    注意 `decision` 与生成前的语义不同——正文已经流式发给用户了，"回炉"没有意义，
    因此 `fail` 在这里被翻译成 `degraded`（降级标记），由调用方在流尾给出提示。
    """
    refs = evidence if isinstance(evidence, (list, tuple)) else []
    texts = [_evidence_text(ref) for ref in refs]
    support = verify(answer, texts, user, use_llm=use_llm, stage="answer")

    citations, dropped = filter_citations(answer, refs)
    decision = support.get("decision") or "pass"
    if decision == "fail":
        decision = "degraded"

    # 引用支撑率 = 留下的引用 ÷ 候选引用。
    # 这是中文业务问答里**最有判别力**的指标：回答通常没有数字可校验（coverage 为 None），
    # 但"检索了 8 条、只有 1 条真的被用到"这件事本身就能说明检索/引用质量。
    candidate_count = len(refs)
    kept_count = len(citations)
    kept_ratio = round(kept_count / candidate_count, 4) if candidate_count else None

    return {
        "decision": decision,                  # pass | degraded | skipped
        "coverage": support.get("coverage"),   # 硬信息支撑率（无硬信息时为 None）
        "kept_ratio": kept_ratio,              # 引用支撑率（有引用时总有值）
        "candidate_count": candidate_count,
        "kept_count": kept_count,
        "checked": support.get("checked", 0),
        "unsupported": list(support.get("missing") or []),
        "citations": citations,
        "dropped": dropped,
        "mode": support.get("mode", "rule"),
        "stage": "answer",
    }


# ---------------------------------------------------------------------------
# 轨迹与提示词（图与 runtime 共用，避免两处各写一份文案）
# ---------------------------------------------------------------------------

_DECISION_LABEL = {
    "pass": "通过",
    "degraded": "降级",
    "fail": "不合格",
    "uncertain": "存疑",
    "skipped": "跳过",
}


def summary_line(result: Dict[str, Any]) -> str:
    """把校验结果压成一句轨迹（agent_runs.notes 与前端「过程」面板共用）"""
    decision = result.get("decision") or "skipped"
    label = _DECISION_LABEL.get(decision, decision)
    coverage = result.get("coverage")
    if coverage is None:
        cov = "—"
    else:
        try:
            cov = f"{float(coverage):.0%}"
        except (TypeError, ValueError):
            cov = "—"

    parts = [f"引用校验：{label}（覆盖率 {cov}"]
    if result.get("checked"):
        parts.append(f"，核验 {result['checked']} 项硬信息")
    if result.get("dropped"):
        parts.append(f"，剔除 {len(result['dropped'])} 条无贡献引用")
    if result.get("unsupported"):
        parts.append(f"，{len(result['unsupported'])} 项未见证据")
    line = "".join(parts) + "）"
    if result.get("mode") == "rule_lenient":
        line += "［规则拿不准，已放行］"
    return line


def repair_hint(result: Dict[str, Any]) -> str:
    """把"缺什么"转成给规划模型的一句话（repair 分支用）"""
    missing = [str(m) for m in (result.get("missing") or []) if str(m).strip()]
    if not missing:
        return (
            "现有证据未能覆盖问题中的关键信息。"
            "请换用不同的业务关键词（流程名/系统名/物料名/同义业务词）再检索一次。"
        )
    shown = "、".join(missing[:6])
    return (
        f"现有证据中缺少这些关键信息：{shown}。"
        "请换用不同的业务关键词（流程名/系统名/物料名/同义业务词）再检索一次，"
        "或直接说明证据不足。"
    )
