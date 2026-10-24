"""分诊（Router）：判断问题意图，决定要不要检索、开放哪些工具

M1 采用**纯规则分诊**（零 token、零额外延迟、可测试）：
LLM 分诊留到后续里程碑——规则能覆盖当前 4 类意图的绝大多数输入，
而多一次 LLM 调用会让每条消息都多几百毫秒延迟。

四类意图：
- `smalltalk` 寒暄/致谢/问助手身份 → 不检索、不给工具，直接回答
  （现状是这类输入也会走一遍检索，白烧 embedding 与向量查询）
- `kb_qa`     业务知识问答 → 预检索 + 可用补充检索工具（主路径）
- `flow_nav`  流程进度问答 → 额外放开流程工具
- `action`    写操作意图（记待办等）→ 不给检索工具，给低风险写工具

保守原则：**除明确的闲聊外一律按业务问题处理**（默认 `kb_qa`），
宁可多检索一次，也不能把业务问题误判成闲聊而丢掉检索。
"""
from __future__ import annotations

import re
from typing import List, Optional

from langchain_core.tools import BaseTool

from app.agent.tools import get_tools
from app.models import User

INTENT_SMALLTALK = "smalltalk"
INTENT_KB_QA = "kb_qa"
INTENT_FLOW_NAV = "flow_nav"
INTENT_ACTION = "action"

INTENT_LABELS = {
    INTENT_SMALLTALK: "寒暄/助手身份",
    INTENT_KB_QA: "业务问答",
    INTENT_FLOW_NAV: "流程导航",
    INTENT_ACTION: "写操作",
}

# 需要检索的意图
RETRIEVAL_INTENTS = (INTENT_KB_QA, INTENT_FLOW_NAV)

# 去掉标点与空白后再做闲聊精确匹配，避免「你好呀～」「谢谢！」漏判
_PUNCT_PATTERN = re.compile(r"[\s？?，,。.！!、；;：:“”\"'‘’（）()\[\]【】{}~～\-—_]+")
# 句尾语气词：「你好呀」「谢谢啦」应当等价于「你好」「谢谢」
_TRAILING_PARTICLES = "呀啊哦嗯啦了吧哈嘞"

# 整句完全匹配才算闲聊（保守：不参与模糊匹配，防止误伤业务问题）
_SMALLTALK_UTTERANCES = {
    "你好", "您好", "hi", "hello", "hey", "在吗", "在不在", "有人吗",
    "谢谢", "多谢", "感谢", "谢谢啦", "辛苦了", "好的", "好", "收到", "明白了",
    "早上好", "中午好", "下午好", "晚上好", "晚安", "再见", "拜拜",
    "你是谁", "你叫什么", "你叫什么名字", "你是做什么的", "你是干什么的",
    "你能做什么", "你会什么", "你能干什么", "你有什么功能", "怎么用",
    "介绍一下你自己", "自我介绍", "帮助", "help",
}

# 写操作意图关键词
# 只收"明确的动词短语"，避免把"怎么把资料存进知识库？"这类**咨询写法**的问题也判成写操作；
# 万一误判，action 意图的工具集里仍保留 knowledge_search，检索能力不会丢。
_ACTION_KEYWORDS = (
    "记一下", "记一条", "记一条待办", "记个", "记录一下", "帮我记", "帮我记录",
    "提醒我", "加个待办", "添加待办", "新建待办", "建个待办", "创建待办",
    "备忘一下", "加进待办",
    # 知识条目写操作（设计方案里 action 意图的典型触发："存进知识库"）
    "存进知识库", "存入知识库", "存到知识库", "录入知识库", "加入知识库",
    "整理成条目", "整理成知识条目",
)

# 流程导航关键词
_FLOW_KEYWORDS = (
    "流程", "步骤", "下一步", "接下来", "接着做", "节点", "进度", "到哪一步", "第几步",
)

# 各意图开放的工具（M1 只开放低风险写工具 memo_create；
# 知识条目的写操作（create_draft_item / update_item）需人工审批链路，暂不开放）
_INTENT_TOOLS = {
    INTENT_SMALLTALK: [],
    INTENT_KB_QA: [
        "knowledge_search",
        "document_search",
        "get_item",
        "list_flows",
        "flow_next_step",
    ],
    INTENT_FLOW_NAV: [
        "list_flows",
        "flow_next_step",
        "knowledge_search",
        "get_item",
    ],
    INTENT_ACTION: [
        "memo_list",
        "memo_create",
        "knowledge_search",
    ],
}


# 高风险写工具：只有开启 HITL（人工审批）时才会交给模型，
# 且执行前一定会被 interrupt 拦住——"模型可以起草，但不能替用户拍板落库"。
HITL_WRITE_TOOLS = ("create_draft_item", "update_item")


def normalize(question: str) -> str:
    """小写化 + 去标点空白 + 去句尾语气词，用于闲聊精确匹配"""
    text = _PUNCT_PATTERN.sub("", (question or "").lower())
    return text.rstrip(_TRAILING_PARTICLES)


def classify_intent(question: str, history: Optional[List[dict]] = None) -> str:
    """规则分诊：返回四类意图之一

    history 目前不参与判定（保留参数以便后续加入「上一轮是写操作」这类上下文规则）。
    顺序很重要：先排掉纯闲聊，再判写操作，最后判流程；其余一律业务问答。
    """
    text = (question or "").strip()
    if not text:
        return INTENT_SMALLTALK

    normalized = normalize(text)
    if normalized in _SMALLTALK_UTTERANCES:
        return INTENT_SMALLTALK

    if any(kw in text for kw in _ACTION_KEYWORDS):
        return INTENT_ACTION

    if any(kw in text for kw in _FLOW_KEYWORDS):
        return INTENT_FLOW_NAV

    return INTENT_KB_QA


def should_retrieve(intent: str) -> bool:
    """该意图是否需要预检索（建立证据基线）"""
    return intent in RETRIEVAL_INTENTS


def tools_for_intent(
    intent: str, user: Optional[User] = None, allow_hitl_writes: bool = False
) -> List[BaseTool]:
    """按意图与角色返回可用工具

    `allow_hitl_writes=True` 时，action 意图额外获得高风险写工具
    （仍受角色限制：普通用户拿不到管理员专属工具）。
    """
    names = list(_INTENT_TOOLS.get(intent, _INTENT_TOOLS[INTENT_KB_QA]))
    if allow_hitl_writes and intent == INTENT_ACTION:
        names += [n for n in HITL_WRITE_TOOLS if n not in names]
    available = {t.name: t for t in get_tools(user=user)}
    return [available[n] for n in names if n in available]


def intent_label(intent: str) -> str:
    return INTENT_LABELS.get(intent, intent)
