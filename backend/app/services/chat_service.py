"""问答服务：混合检索 + LLM 生成回答"""
import json
import math
import re
from typing import List, Dict, Any, AsyncGenerator
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, select
from app.models import User, KnowledgeItem, SourceDocument, Flow, FlowNode, ChatMessage
from app.services.embedding_service import embed_dense, embed_sparse
from app.services import qdrant_service
from app.services.llm_service import get_llm_for_user
from app.core.roles import read_scope
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage


SCORE_THRESHOLD = 0.3
# 单个文档片段最大字符数，避免上下文超长
MAX_DOC_CHUNK_LENGTH = 1500
# 相关性最低阈值（命中关键词数 / 总关键词数），低于此值的结果不返回
MIN_RELEVANCE_RATIO = 0.2

# 阶段二（资料入库文档检索）的严格过滤参数：
# 文档内容宽泛、关键词易误命中（如「工作」「内容」等通用词），阈值必须严于知识条目，
# 否则会把不相关的系统文档、流程梳理文档也带进引用来源
DOC_MIN_RELEVANCE_RATIO = 0.4  # 文档相关性阈值（命中率 ≥ 40%）
DOC_MIN_CONTENT_HITS = 2       # 文档正文至少要命中的关键词数（文件名命中不算，防误引）
DOC_MAX_RESULTS = 3            # 单次回答最多引用的文档数
# 文档检索时的粗筛候选上限
DOC_CANDIDATE_LIMIT = 30

# 长期记忆（改造 4）里"用户偏好"最多补充的关键词数。
# 上限刻意压到 2：偏好只是"锦上添花"，多了会把用户这次真正问的词挤掉，把检索带偏。
MAX_PREFERENCE_TERMS = 2

# 业务同义词扩展：用户提问用词 vs 知识库用词不一致时自动补全
SYNONYMS = {
    '工资': ['薪资', '薪水', '薪酬', '报酬'],
    '薪资': ['工资', '薪水', '薪酬', '报酬'],
    '薪水': ['工资', '薪资', '薪酬'],
    '薪酬': ['工资', '薪资', '薪水'],
    '放假': ['休假', '假期', '节假日'],
    '休假': ['放假', '假期', '节假日'],
    '假期': ['放假', '休假', '节假日'],
    '节假日': ['放假', '休假', '假期'],
    '入职': ['入职', '新员工', '报到'],
    '辞职': ['离职', '离岗', '辞职'],
    '离职': ['辞职', '离岗'],
    '加班': ['加班', '延时工作'],
    '报销': ['报销', '费用报销'],
    '考勤': ['考勤', '打卡', '签到'],
    '打卡': ['考勤', '签到'],
    '签到': ['考勤', '打卡'],
    '培训': ['培训', '入职培训', '新人培训'],
    '合同': ['合同', '劳动合同', '协议'],
    '社保': ['社保', '五险一金', '保险'],
    '五险一金': ['社保', '保险'],
    '公积金': ['公积金', '住房公积金'],
}

SYSTEM_PROMPT = """你是一个新员工入职业务问答助手。请根据提供的知识库内容回答用户问题。

知识库内容来源包括：
1. 结构化知识条目（标题、步骤、注意事项等）
2. 原始文档片段（来自用户上传的 Word/Excel/PDF 等文档）

要求：
1. 仅基于知识库内容回答，不要编造信息
2. 如果知识库中没有相关内容，直接回答"抱歉，知识库中暂无相关内容"
3. 回答要简洁、准确、条理清晰
4. 如果涉及操作步骤，按序号列出
5. 对话中可能包含历史消息：用户常用简短的话追问（例如"漏了一条""还有呢""继续"），
   这是在要求补充或修正上一轮回答，而不是提出全新问题。此时应结合对话历史理解意图，
   对照知识库内容检查上一轮回答是否有遗漏，并在上一轮回答的基础上补充完整。
6. 排版必须使用标准 Markdown，严格遵守：
   - 章节标题（「一、」「二、」等）必须独占一行，且与上一段之间空一行；
     绝不允许把新章节标题直接接在上一句话的末尾
   - 有序列表（1. 2. 3.）和无序列表（- ）每一项独立一行
   - 章节与章节之间用空行分隔
"""


def search_relevant_items(
    user: User,
    question: str,
    db: Session = None,
    limit: int = 8,
    extra_terms: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """两阶段检索相关知识（Qdrant 不可用时降级为数据库关键词搜索）

    阶段一：知识库 —— Qdrant 混合检索结构化知识条目 + 流程节点补全
            （降级时为数据库关键词搜索，同样只查知识条目和流程节点）；
    阶段二：资料入库 —— 对用户上传的原始文档做关键词相关性检索（严格阈值过滤）。
    知识库结果排在文档结果前面，知识库优先命中。
    检索后做两步质量保障：
    1. 流程节点补全——问题命中某流程名时，把该流程的全部节点补进结果
       （流程节点不入向量库，只靠相似度必然漏召回）；
    2. 近重复去重——流程节点与其关联知识条目标题几乎相同（如「XX流程 - 条目名」
       与「条目名」），保留分数高的一条，避免引用来源重复占位。

    `extra_terms`（改造 4）= L2 用户偏好里的实体词，只影响**关键词类**检索路径
    （文档检索 / 降级检索）。刻意不把它拼进 Qdrant 的 dense 查询文本——
    那会改变向量语义，把主检索路径的精度押在"用户画像猜得准"上，风险不划算。
    """
    # ---- 阶段一：知识库（知识条目 + 流程节点）----
    try:
        dense_vec = embed_dense(question, user)
        sparse_vec = embed_sparse(question)
        results = qdrant_service.hybrid_search(
            user=user,
            dense_query_vector=dense_vec,
            sparse_query_vector=sparse_vec,
            limit=limit,
            score_threshold=SCORE_THRESHOLD,
        )
        results = _augment_flow_nodes(user, question, db, results)
    except Exception as e:
        print(f"[警告] Qdrant 检索失败，降级为数据库关键词搜索: {e}")
        # 降级：从数据库关键词搜索（只查知识条目和流程节点，文档由阶段二统一负责）
        if db is None:
            return []
        results = _fallback_keyword_search(user, question, db, limit, extra_terms=extra_terms)

    results = _dedupe_results(results)[:limit]

    # ---- 阶段二：资料入库文档（关键词相关性检索，严格过滤不相关文档）----
    if db is not None:
        try:
            doc_results = _search_documents(user, question, db, extra_terms=extra_terms)
        except Exception as e:
            print(f"[警告] 文档检索失败，跳过文档引用: {e}")
            doc_results = []
        results = results + doc_results

    # 保持阶段顺序（知识库在前、文档在后）的去重，不重新排序
    return _dedupe_keep_order(results)[: limit + DOC_MAX_RESULTS]


def _dedupe_keep_order(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按标题近重复去重，保留先出现的（不改变原有顺序）"""
    kept: List[Dict[str, Any]] = []
    for r in results:
        if any(_is_dup_title(r.get("title", ""), k.get("title", "")) for k in kept):
            continue
        kept.append(r)
    return kept


def _latest_document_filter():
    """只保留版本链最新版：问答绝不能引用被替代的旧版文档

    同名文件升版后旧版仍在库里（可下载、可回溯），若不在这里过滤，
    模型会把「上一版口径」当成现行规定回答出去——正是版本台账要解决的问题。
    """
    return ~SourceDocument.id.in_(
        select(SourceDocument.supersedes_id).where(SourceDocument.supersedes_id.isnot(None))
    )


def _search_documents(
    user: User, question: str, db: Session, extra_terms: List[str] | None = None
) -> List[Dict[str, Any]]:
    """阶段二：资料入库文档检索（IDF 加权相关性，严格过滤，最多返回 DOC_MAX_RESULTS 条）

    可见范围与服务层一致：管理员全局；普通用户 = 自己的 + 共享的。
    文档内容宽泛，通用词（「工作」「内容」「需要」等）几乎每篇文档都有，
    简单命中率会把不相关文档误判为相关。因此采用 IDF 加权：
    在多数文档中都出现的关键词（通用词）权重趋近 0，特征词（「出差」「物料」等）权重高，
    只有特征词命中足够的文档才会进入引用来源。
    """
    keywords = _extract_keywords(question, extra_terms=extra_terms)
    if not keywords:
        return []

    doc_conditions = []
    for kw in keywords:
        doc_conditions.append(SourceDocument.content.like(f'%{kw}%'))
        doc_conditions.append(SourceDocument.filename.like(f'%{kw}%'))
    doc_query = db.query(SourceDocument).filter(or_(*doc_conditions), _latest_document_filter())
    doc_scope = read_scope(SourceDocument, user)
    if doc_scope is not None:
        doc_query = doc_query.filter(doc_scope)
    docs = doc_query.limit(DOC_CANDIDATE_LIMIT).all()
    if not docs:
        return []

    # IDF 权重：df = 包含该关键词的文档数，idf = ln(1 + N / (1 + df))
    # 分母同样只算最新版，否则旧版会让通用词看起来更"常见"，压低特征词权重
    total_docs = (
        db.query(func.count(SourceDocument.id)).filter(_latest_document_filter()).scalar() or 0
    )
    if total_docs <= 0:
        return []
    weights: Dict[str, float] = {}
    for kw in keywords:
        df = db.query(func.count(SourceDocument.id)).filter(
            or_(SourceDocument.content.like(f'%{kw}%'), SourceDocument.filename.like(f'%{kw}%')),
            _latest_document_filter(),
        ).scalar() or 0
        weights[kw] = math.log(1 + total_docs / (1 + df))
    total_weight = sum(weights.values()) or 1.0

    results = []
    for doc in docs:
        content = doc.content or ""
        content_hits = [kw for kw in keywords if kw in content]
        # 正文命中数不足，直接跳过（文件名命中不参与计数）
        if len(content_hits) < DOC_MIN_CONTENT_HITS:
            continue
        # IDF 加权命中率：通用词命中几乎不加分，特征词命中才真正提升分数
        hit_weight = sum(weights[kw] for kw in content_hits)
        score = hit_weight / total_weight
        if score < DOC_MIN_RELEVANCE_RATIO:
            continue
        relevant_text = _extract_relevant_paragraphs(content, keywords)
        if not relevant_text:
            continue
        results.append({
            "id": doc.id,
            "title": doc.filename,
            "content": relevant_text,
            "source_type": "document",
            "score": score,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:DOC_MAX_RESULTS]


def _augment_flow_nodes(user: User, question: str, db: Session, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """问题命中流程名时，把该流程的全部节点补进检索结果

    流程节点没有向量化，Qdrant 检索不到它们；而「按流程提问」是最主要的
    使用方式（如「校招出发前需要做哪些工作」期望拿到该流程的全部步骤）。
    """
    if db is None:
        return results
    keywords = _extract_keywords(question)
    if not keywords:
        return results

    flow_query = db.query(Flow)
    flow_scope = read_scope(Flow, user)
    if flow_scope is not None:
        flow_query = flow_query.filter(flow_scope)

    existing_ids = {r["id"] for r in results}
    existing_titles = [r.get("title", "") for r in results]
    augmented = list(results)
    for flow in flow_query.all():
        if not any(kw in flow.name for kw in keywords):
            continue
        sorted_nodes = sorted(flow.nodes, key=lambda n: n.order)
        for i, node in enumerate(sorted_nodes):
            if node.id in existing_ids:
                continue
            title = f"{flow.name} - {node.name}"
            # 与已有结果标题几乎相同（后缀包含）则视为重复，跳过
            if any(_is_dup_title(title, t) for t in existing_titles):
                continue
            augmented.append(_flow_node_result(db, flow, node, sorted_nodes, i, score=0.85))
            existing_ids.add(node.id)
            existing_titles.append(title)
    return augmented


def _is_dup_title(a: str, b: str) -> bool:
    """标题近重复判断：完全相等恒判重；后缀包含需长度≥8（避免短标题误伤）"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < 8 or len(b) < 8:
        return False
    return a.endswith(b) or b.endswith(a)


def _dedupe_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按标题近重复去重，保留分数高的一条（分数相同保留先出现的）"""
    kept: List[Dict[str, Any]] = []
    for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True):
        title = r.get("title", "")
        if any(_is_dup_title(title, k.get("title", "")) for k in kept):
            continue
        kept.append(r)
    return kept


def _extract_keywords(question: str, extra_terms: List[str] | None = None) -> List[str]:
    """从问题中提取关键词（中文用 jieba 分词）

    `extra_terms`（改造 4）是 L2 用户偏好里的实体词（常用流程名/系统名）。
    它们**排在问题自身关键词之后**、且**最多补 2 个**：偏好只用来"锦上添花"，
    绝不能把用户这次真正问的词挤出关键词表（那会把检索带偏）。
    """
    import jieba
    # 去除标点
    cleaned = question
    for ch in '？?，,。.！!、；;：:""\'\'（）()【】[]{}\n\r\t':
        cleaned = cleaned.replace(ch, ' ')
    # jieba 分词，过滤单字和停用词（含问候语、语气词等无意义词）
    stopwords = {'的', '了', '是', '在', '有', '和', '就', '不', '人', '都', '一', '一个',
                 '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好',
                 '什么', '怎么', '如何', '为什么', '可以', '吗', '呢', '吧', '啊', '哦', '嗯',
                 '你好', '您好', '谢谢', '感谢', '麻烦', '请问', '帮我', '帮忙', '一下',
                 '知道', '了解', '明白', '清楚', '对的', '是的', '没错', 'ok', 'OK', 'Okay'}
    words = jieba.lcut(cleaned)
    keywords = [w.strip() for w in words if len(w.strip()) > 1 and w.strip() not in stopwords]
    # 同义词扩展：补全业务同义词，提升召回率
    expanded = set(keywords)
    for kw in keywords:
        if kw in SYNONYMS:
            expanded.update(SYNONYMS[kw])
    keywords = list(expanded)
    # 如果分词结果为空，说明是闲聊或无意义问题，直接返回空列表（不做字组合回退）
    base = keywords[:10]  # 最多 10 个关键词
    if not extra_terms:
        return base
    known = set(base)
    added = 0
    for term in extra_terms:
        if added >= MAX_PREFERENCE_TERMS:  # 偏好最多补 2 个，硬收口
            break
        term = (term or '').strip()
        if not term or term in known:
            continue
        base.append(term)
        known.add(term)
        added += 1
    return base


def _extract_relevant_paragraphs(content: str, keywords: List[str], max_length: int = MAX_DOC_CHUNK_LENGTH) -> str:
    """从文档内容中提取包含关键词的段落，截断到 max_length"""
    if not content:
        return ""
    # 按段落分割（空行分隔）
    paragraphs = re.split(r'\n\s*\n', content)
    relevant = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # 段落中包含任意关键词则保留
        if any(kw in para for kw in keywords):
            relevant.append(para)
    # 如果没有匹配到段落，取前几段
    if not relevant:
        relevant = paragraphs[:3]
    result = "\n\n".join(relevant)
    # 截断
    if len(result) > max_length:
        result = result[:max_length] + "..."
    return result


def _compute_relevance(text: str, keywords: List[str]) -> float:
    """计算文本与关键词的相关性：命中关键词数 / 总关键词数"""
    if not keywords or not text:
        return 0.0
    hit_count = sum(1 for kw in keywords if kw in text)
    return hit_count / len(keywords)


def _fallback_keyword_search(
    user: User,
    question: str,
    db: Session,
    limit: int,
    extra_terms: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """数据库关键词降级搜索（只查知识条目 + 流程节点，按相关性过滤）

    文档检索由阶段二 `_search_documents` 统一负责，两条路径（Qdrant 正常/降级）行为一致。
    可见范围与服务层一致：管理员全局；普通用户 = 自己的 + 共享的。
    """
    # 用 jieba 提取中文关键词
    keywords = _extract_keywords(question, extra_terms=extra_terms)

    # 没有有效关键词（如"你好""谢谢"等闲聊），直接返回空，不返回任何知识库内容
    if not keywords:
        return []

    results = []

    # 1. 搜索结构化知识条目
    conditions = []
    for kw in keywords:
        conditions.append(KnowledgeItem.title.like(f'%{kw}%'))
        conditions.append(KnowledgeItem.system_or_material.like(f'%{kw}%'))
        conditions.append(KnowledgeItem.caution.like(f'%{kw}%'))
    item_query = db.query(KnowledgeItem).filter(
        KnowledgeItem.status == 'published',
        or_(*conditions)
    )
    item_scope = read_scope(KnowledgeItem, user)
    if item_scope is not None:
        item_query = item_query.filter(item_scope)
    items = item_query.limit(limit * 3).all()  # 多取一些，后面按相关性过滤

    for item in items:
        full_text = item.to_search_text()
        score = _compute_relevance(full_text, keywords)
        # 过滤低相关性结果
        if score < MIN_RELEVANCE_RATIO:
            continue
        results.append({
            "id": item.id,
            "title": item.title,
            "content": full_text,
            "source_type": "knowledge_item",
            "score": score,
        })

    # 2. 搜索流程节点（匹配流程名或节点名，返回节点描述+下一节点）
    flow_query = db.query(Flow)
    flow_scope = read_scope(Flow, user)
    if flow_scope is not None:
        flow_query = flow_query.filter(flow_scope)
    flows = flow_query.all()
    for flow in flows:
        sorted_nodes = sorted(flow.nodes, key=lambda n: n.order)
        for i, node in enumerate(sorted_nodes):
            # 检查流程名或节点名是否出现在问题中
            matched = False
            for kw in keywords:
                if kw in flow.name or kw in (node.name or ''):
                    matched = True
                    break
            if not matched:
                continue
            results.append(_flow_node_result(db, flow, node, sorted_nodes, i, score=0.9))

    # 按相关性分数降序排序，去重后取前 limit 条
    results.sort(key=lambda x: x["score"], reverse=True)
    results = _dedupe_results(results)[:limit]

    # 没有匹配到任何内容则返回空，不做兜底（避免无关问题显示引用来源）
    return results


def _flow_node_result(db: Session, flow: Flow, node: FlowNode,
                      sorted_nodes: List[FlowNode], i: int, score: float) -> Dict[str, Any]:
    """构建单个流程节点的检索结果（节点描述 + 下一步 + 关联知识条目）"""
    parts = [f"流程：{flow.name}"]
    parts.append(f"当前步骤：{node.name}")
    if node.description:
        parts.append(f"步骤说明：{node.description}")
    # 下一节点
    if i + 1 < len(sorted_nodes):
        next_node = sorted_nodes[i + 1]
        parts.append(f"下一步：{next_node.name}")
        if next_node.description:
            parts.append(f"下一步说明：{next_node.description}")
    else:
        parts.append("该步骤已是流程最后一步")

    # 关联知识条目
    if node.related_item_ids:
        related_items = db.query(KnowledgeItem).filter(
            KnowledgeItem.id.in_(node.related_item_ids)
        ).all()
        if related_items:
            parts.append("关联资料：")
            for ri in related_items:
                parts.append(f"  - {ri.title}：{ri.to_search_text()[:200]}")

    return {
        "id": node.id,
        "title": f"{flow.name} - {node.name}",
        "content": "\n".join(parts),
        "source_type": "flow_node",
        "score": score,
    }


# ---------------------------------------------------------------------------
# 多轮对话上下文：历史加载 + 追问识别 + 问题改写
# ---------------------------------------------------------------------------

# 历史消息参与提示词时单条截断长度，防止上下文超长
HISTORY_MESSAGE_MAX_LENGTH = 600

# 追问特征词：包含这些词的输入大概率是对上一轮回答的补充/修正
FOLLOWUP_MARKERS = (
    '漏', '缺', '少了', '还有', '继续', '补充', '刚才', '上面', '刚刚',
    '这个', '那个', '它', '追加', '不全', '完整', '错', '不对', '呢',
)

CONDENSE_PROMPT = """你是一个检索查询改写器。用户正在与知识库问答助手多轮对话，当前输入可能是依赖上下文的简短追问。
请结合对话历史，把当前输入改写成一个不依赖上下文、可独立用于知识库检索的完整问题。

规则：
1. 如果当前输入本身已是独立问题，原样返回
2. 改写结果必须保留用户当前输入的核心意图（例如用户说"漏了一条"，意图是找出上一轮回答遗漏的内容）
3. 只输出改写后的问题本身，不要任何解释、前缀或引号
"""


def get_recent_history(
    db: Session, user_id: str, turns: int | None = None
) -> List[Dict[str, str]]:
    """取当前用户最近几轮对话（user/assistant 交替，按时间正序）

    `turns` 不传时按记忆层配置取（改造 4）：开启记忆 = `AGENT_MEMORY_SHORT_TURNS`
    （默认 6 轮），关闭记忆 = 改造前的 4 轮。这样"关掉开关即回到原行为"
    只需要改一处配置，不必回来改调用方。
    """
    if turns is None:
        turns = _memory_short_turns()
    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user_id, ChatMessage.role.in_(('user', 'assistant')))
        .order_by(ChatMessage.created_at.desc())
        .limit(turns * 2)
        .all()
    )
    msgs.reverse()
    return [
        {'role': m.role, 'content': (m.content or '')[:HISTORY_MESSAGE_MAX_LENGTH]}
        for m in msgs
    ]


def _memory_short_turns() -> int:
    """短期窗口轮数（记忆模块不可用时退回改造前的 4 轮）"""
    try:
        from app.agent import memory

        return memory.short_turns()
    except Exception as e:  # noqa: BLE001
        print(f"[提示] 读取记忆配置失败，使用默认 4 轮: {e}")
        return 4


def _memory_block(user: User, question: str, db: Session) -> str:
    """长期记忆块（改造 4）：关闭记忆或无可用记忆时返回空串"""
    try:
        from app.agent import memory

        return memory.memory_block(db, user, question)
    except Exception as e:  # noqa: BLE001 —— 记忆是加分项，失败就当没有
        print(f"[提示] 组装长期记忆失败（忽略）: {e}")
        return ""


def _recall_terms(db: Session, user: User) -> List[str]:
    """L2 偏好里可参与查询扩展的实体词（流程名/系统名）"""
    try:
        from app.agent import memory

        return memory.preference_terms(db, getattr(user, "id", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[提示] 读取偏好词失败（忽略）: {e}")
        return []


def _looks_like_followup(question: str) -> bool:
    """启发式判断是否为追问：包含指代/补充类特征词，或极短（≤6字）的输入"""
    q = question.strip()
    if not q:
        return False
    if len(q) <= 6:
        return True
    return any(mk in q for mk in FOLLOWUP_MARKERS)


def condense_question(user: User, question: str, history: List[Dict[str, str]]) -> str:
    """把追问改写为可独立检索的问题

    优先用 LLM 改写；LLM 不可用时降级为「原问题 + 上一轮提问」拼接，
    保证检索关键词里至少能带上上一轮的主题词。
    """
    last_user_q = next(
        (h['content'] for h in reversed(history) if h['role'] == 'user'), ''
    )
    try:
        llm = get_llm_for_user(user)
        history_text = '\n'.join(
            f"{'用户' if h['role'] == 'user' else '助手'}：{h['content']}"
            for h in history
        )
        messages = [
            SystemMessage(content=CONDENSE_PROMPT),
            HumanMessage(
                content=f"对话历史：\n{history_text}\n\n当前输入：{question}\n\n请输出改写后的问题："
            ),
        ]
        resp = llm.invoke(messages)
        rewritten = (resp.content or '').strip().strip('"“”‘\'')
        return rewritten or question
    except Exception as e:
        print(f"[提示] 追问改写失败，使用拼接降级: {e}")
        if last_user_q and last_user_q != question:
            return f"{question}（上下文：{last_user_q}）"
        return question


def _prepare_question(user: User, question: str, db: Session):
    """返回 (改写后的检索问题, 对话历史)"""
    history = get_recent_history(db, user.id) if db is not None else []
    search_question = question
    if history and _looks_like_followup(question):
        search_question = condense_question(user, question, history)
    return search_question, history


def _build_answer_messages(
    question: str,
    context: str,
    history: List[Dict[str, str]],
    memory_block: str = "",
) -> list:
    """组装带对话历史的回答消息列表

    `memory_block`（改造 4）是长期记忆块（会话摘要 + 相关历史片段 + 用户偏好）。
    它被拼进**最后一条** HumanMessage 的开头，而不是塞进 SystemMessage：
    - 放 system 会污染所有行为（含闲聊、写操作约束），放用户消息只是一份"附加材料"；
    - 块内自带"仅供参考、必须以知识库内容为准"的约束，避免变成新的幻觉源。
    """
    messages = [SystemMessage(content=SYSTEM_PROMPT)]
    for h in history:
        if h['role'] == 'user':
            messages.append(HumanMessage(content=h['content']))
        else:
            messages.append(AIMessage(content=h['content']))
    head = f"{memory_block}\n\n" if memory_block else ""
    messages.append(HumanMessage(content=f"{head}知识库内容：\n{context}\n\n用户问题：{question}"))
    return messages


def build_context(references: List[Dict[str, Any]]) -> str:
    """将检索结果拼接为上下文"""
    if not references:
        return ""
    parts = []
    type_labels = {
        "knowledge_item": "知识条目",
        "document": "文档片段",
        "flow_node": "流程节点",
    }
    for i, ref in enumerate(references, 1):
        source_label = type_labels.get(ref.get("source_type"), "参考资料")
        parts.append(f"【参考{i}·{source_label}】{ref['content']}")
    return "\n\n".join(parts)


def get_answer_sync(user: User, question: str, db: Session) -> Dict[str, Any]:
    """同步问答（非流式）"""
    search_question, history = _prepare_question(user, question, db)
    memory_block = _memory_block(user, question, db)
    references = search_relevant_items(
        user, search_question, db, extra_terms=_recall_terms(db, user)
    )

    if not references:
        return {
            "answer": "抱歉，知识库中暂无相关内容。",
            "references": [],
        }

    context = build_context(references)
    llm = get_llm_for_user(user)

    messages = _build_answer_messages(question, context, history, memory_block=memory_block)
    response = llm.invoke(messages)

    return {
        "answer": response.content,
        "references": [
            {
                "item_id": r["id"],
                "title": r["title"],
                "score": r.get("score", 0),
                "source_type": r.get("source_type", "knowledge_item"),
            }
            for r in references
        ],
    }


async def get_answer_stream(user: User, question: str, db: Session) -> AsyncGenerator[str, None]:
    """流式问答（SSE）"""
    search_question, history = _prepare_question(user, question, db)
    memory_block = _memory_block(user, question, db)
    references = search_relevant_items(
        user, search_question, db, extra_terms=_recall_terms(db, user)
    )

    if not references:
        yield "data: 抱歉，知识库中暂无相关内容。\n\n"
        yield "data: [DONE]\n\n"
        return

    context = build_context(references)
    try:
        llm = get_llm_for_user(user)
    except ValueError as e:
        # 配置类错误要作为一条回答返回，而不是抛异常让前端一直转圈
        yield f"data: {e}（系统级模型配置由超管在【API Key 配置】页统一维护）\n\n"
        yield "data: [DONE]\n\n"
        return

    messages = _build_answer_messages(question, context, history, memory_block=memory_block)

    # 先返回引用信息
    ref_data = {
        "references": [
            {
                "item_id": r["id"],
                "title": r["title"],
                "score": r.get("score", 0),
                "source_type": r.get("source_type", "knowledge_item"),
            }
            for r in references
        ]
    }
    yield f"data: {json.dumps(ref_data, ensure_ascii=False)}\n\n"

    # 流式输出回答
    async for chunk in llm.astream(messages):
        if chunk.content:
            yield f"data: {chunk.content}\n\n"

    yield "data: [DONE]\n\n"
