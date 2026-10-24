# 新员工入职 AI 助手 · Agent 化改造设计方案

> 版本：v1.0 | 日期：2026-10-07
> 定位：把当前的「固定工作流 RAG 应用」升级为「可自主决策、可自我校验的 Agent 系统」
> 原则：**不推倒重来**。复用已有的 60 个测试用例、两阶段检索、Qdrant/向量服务、评测体系，只改「编排层」

---

## 一、现状诊断：为什么现在还不是 Agent

先把结论摆在前面，避免自欺欺人。

| 维度 | 当前实现 | 证据（代码位置） | 是否具备 Agent 特征 |
|------|----------|------------------|---------------------|
| 控制流 | 写死的单向管线：改写问题 → 检索一次 → 拼 prompt → LLM 生成 → 结束 | `chat_service.get_answer_stream()` | ❌ 无循环、无分支决策 |
| 工具使用 | 无。检索结果由代码拼进 prompt，模型没有选择权 | 全仓 `grep bind_tools` 无结果 | ❌ 无 tool calling |
| 自主性 | 是否检索、检索几次、用哪几个来源，全由代码决定 | `search_relevant_items()` 固定两阶段 | ❌ LLM 不参与规划 |
| 校验 | 无。引用来源 = 检索结果，未验证是否真的支撑回答 | `ref_data` 直接取 `references` | ❌ 无 self-check |
| 记忆 | 仅最近 4 轮历史，超长单条截断 600 字，无摘要无长期记忆 | `get_recent_history(turns=4)` | ⚠️ 极简短期记忆 |
| 主动性 | 全部被动响应，无定时任务、无巡检 | — | ❌ 无 |
| 多步任务 | 不支持。一次问答 = 一次 RPC | — | ❌ |

**特别说明一个现存事实**：`app/services/flow_agent.py` 里定义了 `StateGraph`（`build_flow_graph()`），但真正被 API 调用的 `get_next_step_advice()` 注释写着"直接执行逻辑（不用 LangGraph 编译也可以，但为了展示 Agent 能力）"，**实际没有走图**。所以当前的 LangGraph 是装饰性的，不构成 Agent 能力。

**结论**：当前系统是 **LLM-powered workflow**（固定 DAG，人写死），不是 Agent。这个判断不变——本方案就是把它真正改造过去。

### 1.1 现有资产盘点（可复用的部分）

| 资产 | 位置 | 改造中的角色 |
|------|------|--------------|
| 两阶段混合检索（知识库 + IDF 加权文档检索） | `chat_service.search_relevant_items` | 直接包装成 `knowledge_search` 工具，算法零改动 |
| Qdrant 混合检索 + 降级 | `qdrant_service.hybrid_search` | 底层能力不变 |
| 流程节点补全、近重复去重 | `_augment_flow_nodes` / `_dedupe_keep_order` | 保留在工具内部 |
| 追问改写 | `condense_question` | 升级为 Agent 的 query rewrite 工具 |
| 文档抽取入库 | `ingest_service` | 包装成 `item_create_draft` 工具 |
| 流程下一步 | `flow_service.get_next_node` | 包装成 `flow_next_step` 工具 |
| 备忘录 / 待办 | `memo_service` | 包装成 `memo_*` 工具（Agent 可"顺手记一条待办"） |
| 评测 + 负反馈闭环 | `evaluation_service.run_evaluation` | 扩展为 Agent 评测 harness |
| 权限体系（user/admin/superadmin） | `app/core/roles.py` 的 `read_scope` | 工具层统一做数据边界裁剪 |
| 60 个回归用例 | `backend/tests/` | 保护性回归，改造期间必须全绿 |

---

## 二、目标形态

### 2.1 两个成熟度层级

```
L1 · 单体增强 Agent（本方案主线，投入 5~7 天）
    ┌─────────────────────────────────────────────┐
    │  用户提问                                    │
    │    ↓                                        │
    │  分诊 Router ──→ 闲聊/无关 → 直接回答          │
    │    ↓ 业务问题                                │
    │  规划 → 调用工具(可多轮) → 起草 → 自检 ──┐     │
    │         ↑___________________________|     │ 不通过则补检索/改问题重试
    │    ↓                                      │
    │  带校验标记的最终回答 + 引用                  │
    └─────────────────────────────────────────────┘

L2 · 多 Agent 协作（M5 可选，投入 2~3 天）
    Supervisor 调度 + 3 个专家 Agent + 共享黑板
```

**明确判断**：以当前业务规模（知识条目几十~几百条、单轮问答为主），**L1 的收益占 80%**，L2 的价值集中在"长任务 + 审批链路"（如"帮我把这份资料整理成条目并挂到校招流程上，我确认后发布"）。建议先把 L1 做扎实，L2 作为能力展示的下一跳。

### 2.2 改造后要达成的指标

| 指标 | 当前 | 目标 | 度量方式 |
|------|------|------|----------|
| 引用正确率（引用确实支撑回答） | 未度量（体感 ~70%） | ≥ 90% | Verifier 的 `citation_support_rate` |
| 答案完备率（该引的都引了） | 未度量 | ≥ 85% | 评测集 + LLM-as-judge |
| 检索命中率（现有口径） | 现有评测可跑 | 不下降 | `run_evaluation` 对比 fixed vs agent |
| 平均检索轮数 | 恒定 1 | 1.4~2.0（自适应） | `agent_runs.steps` 统计 |
| 端到端首字延迟 | ~1.5s | ≤ 2.5s（P50） | trace 埋点 |
| 单次问答 token 成本 | 1× baseline | ≤ 2× baseline（硬预算） | usage 累加 |

**硬约束**：改造过程必须满足「不劣化」——任何一版都可以通过环境变量一键回落到现有固定管线。

---

## 三、目标架构总览

```
┌───────────────────────────────────────────────────────────────────────────┐
│ 前端 Vue3                                                                  │
│  对话页 → SSE 事件流（text / references / tool_start / tool_end /          │
│           citation_check / done）                                          │
│  新增 AgentTrace 折叠面板：展示「分诊 → 检索 → 校验 → 生成」全过程          │
│  引用卡片新增核验徽标：✅ 已核验 / ⚠️ 部分支持 / ❌ 无支撑（默认不显示 ❌）    │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │ POST /api/chat/agent/stream（保留旧 /api/chat/stream）
┌───────────────────────────────▼───────────────────────────────────────────┐
│ Agent 运行时（新增 app/agent/）                                             │
│                                                                            │
│  ┌─────────┐   ┌──────────┐   ┌───────────────┐   ┌──────────┐            │
│  │ Router  │→ │ Planner  │→ │ Tool Executor │→ │ Verifier │            │
│  │ 分诊     │   │ 决定策略  │   │ (ToolNode)    │   │ 引用校验  │            │
│  └─────────┘   └──────────┘   └───────┬───────┘   └────┬─────┘            │
│       │                               │ 循环 ≤ N 轮     │ 不通过 → 回到     │
│       │ 闲聊/无关 → 直出                │                │ Planner(补检索)  │
│       ▼                               ▼                ▼                  │
│  ┌──────────────────────────────────────────────────────────────┐         │
│  │  State（LangGraph）+ Checkpoint（SQLite）                      │         │
│  │  messages / evidence / used_evidence / citations /            │         │
│  │  verification / budget / trace                                │         │
│  └──────────────────────────────────────────────────────────────┘         │
│                                                                            │
│  记忆：会话摘要 + 检索式历史 + 用户偏好        预算：工具调用/LLM 次数/超时    │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │ 工具注册表（权限感知）
┌───────────────────────────────▼───────────────────────────────────────────┐
│ Tool Registry（新增 app/agent/tools.py）                                    │
│  knowledge_search · document_search · flow_list · flow_next_step           │
│  item_get · item_create_draft · memo_create · memo_list · stats_snapshot   │
│  ── 每个工具 = 现有 service 层函数 + Pydantic 入参 + 统一权限裁剪 + 审计日志   │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
   现有 service 层           SQLite(元数据)          Qdrant(向量)
   chat/flow/ingest/memo    ＋新增 agent_runs        混合检索不变
   /evaluation/knowledge    ＋patrol_reports
```

---

## 四、核心改造项

### 改造 1：工具化改造（Tool Registry）

**这是整个改造的地基**——把现有 service 函数暴露成 LLM 可调用的工具，Agent 才有"手脚"。

新增 `backend/app/agent/tools.py`：

```python
"""工具注册表：把现有 service 能力暴露为 LLM 可调用的 tool。

设计约束：
1. 只做「包装」，不重写业务逻辑 —— 工具内部调用的还是现有 service 函数；
2. 每个工具入参用 Pydantic 定义，由 LLM 填参（LangChain 自动生成 JSON Schema）；
3. 权限在工具层统一裁剪：写类工具要求 role >= admin，检索类工具按 read_scope 过滤；
4. 每次调用写一条审计记录（agent_runs.steps），便于回放与调优。
"""
from langchain_core.tools import tool
from pydantic import BaseModel, Field


class KnowledgeSearchInput(BaseModel):
    query: str = Field(description="检索用的自然语言问题，需自包含、不依赖上下文")
    top_k: int = Field(default=8, ge=1, le=15, description="返回条数")


@tool("knowledge_search", args_schema=KnowledgeSearchInput)
def knowledge_search(query: str, top_k: int = 8) -> list[dict]:
    """检索知识库（结构化条目 + 流程节点 + 已入库文档片段）。

    这是最常用的工具。当用户问业务操作问题时优先调用；
    若结果不够，可换关键词再调一次（例如换成流程名或系统名）。
    """
    # 复用现有两阶段检索，算法零改动
    return chat_service.search_relevant_items(USER_CTX.user, query, db=USER_CTX.db, limit=top_k)
```

工具清单（**全部复用现有函数**）：

| 工具名 | 入参 | 复用实现 | 权限 | 典型触发 |
|--------|------|----------|------|----------|
| `knowledge_search` | query, top_k | `chat_service.search_relevant_items` | 所有 | 任何业务问题 |
| `document_search` | query | `chat_service._search_documents` | 所有 | 需要原文出处时 |
| `list_flows` | — | `flow_service` 查询 | 所有 | "有哪些流程" |
| `flow_next_step` | flow_id, current_node_id | `flow_service.get_next_node` | 所有 | "当前做完 X 了，下一步？" |
| `get_item` | item_id | `knowledge_service` | 所有 | 需要条目全文 |
| `create_draft_item` | title, steps, caution, … | `ingest_service` 的入库路径 | admin | "把这段资料存进知识库" |
| `update_item` | item_id, patch | `knowledge_service` | admin | "更新这条条目" |
| `memo_create` | title, content, priority, due_date | `memo_service.create_memo` | 所有 | "帮我记一条待办" |
| `memo_list` | folder_id?, keyword? | `memo_service.get_memos` | 所有 | "我有哪些待办" |
| `stats_snapshot` | — | `evaluation_service.get_stats` | admin | "知识库现在什么情况" |

**上下文注入方式**：LangChain 工具签名里不能带 `user`/`db`（会被序列化进 Schema）。用 `contextvars.ContextVar` 在请求入口注入 `USER_CTX`，工具内部读取——这是生产环境的标准做法，比把 user_id 当参数让模型填更安全（防越权）。

```python
# app/agent/context.py
USER_CTX: ContextVar[AgentContext] = ContextVar("user_ctx")

@dataclass
class AgentContext:
    user: User
    db: Session
    role: str
    trace: list[dict]   # 工具调用审计
```

⚠️ **注意**：`Session` 不能跨线程/异步任务共享。SSE 流式场景下，工具执行若在线程池里跑，需要用 `sessionmaker` 造独立 Session 或把 db 操作放在请求线程内完成（当前项目是同步 SQLAlchemy + FastAPI 同步依赖，`StreamingResponse` 里继续用同一 Session 是可以的，但要避免 `asyncio.to_thread`）。

---

### 改造 2：Agent 运行时（LangGraph 真正跑起来）

新增 `backend/app/agent/state.py`：

```python
from typing import Annotated, TypedDict, Literal
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    # 对话
    messages: Annotated[list[BaseMessage], add_messages]
    question: str                 # 用户原始问题
    search_question: str          # 改写后的检索问题
    # 意图
    intent: Literal["smalltalk", "kb_qa", "flow_nav", "action"]
    # 证据
    evidence: list[dict]          # 检索到的候选（id/title/content/source_type/score）
    used_evidence: list[dict]     # 被回答真正引用的子集
    citations: list[dict]         # 返回给前端的引用（含 verified 标记）
    # 校验
    verification: dict            # {coverage, unsupported_claims, decision}
    repair_rounds: int
    # 预算与可观测
    budget: dict                  # {max_tool_calls, max_llm_calls, deadline_ts}
    tool_calls: int
    llm_calls: int
    trace: list[dict]             # 全过程轨迹（落库 + 前端展示）
    answer: str
```

新增 `backend/app/agent/graph.py`（图结构）：

```python
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

def build_agent_graph(tools):
    g = StateGraph(AgentState)
    g.add_node("route", route_node)              # 分诊
    g.add_node("plan", plan_node)                # 规划：决定要不要补充检索、换什么词
    g.add_node("act", ToolNode(tools))           # 执行工具
    g.add_node("draft", draft_node)              # 起草回答（带引用编号）
    g.add_node("verify", verify_node)            # 引用校验
    g.add_node("finalize", finalize_node)        # 定稿（剥离无支撑句 + 落库）

    g.set_entry_point("route")
    g.add_conditional_edges("route", after_route, {
        "smalltalk": "finalize",     # 闲聊/无关：不检索，直接答
        "agent": "plan",
    })
    g.add_conditional_edges("plan", after_plan, {
        "act": "act",                # 还需检索 → 调工具
        "draft": "draft",            # 证据够了 → 起草
    })
    g.add_edge("act", "plan")        # 工具执行完回到规划（形成循环）
    g.add_edge("draft", "verify")
    g.add_conditional_edges("verify", after_verify, {
        "repair": "plan",            # 校验不通过 → 补检索（≤2 轮）
        "finalize": "finalize",
    })
    g.add_edge("finalize", END)
    return g.compile(checkpointer=SqliteSaver.from_conn_string("data/agent_checkpoints.db"))
```

**关键节点逻辑**：

**① route_node（分诊）** —— 用一次极廉价 LLM 调用（或规则优先）判断意图：
- `smalltalk`：问候、闲聊、"你是谁" → 不走检索，直接回答（解决当前"问候语也会检索"的浪费）
- `kb_qa`：业务问题 → 走完整 Agent
- `flow_nav`：问流程进度 → 工具白名单收敛为 `flow_*`
- `action`：写操作意图（"存进知识库""记条待办"）→ 写工具 + **HITL 审批卡片**

规则优先实现（省 token）：命中 `FOLLOWUP_MARKERS` 或长度 ≤ 6 且历史非空 → 直接 `kb_qa`；命中"你好/谢谢/你是谁"白名单 → `smalltalk`；只有模糊时才调 LLM。

**② plan_node（规划）** —— 判断证据是否充分，决定继续调工具还是起草：

```python
def plan_node(state: AgentState, config):
    # 预算守卫：超限直接收敛，不再调用 LLM
    if state["tool_calls"] >= state["budget"]["max_tool_calls"]:
        return {"intent": state["intent"]}  # after_plan 会走 draft

    prompt = PLAN_PROMPT.format(
        question=state["search_question"],
        evidence_brief=summarize(state["evidence"]),   # 只给标题+分数摘要，省 token
        rounds=state["repair_rounds"],
    )
    llm = get_llm_for_user(USER_CTX.get().user).bind_tools(TOOLS)
    msg = llm.invoke([...])
    return {"messages": [msg]}
```

`after_plan` 路由：`messages[-1].tool_calls` 非空 → 走 `act`；否则 → 走 `draft`。**这一步是"自主性"的真正来源**：模型自己决定"结果不够，我换个词再搜一次"。

**③ verify_node（校验）** —— 见改造 3。

**④ finalize_node** —— 定稿：剔除 `unsupported` 句子（或降级为"推测"表述）、生成 `citations`（带 `verified` 标记）、写 `agent_runs` 落库、组装 SSE 尾包。

**Checkpoint 与恢复**：`SqliteSaver` 让每个 `thread_id`（= chat 会话 id）的中断状态可恢复，`action` 类意图的 HITL 审批就靠它——用户确认后再 `graph.invoke(None, config)` 从中断点继续。

---

### 改造 3：引用校验（Verifier）—— 优先级最高

**为什么先做这个**：项目最大的业务痛点是"引用不相关"（上一轮已经为此做过两阶段检索 + `source_type` 分流），说明**引用质量是用户最敏感的验收点**。Verifier 直接命中痛点，且不需要改动检索算法。

新增 `backend/app/agent/verifier.py`：

```python
VERIFY_PROMPT = """你是引用校验员。下面是一段 AI 回答和它声称的证据来源。
请逐句判断回答中的每个事实性陈述是否被证据支持。

严格判定：
1. 证据里没有明确写到的，判为 unsupported（不要脑补、不要宽容）
2. 数字、系统名、时间、人名、流程顺序 错一点就算 unsupported
3. 常识性过渡句（"以下是操作步骤"）不算事实陈述，忽略

回答：
{answer}

证据：
{evidence}

输出 JSON：
{{"claims":[{{"text":"...","supported_by":["引用编号"],"supported":true}}],
  "unsupported":[ "..." ],
  "coverage": 0.0-1.0}}
"""

def verify_answer(answer: str, used_evidence: list[dict], user) -> dict:
    """两级校验：先规则，后 LLM（省钱）"""
    # 一级：规则校验（零成本，覆盖多数情况）
    #   - 回答中的数字/系统名/专有名词 是否在证据文本中出现
    #   - 引用编号是否真实存在于 used_evidence
    rule_result = _rule_based_check(answer, used_evidence)
    if rule_result["decision"] != "uncertain":
        return rule_result
    # 二级：LLM 校验（仅规则存疑时调用，可配开关）
    if not settings.VERIFIER_USE_LLM:
        return {**rule_result, "coverage": 0.75, "decision": "pass"}
    return _llm_verify(answer, used_evidence, user)
```

**决策规则**（写进 `after_verify`）：
- `coverage ≥ 0.85` → `finalize`（通过）
- `0.55 ≤ coverage < 0.85` 且 `repair_rounds < 2` → `repair`（回到 plan，带着"缺什么"的提示再检索一次）
- `coverage < 0.55` 或 `repair_rounds ≥ 2` → `finalize` **降级输出**：只保留有支撑的部分 + 明确告知"以下部分知识库未收录"

**前端体现**：`citations[].verified` 三态
- `verified: true` → ✅ 已核验（引用原文确实支撑回答）
- `verified: "partial"` → ⚠️ 部分支持
- 未通过校验的引用**直接不出现在引用列表**（宁少勿错，符合现有"文档严格过滤"的产品取向）

**落库与度量**：`chat_messages` 新增 `citation_coverage FLOAT`、`agent_trace_id`，评测页新增"引用正确率"卡片。这个数字是改造效果的硬证据，也是面试最能讲的指标。

---

### 改造 4：记忆升级（短期 → 三层记忆）

现状只有 4 轮历史拼接。升级为：

| 层级 | 内容 | 存储 | 实现 |
|------|------|------|------|
| L1 会话短期 | 最近 N 轮原文（N=6） | `chat_messages` 现有 | 复用 `get_recent_history`，turns 4→6 |
| L1.5 会话摘要 | 超长会话的滚动摘要 | 新增 `conversation_summaries` 表 | 超过 12 轮触发一次摘要（每 6 轮增量更新） |
| L1.6 检索式历史 | 与当前问题最相关的历史片段 | `chat_messages` + Qdrant | 把历史消息向量化存入独立 collection `chat_history`，按需召回 top-3（解决"20 轮前提过的细节想不起来"） |
| L2 用户长期偏好 | 常用流程、关注系统、岗位角色、常用词 | 新增 `user_memory` 表（JSON） | Agent 每轮结束后异步抽取（"用户常问校招，关注格慧招系统"） |
| L3 组织记忆 | 未答问题、缺口聚类、巡检报告 | 新增 `unanswered_questions` / `patrol_reports` | 见改造 5 |

**L2 的用法**：不直接进 system prompt 全文（省 token），而是作为**检索提示**——用户偏好中的流程名/系统名参与查询扩展，与现有 `SYNONYMS` 同义词扩展机制合并。

---

### 改造 5：主动巡检 Agent（从被动到主动）

这是当前系统完全缺失的能力，也是"Agent 感"最强的部分。

新增 `backend/app/agent/patrol.py` + 定时触发（用平台的定时自动化能力，每天 09:00 调 `POST /api/agent/patrol`）。

**四个检查器**：

| 检查器 | 规则 | 产出 |
|--------|------|------|
| 过期检查 | `status=published` 且 `updated_at` 超 180 天 | 待复核清单（按优先级排序） |
| 缺口聚类 | `unanswered_questions` 按 embedding 聚类（相似度 ≥ 0.8 归簇），簇内计数 ≥ 3 | **草稿条目**（LLM 生成 title/steps 骨架）+ "缺口报告" |
| 质量异常 | 某条目被引用 ≥ 5 次但 `useful` 率 < 50% | 条目改进建议（指出可能过时的步骤） |
| 一致性检查 | 同一流程节点的 `description` 与其关联条目内容矛盾（LLM 判定） | 冲突清单，人工裁决 |

**闭环**：巡检产出全部是 **`status=draft` 草稿 + 报告**，绝不自动发布——复用现有「草稿 → 人工复核 → 发布」链路。这条设计守住了「不引入错误知识」的底线。

**新增表**：

```sql
CREATE TABLE patrol_reports (
  id VARCHAR PRIMARY KEY,
  user_id VARCHAR NOT NULL,
  run_at DATETIME NOT NULL,
  stale_items JSON,        -- 过期条目
  gap_clusters JSON,       -- 缺口聚类（含生成的草稿 id）
  quality_issues JSON,     -- 质量异常
  consistency_issues JSON, -- 一致性冲突
  summary TEXT             -- LLM 生成的一句话报告
);

CREATE TABLE unanswered_questions (
  id VARCHAR PRIMARY KEY,
  user_id VARCHAR NOT NULL,
  question TEXT NOT NULL,
  retrieved_nothing BOOLEAN,  -- 是否完全没检索到
  created_at DATETIME
);
```

`unanswered_questions` 在 `finalize_node` 里自动写入（当 `evidence` 为空或 `coverage < 0.55` 时）——**零额外成本，顺手采集**。这是整个设计方案里"投入产出比最高"的一处。

---

### 改造 6：多 Agent 协作（L2，可选）

**先说什么时候才该上**（诚实评估）：

| 触发条件 | 是否满足 | 说明 |
|----------|----------|------|
| 存在跨多步骤、跨小时的长任务 | ⚠️ 部分 | "整理资料 → 挂流程 → 等审批 → 发布"是唯一场景 |
| 单 Agent 工具数 > 15 导致选择困难 | ❌ | 目前 10 个工具，单 Agent 完全够 |
| 需要并行处理（如同时查 5 个系统） | ❌ | 无外部系统对接 |
| 需要角色对抗提升质量 | ✅ | Verifier 已经是"对抗"雏形 |

**结论**：L2 的收益主要在**能力展示**与**长任务链路**，不是当前性能瓶颈。建议 M5 再上，且只做最小形态。

**最小形态设计**：

```
                ┌──────────────────┐
                │ Supervisor       │  只做路由 + 汇总，不带工具
                │ (调度 Agent)      │
                └────┬────────┬────┘
        handoff      │        │      handoff
        ┌────────────▼──┐  ┌──▼─────────────┐
        │ KnowledgeAgent│  │ FlowAgent      │  各带自己的工具子集
        │ 检索+回答+校验 │  │ 流程导航+下一步 │
        └───────┬───────┘  └────────┬───────┘
                │  共享黑板 State    │
                └──────────┬────────┘
                     ┌─────▼──────┐
                     │ IngestAgent │  抽取+入库草稿（写操作，需审批）
                     └────────────┘
```

**关键工程决策**：
1. **用共享 State，不用自由对话**。Supervisor 通过 `Command(goto="flow_agent")` 交接，而不是让 Agent 互相聊天——否则 token 爆炸且不可控。
2. **单一写权限**：只有 `IngestAgent` 能写库，且必须经 HITL 审批。防止多 Agent 并发写冲突。
3. **交接协议固定**：
   ```python
   class Handoff(BaseModel):
       to: Literal["knowledge", "flow", "ingest", "user"]
       reason: str
       payload: dict       # 已收集的上下文，避免下游重复检索
   ```
4. **上限**：Supervisor 最大交接 4 次，超限强制汇总返回。

---

## 五、数据模型变更汇总

```sql
-- 1. 对话消息扩展（幂等迁移，沿用 main.py 的 init_migrate 模式）
ALTER TABLE chat_messages ADD COLUMN citation_coverage FLOAT;
ALTER TABLE chat_messages ADD COLUMN agent_trace_id VARCHAR;

-- 2. Agent 运行轨迹（可观测性核心）
CREATE TABLE agent_runs (
  id VARCHAR PRIMARY KEY,
  user_id VARCHAR NOT NULL,
  question TEXT NOT NULL,
  intent VARCHAR,                 -- smalltalk/kb_qa/flow_nav/action
  mode VARCHAR,                   -- fixed | shadow | agent
  steps JSON,                     -- [{node, tool, args, ms, tokens, result_brief}]
  tool_calls INT DEFAULT 0,
  llm_calls INT DEFAULT 0,
  total_tokens INT DEFAULT 0,
  latency_ms INT,
  citation_coverage FLOAT,
  answer TEXT,
  created_at DATETIME
);

-- 3. 会话摘要
CREATE TABLE conversation_summaries (
  id VARCHAR PRIMARY KEY,
  user_id VARCHAR NOT NULL,
  summary TEXT,
  covered_message_count INT,
  updated_at DATETIME
);

-- 4. 用户长期记忆
CREATE TABLE user_memory (
  user_id VARCHAR PRIMARY KEY,
  data JSON,                      -- {flows:[], systems:[], topics:[]}
  updated_at DATETIME
);

-- 5. 未答问题（缺口采集）
CREATE TABLE unanswered_questions (...);   -- 见改造 5

-- 6. 巡检报告
CREATE TABLE patrol_reports (...);         -- 见改造 5
```

---

## 六、API 与前端变更

### 6.1 API（全部**新增**，旧接口不动 → 灰度安全）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat/agent/stream` | Agent 模式流式问答（SSE 扩展事件） |
| GET | `/api/agent/runs` | 运行轨迹列表（管理员，用于调优） |
| GET | `/api/agent/runs/{id}` | 单次运行完整轨迹 |
| POST | `/api/agent/patrol` | 手动触发巡检（定时任务也调它） |
| GET | `/api/agent/patrol/reports` | 巡检报告列表 |
| POST | `/api/agent/approve/{run_id}` | HITL：审批写操作，从中断点恢复 |

**SSE 事件协议（向后兼容）**：现有前端只认 `data: <文本>` 和首个 `data: {references:[...]}`。新协议的**所有新事件都是可选的额外行**，老前端忽略即可：

```
data: {"type":"tool_start","tool":"knowledge_search","args":{"query":"校招出差前工作"}}

data: {"type":"tool_end","tool":"knowledge_search","ms":420,"hits":6}

data: {"type":"citation_check","coverage":0.92,"dropped":1}

data: {"references":[{"item_id":"...","title":"...","source_type":"knowledge_item","verified":true}]}

data: 最终回答文本流式输出...

data: {"type":"done","agent_run_id":"..."}
```

### 6.2 前端

| 文件 | 改动 |
|------|------|
| `frontend/src/views/Chat.vue` | 新增 Agent 过程折叠面板（分诊/检索/校验步骤条）；引用卡片加核验徽标；写操作渲染审批卡片 |
| 新增 `frontend/src/components/AgentTrace.vue` | 轨迹可视化组件（步骤 + 耗时 + 工具名 + 命中数） |
| `frontend/src/views/admin/Evaluate.vue` | 新增「引用正确率」「平均检索轮数」「Agent/Fixed 对比」卡片 |
| 新增 `frontend/src/views/admin/AgentRuns.vue` | 轨迹列表（管理员调优用） |

---

## 七、评测体系升级

现有 `run_evaluation` 只测「检索命中率」，Agent 化后需要扩展：

```python
def run_evaluation(db, user_id, user, mode: str = "auto") -> dict:
    """mode: fixed（固定管线）| agent（Agent 模式）| auto（两者都跑并对比）"""
```

新增指标：

| 指标 | 定义 | 目标 |
|------|------|------|
| `retrieval_hit_rate` | 现有口径，期望条目是否被检索到 | 不下降 |
| `citation_support_rate` | 引用中被 Verifier 判定为"确实支撑"的比例 | ≥ 90% |
| `answer_completeness` | LLM-as-judge：回答是否覆盖期望条目的全部步骤 | ≥ 85% |
| `avg_tool_calls` | 平均工具调用次数 | 1.4~2.0 |
| `avg_latency_ms` / `avg_tokens` | 成本与性能 | 延迟 ≤ 2.5×、token ≤ 2× |

**LLM-as-judge 实现要点**：
- 用**不同模型**做裁判（避免自我偏好）：生成用 DeepSeek，裁判用通义千问
- 裁判 prompt 固定 + 输出 JSON（`{score: 1-5, missing: [], wrong: []}`）
- 每次评测固定跑同一测试集（≥20 条），结果落 `evaluation_results`，新增 `mode` 字段便于横向对比

**这是能力展示的加分项**：不是"我做了个 Agent"，而是"我做了个 Agent，并用 20 条测试集证明了引用正确率从 X% 提升到 Y%"。

---

## 八、分阶段落地路线图

| 里程碑 | 内容 | 交付物 | 验收标准 | 回滚方式 |
|--------|------|--------|----------|----------|
| **M0** | 工具化 + 上下文注入 + 轨迹落库（**行为不变**） | `app/agent/{tools,context,state}.py`、`agent_runs` 表 | 60 个回归用例全绿；工具单测通过 | 纯新增，无影响 |
| **M1** | ReAct 单 Agent（route + plan + act + draft）+ 灰度开关 | `app/agent/{graph,router}.py`、`/api/chat/agent/stream` | shadow 模式下 Agent 与 fixed 结果对比无劣化；`AGENT_MODE=on` 可切换 | `AGENT_MODE=off` 一键回落 |
| **M2** | Agent 运行时补全（finalize 定稿 + 检查点恢复 + HITL 写操作审批） | `app/agent/runtime.py`、`agent_runs.thread_id`、`/api/agent/approve/{id}` | 审批前不落库、审批后可从中断点恢复；118 用例全绿 | `AGENT_HITL_ENABLED=false` |
| **M3** | Verifier + 引用校验闭环（**优先级最高**） | `app/agent/verifier.py`、引用核验徽标 | `citation_support_rate` 可度量（实测 85.7%/14.3%/25.0%）；不相关引用被自动剔除 | `AGENT_VERIFIER_ENABLED=false` |
| **M4** | 三层记忆（短期 6 轮 + 滚动摘要 + 检索式历史 + 用户偏好 + 缺口采集） | `app/agent/memory.py`、`conversation_summaries`/`user_memory`/`unanswered_questions` 表、Qdrant `chat_history` | 20 轮长对话后仍能答出前文细节（实测埋在第 2 轮的细节被 0.949 召回） | `AGENT_MEMORY_ENABLED=false` 一键退回 4 轮历史 |
| **M5** | 主动巡检 Agent + 定时触发（消费 M4 采集的 `unanswered_questions`） | `app/agent/patrol.py`、`patrol_reports` 表、`/api/agent/patrol` | 能产出 ≥1 簇有效缺口草稿；不误发布（实测：4 条相似缺口聚成 1 簇并生成 draft 草稿，已发布条目数不变、草稿未进向量库） | `AGENT_PATROL_ENABLED=false` + 停用定时任务 |
| **M6**（可选） | 多 Agent Supervisor（最小形态） | `app/agent/supervisor.py` | 长任务（整理→挂流程→审批→发布）链路跑通 | 只用单 Agent 图 |

**灰度策略（关键设计）**：

```python
# app/core/config.py
AGENT_MODE = "shadow"   # off | shadow | on
# off    → 走现有固定管线（默认，安全）
# shadow → 两路都跑，用户只看到 fixed 结果，Agent 轨迹落库用于对比调优
# on     → 用户走 Agent 路径
```

`shadow` 模式是整个改造的安全带：在真实流量上验证 Agent 质量，**零用户风险**。这比"改完直接上线看运气"专业得多，也是方案里值得讲的设计取舍。

---

## 九、风险与应对

| 风险 | 具体表现 | 应对 |
|------|----------|------|
| **延迟上升** | Agent 多轮工具调用 + 校验，首字延迟可能翻倍 | 分诊先出结果；工具调用并行化；Verifier 规则优先；SSE 先推引用再推正文；硬超时 45s |
| **token 成本失控** | 循环调用无上限 | `budget` 硬预算（工具 ≤6 次、LLM ≤8 次）+ 每次调用累加拦截 + 结果缓存（embedding 与检索结果按规范化问题缓存） |
| **模型不支持 tool calling** | 用户自定义的模型可能是老模型 | 启动时探测 `bind_tools` 能力；不支持 → 自动回落 fixed 管线（`AGENT_MODE=off`），并在前端提示 |
| **Agent 幻觉/编造** | 自由规划后答非所问 | Verifier 兜底 + 无支撑句子剔除 + 无证据时明确说"未收录"（不降级为编造） |
| **权限越权** | 模型自己填 `user_id` 参数导致越权 | 上下文用 `ContextVar` 注入，**绝不把 user_id 暴露为工具参数**；写工具双层校验 role |
| **写操作误执行** | Agent 自动删条目/改流程 | 所有写操作必须 HITL 审批（LangGraph `interrupt`）；默认白名单只给读工具 |
| **状态不可恢复** | SSE 断开后流程丢失 | LangGraph Checkpoint（SQLite）按会话 `thread_id` 持久化 |
| **复杂度失控** | 过度设计、维护困难 | 严格按 M0→M5 推进，每步独立可交付；L2 明确"非必需" |
| **回归破坏** | 改坏现有问答 | 60 个用例做保护网；`AGENT_MODE=off` 可秒级回转 |

---

## 十、与现有代码的映射（可直接开工）

| 类型 | 文件 | 动作 |
|------|------|------|
| 新增 | `backend/app/agent/__init__.py` | 模块入口 |
| 新增 | `backend/app/agent/context.py` | `AgentContext` + `ContextVar` |
| 新增 | `backend/app/agent/tools.py` | 10 个工具的 LangChain 包装 |
| 新增 | `backend/app/agent/state.py` | `AgentState` TypedDict |
| 新增 | `backend/app/agent/router.py` | 分诊（规则优先 + LLM 兜底） |
| 新增 | `backend/app/agent/prompts.py` | PLAN / DRAFT / VERIFY / PATROL 四套 prompt |
| 新增 | `backend/app/agent/graph.py` | LangGraph 图 + Checkpoint |
| 新增 | `backend/app/agent/verifier.py` | 两级引用校验 |
| 新增 | `backend/app/agent/memory.py` | 摘要 + 检索式历史 + 用户偏好 |
| 新增 | `backend/app/agent/patrol.py` | 巡检四个检查器 |
| 新增 | `backend/app/models/agent_run.py` | `AgentRun` / `PatrolReport` / `UnansweredQuestion` 等 |
| 新增 | `backend/app/api/agent.py` | `/api/agent/*` 路由 |
| 修改 | `backend/app/core/config.py` | `AGENT_MODE` / `VERIFIER_ENABLED` / 预算参数 |
| 修改 | `backend/app/services/chat_service.py` | 抽出 `_prepare_question` 供 Agent 复用；检索函数保持签名兼容 |
| 修改 | `backend/app/api/chat.py` | 新增 `/api/chat/agent/stream`（旧路径完全不动） |
| 修改 | `backend/app/main.py` | `init_migrate` 补新表/新字段；启动时探测模型 tool calling 能力 |
| 修改 | `backend/app/models/chat_message.py` | 加 `citation_coverage` / `agent_trace_id` |
| 修改 | `backend/app/services/evaluation_service.py` | `run_evaluation` 加 `mode` 参数与引用指标 |
| 修改 | `backend/app/services/flow_agent.py` | 删除装饰性假图，统一走 Agent 图（或明确标注废弃） |
| 新增 | `backend/tests/test_08_agent_tools.py` | 工具层单测（权限裁剪、参数校验） |
| 新增 | `backend/tests/test_09_agent_graph.py` | 图路由单测（分诊规则、预算收敛、校验降级） |
| 修改 | 前端 `Chat.vue` + 新增 `AgentTrace.vue` | 过程可视化 + 引用徽标 + 审批卡片 |

---

## 十一、实施进度

### M0 · 工具化改造（已完成 2026-10-07）

**范围**：纯新增，零改动现有文件（不动任何 API、service、前端）。

| 交付物 | 说明 |
|--------|------|
| `backend/app/agent/__init__.py` | 包入口，导出上下文与注册表 |
| `backend/app/agent/context.py` | `AgentContext` + `ContextVar` 上下文注入 + 调用轨迹（`bind_context` / `agent_context` / `current_context` / `get_context_or_raise`） |
| `backend/app/agent/tools.py` | 10 个工具（6 读 + 3 写 + 1 管理）+ 统一包装器 + `get_tools(user)` / `describe_tools(user)` |
| `backend/tests/test_08_agent_tools.py` | 19 个回归用例 |
| 回归结果 | **79 passed**（含原有 60 个全部通过） |

**工具清单（全部复用现有 service 函数，逻辑零改动）**

| 工具 | 复用实现 | 权限 |
|------|----------|------|
| `knowledge_search` | `chat_service.search_relevant_items` | 所有 |
| `document_search` | `chat_service._search_documents` | 所有 |
| `list_flows` | `flow_service.get_flows` | 所有 |
| `flow_next_step` | `flow_service.get_next_node` / `get_related_items` | 所有（支持起点/中间/终点三态） |
| `get_item` | `knowledge_service.get_item` | 所有（沿用 read_scope） |
| `memo_list` | `memo_service.get_memos` | 所有（仅本人数据） |
| `memo_create` | `memo_service.create_memo` | 所有（仅本人数据） |
| `create_draft_item` | `knowledge_service.create_item` | 管理员（**强制 draft**，不能直接发布） |
| `update_item` | `knowledge_service.update_item` | 管理员（**不含 status**，发布属人工决策） |
| `stats_snapshot` | `evaluation_service.get_stats` | 管理员 |

**四条落地约定（M1 直接依赖）**

1. **只包装不重写** —— 检索算法、权限裁剪、向量同步全部沿用原实现，上线不改变既有行为；
2. **永不抛异常** —— 成功 `{"ok": True, ...}`、失败 `{"ok": False, "error": "..."}`，让模型看见失败原因自行决策，不打断整轮对话；
3. **权限在工具内校验** —— 写类工具要求管理员，读类沿用 `read_scope`（越权只得到「不存在或无权访问」，不泄露是否存在）；
4. **每次调用写 `AgentContext.trace`** —— 含工具名、参数、ok、摘要、耗时，业务失败（`ok=False` 未抛异常）同样记为失败，供后续质量度量。

**使用方式**

```python
from app.agent import agent_context
from app.agent.tools import TOOLS_BY_NAME

with agent_context(current_user, db) as ctx:
    result = TOOLS_BY_NAME["knowledge_search"].invoke({"query": "校招出差前准备"})
    print(result["count"], ctx.summary())   # ctx.summary() 可直接落 agent_runs 表
```

**M1 前置条件**：需安装 `langgraph-checkpoint-sqlite`（当前环境仅有 `langgraph-checkpoint`，`SqliteSaver` 尚不可用）。

---

### M1 · ReAct 单 Agent + 灰度开关（已完成 2026-10-07）

**新增文件**

| 交付物 | 说明 |
|--------|------|
| `backend/app/agent/state.py` | `AgentState`（消息/意图/证据/预算/轨迹）；**不放 ORM 对象与 Session**，保证可序列化 |
| `backend/app/agent/prompts.py` | 规划提示词 + 闲聊提示词 + 写操作约束（起草复用固定管线的 system prompt） |
| `backend/app/agent/router.py` | 规则分诊（4 类意图）+ 意图级工具白名单 |
| `backend/app/agent/graph.py` | LangGraph 图：route → retrieve →(plan ↔ act)→ draft |
| `backend/app/agent/runtime.py` | 模式分发、预算与降级、回答生成、轨迹落库、shadow 后台运行、两套事件流 |
| `backend/app/models/agent_run.py` | `agent_runs` 轨迹表（新表，`create_all` 自动创建） |
| `backend/app/api/agent.py` | `/api/agent/status`、`/tools`、`/run`、`/runs`、`/runs/{id}` |
| `backend/tests/test_09_agent_graph.py` | 24 个用例 |
| 回归结果 | **103 passed**（原有 60 + M0 19 + M1 24） |

**修改文件（增量、可回滚）**

| 文件 | 改动 |
|------|------|
| `app/core/config.py` | 新增 `AGENT_MODE` / 预算 / 快路径 / 检查点等配置（默认 `off`） |
| `app/models/__init__.py` | 导出 `AgentRun` |
| `app/api/chat.py` | 按 `AGENT_MODE` 分发；新增 `/api/chat/agent/stream` 富事件端点；**原有实现未改动** |
| `app/main.py` | 挂载 agent 路由 |
| `requirements.txt` | 增加 `langgraph-checkpoint-sqlite` |

**图的实际形状（与设计稿的差异及原因）**

```
route ──闲聊──> draft ──> END
  │
  └──业务/写操作──> retrieve ──命中≥5，快路径──> draft ──> END
                        │
                        └──证据薄──> plan ──要补检索──> act ──┐
                                      ↑_____________________|
                                      └──证据够/预算尽──> draft
```

三处刻意偏离设计稿的调整，都是为了"不劣化 + 不慢"：

1. **预检索由代码保证**（设计稿里由模型决定要不要检索）。业务问题先做一次确定性混合检索建立证据基线，模型只决定"是否补充"。失败面从"完全没检索"缩小到"少检索一次"。
2. **draft 节点不生成 token**，只组装最终提问消息；生成由 runtime 统一流式输出，流式/同步共用同一份消息。
3. **快路径（实测新增）**：预检索命中数 ≥ `AGENT_FAST_PATH_MIN_HITS`（默认 5）即跳过规划模型调用直接起草。真实库实测：**回答前耗时 23.9s → 4.9s，工具调用 4 次 → 1 次，引用完全一致**。

**实测数据（真实知识库，`AGENT_MODE=on` 单次运行）**

| 指标 | 改进前 | 改进后 |
|------|--------|--------|
| 回答前耗时 | 23 953 ms | **4 915 ms** |
| 工具调用次数 | 4（预算打满才停） | **1** |
| 引用条数 | 7 | 7（一致） |
| 预检索本身耗时 | ~4 600 ms | ~4 600 ms（下一步优化目标） |

**降级路径（四种失败都不让用户看到报错，均有测试覆盖）**

| 失败场景 | 行为 |
|----------|------|
| 规划模型调用报错 | 用已有证据直接起草 |
| 规划调用超时（默认 20s 软超时） | 同上 |
| 模型不支持 tool calling | 降级为"仅预检索"，答案仍带引用 |
| 工具绑定/建图/跑图 任意异常 | 整体回落固定管线；预检索无命中时返回与固定管线一字不差的兜底话术 |

**灰度开关用法**

```bash
# backend/.env
AGENT_MODE=off       # 默认：行为与改造前完全一致
AGENT_MODE=shadow    # 用户看老结果，Agent 后台只读跑一遍并落轨迹（配 AGENT_SHADOW_RATE 采样）
AGENT_MODE=on        # 用户走 Agent；SSE 协议不变，前端无需改动
```

**已明确留给后续里程碑的部分**

- 富事件流（`/api/chat/agent/stream`）的过程事件目前在生成开始前一次性下发；逐步实时推送需改用 `astream_events` 回调。
- 引用校验（Verifier）仍未做——当前引用质量仍等于检索质量，未做逐句校验（见 M3）。
- HITL 的**审批 UI**（前端审批卡片）未做，需通过 `POST /api/agent/approve/{run_id}` 或后续管理页操作。

---

### M2 · Agent 运行时补全：finalize + 检查点恢复 + HITL 写操作审批（已完成 2026-10-07）

**为什么单独做这一步**：M1 把图跑起来了，但设计稿里 改造 2 的三件事只完成了一半——
① `finalize` 定稿节点缺失（引用在 draft 里临时拼的）；② `verify`/`finalize` 的位置没留出来；
③ **检查点与 HITL 写操作审批**（设计稿明确写"action 类意图的 HITL 审批就靠它"）尚未开放。
本步把这三件事补齐，且全部为增量改动。

**图的形状（与 M1 的差异）**

```
route ──闲聊──> draft ──┐
  │                     │
  └──业务/写操作──> retrieve ──命中≥5，快路径──> draft ──┤
                        │                              ├──> finalize ──> END
                        └──证据薄──> plan ──补检索──> act ──┐
                                      ↑___________________|
                                      └──证据够/预算尽──> draft
```

新增 `finalize` 节点，**所有路径都收敛到它**：引用与"知识库缺口"标记只在这一处产生，
杜绝"引用在多个节点里各拼一遍"的漂移；同时把 `verification` 字段占好位，
改造 3 的 Verifier 插进来即可，**不必再动图结构**。

**HITL 写操作人工审批（核心新增）**

| 环节 | 实现 |
|------|------|
| 触发 | `action` 意图 + `AGENT_HITL_ENABLED=true` 时，`create_draft_item` / `update_item` 才交给模型 |
| 拦截 | `act` 节点在**执行前**调 `interrupt({writes:[...]})`，整轮图挂起并落检查点 |
| 落库 | 未审批前**一条数据都不会写**（实测：审批前 0 条 → 审批后 1 条） |
| 恢复 | `POST /api/agent/approve/{run_id}`，用 `Command(resume=...)` 从同一 `thread_id` 继续 |
| 答复 | `{"approved": true}` 全批 / `false` 全拒 / `{"approved_call_ids":[...]}` 精确批 |
| 审计 | 原记录置 `approved`/`rejected`，恢复结果另写一条 `mode=approve` 轨迹 |
| 安全 | 只能审批自己的挂起操作（超管可代办）；`thread_id` 落 `agent_runs` |

**默认关闭，零影响**：`AGENT_HITL_ENABLED=false` 时，写工具不在任何意图的白名单里，
HITL 也不会挂起——行为与改造前**完全一致**。

**顺带补的一个真实缺口**：设计方案里 `action` 意图的例子就写着"存进知识库"，
但 M1 的关键词表里没有这类表述，实测「帮我把资料存进知识库」被判成 `kb_qa`，
**HITL 在真实场景根本触发不到**。已补 `存进/存入/存到/录入/加入知识库`、`整理成条目` 等强动词短语
（只收明确写法，避免把"怎么存进知识库？"这类咨询也判成写操作）。

**新增/修改清单**

| 类型 | 文件 | 动作 |
|------|------|------|
| 修改 | `app/agent/state.py` | 新增 `citations` / `verification` / `unanswered` / `pending_writes` / `approval` / `finished_at` |
| 修改 | `app/agent/graph.py` | 新增 `finalize` 节点；`act` 节点接入 `interrupt()` HITL；`draft → finalize → END` |
| 修改 | `app/agent/runtime.py` | 检查点在 `AGENT_USE_CHECKPOINT or AGENT_HITL_ENABLED` 时启用；`thread_id` 会话化；新增 `resume_run()`；中断检测与 `pending_writes` |
| 修改 | `app/agent/router.py` | `HITL_WRITE_TOOLS`；`tools_for_intent(..., allow_hitl_writes=)`；补写操作关键词 |
| 修改 | `app/models/agent_run.py` + `app/main.py` | `agent_runs.thread_id` 字段 + 幂等迁移 |
| 修改 | `app/api/agent.py` | 新增 `POST /api/agent/approve/{run_id}` + 审批请求体 |
| 修改 | `app/core/config.py` | `AGENT_HITL_ENABLED` / `AGENT_HITL_TOOLS` / `AGENT_HITL_MAX_PENDING` |
| 新增 | `backend/tests/test_10_agent_approval.py` | 15 个用例 |
| 回归结果 | — | **118 passed**（M0 19 + M1 24 + 原有 60 + 新增 15） |

**验证方式**

- 离线回归：假模型 + 内存检查点，覆盖"中断/批准/拒绝/检查点关闭不中断/thread_id 落库/接口 404·409·403"；
- 真实冒烟（真实 SQLite 检查点 + 真实库）：`action` 分诊 → 挂起 `create_draft_item` →
  **审批前落库 0 条** → 审批通过恢复 → **落库 1 条** → 清理归零；
- 不劣化验证：`kb_qa` 问题引用仍为 7 条，与 M1 实测完全一致。

**HITL 用法**

```bash
# backend/.env
AGENT_HITL_ENABLED=true          # 打开写操作人工审批（自动启用检查点）
AGENT_HITL_TOOLS=create_draft_item,update_item
```

```bash
# 1) 发起（管理员）：会返回 waiting_approval=true 与 pending_writes
POST /api/agent/run?question=帮我把校招出差准备的资料存进知识库
# 2) 审批并恢复
POST /api/agent/approve/{run_id}   {"approved": true}
```

---

### M3 · 引用校验 Verifier（已完成 2026-10-07）

**为什么先做这个**：项目最大的业务痛点是"引用不相关"——用户明确反馈过"点开引用的
『校园招聘接洽函』跳到知识库显示条目不存在""回答引用的资料跟问题无关"。
在做 M0~M2 的过程中已经为此做过两阶段检索与 `source_type` 分流跳转，
但**始终没有一个数字能回答"引用到底准不准"**。Verifier 就是把这个体感问题变成指标。

**落地形状（两级校验，代价从低到高）**

新增 `backend/app/agent/verifier.py`：

| 级别 | 触发条件 | 代价 | 做什么 |
|------|----------|------|--------|
| 一级：规则 | 永远执行 | 0（纯正则，无网络） | 抽"强事实"（数字带单位/百分比/日期/三位以上数字/英文缩写/书名号专名），看是否出现在证据原文里 → 命中率即 coverage |
| 二级：LLM | 仅当规则覆盖率落在 `0.55~0.85` 的中间地带 | 1 次模型调用（默认关闭） | 逐句判断陈述是否被证据支撑，输出 JSON |

**两个使用时机**（这是本节最关键的工程判断，两者做的事完全不同）

1. **生成前 `verify_evidence(question, evidence)`** —— 校验"证据能不能回答问题"。
   问题里的硬信息在证据中缺失 → 说明检索跑偏了 → 触发**回炉补检索**（唯一有效的补救时机，
   答案一旦生成就救不回来了）。问题本身不含硬信息时返回 `uncertain → 放行`，不会无谓多检索一轮。
2. **生成后 `verify_answer(answer, evidence)`** —— 校验"答案的事实有没有依据" +
   **引用三态过滤**。这是用户直接看到的部分：回答实际没用到的来源会被**剔除**，
   只留真正被用到的（宁少勿错，与项目既有的"文档严格过滤"取向一致）。

**图的形状（在 M2 基础上插入 verify 节点）**

```
route ──闲聊──> draft ──> verify ──> finalize ──> END
  │                            │
  └──业务──> retrieve ──> plan ─┴──校验不合格且未超回炉上限──> plan（回边，带"缺什么"提示）
                ↑______act______|        └──通过/超上限──> finalize
```

**实测踩出来的三个真实缺陷（都不是设计时能想到的）**

1. **引号误判把覆盖率从 100% 压到 25%**
   最初的规则把 `“双引号”` 里的内容也当"专名"抽取。但模型很爱用引号做**强调**
   （"关于‘校招出差前’的准备工作"），这些被当成"证据里没有的事实"，
   直接触发**错误的降级标记**。修复：只收书名号 `《》` 与中文直角引号 `「」`（基本只用于专名）。
2. **"中文业务专名"的正则抽取必须放弃**
   试过用 `[汉字]{2,6}(系统|接洽函|流程)` 抽系统名/表单名，结果把
   "但涉及接洽函""向学校发送的接洽函"整段前缀吞进来。**假阳性比漏判更糟**——
   它会让覆盖率虚低并给出错误结论，直接污染度量指标。规则层只做它能可靠判断的事。
3. **答案说"知识库暂无相关内容"却配了一条引用**
   引用过滤的"至少保留 1 条"保底规则，在"知识库答不上来"的场景下会造出自相矛盾的组合
   （用户看到"暂无相关内容"旁边挂着来源）。修复：识别无内容话术后**不保底**，返回空引用。

**指标口径的一次修正（值得单独说明）**

原设计只定义了 `coverage`（硬信息覆盖率）作为引用正确率。实测发现：
**中文业务问答的回答里往往一个数字都没有**（"需完成 6 项准备工作" 会被模型写成中文），
三题实测的 `coverage` 全是空值——指标形同虚设。

因此补了主指标 **`citation_support_rate`（引用支撑率）= 保留引用 ÷ (保留 + 剔除)**。
它**不依赖回答里有没有硬信息**，直接度量"检索出来的东西有多少真的被答案用到"，
这正是"引用不相关"最贴切的量化。`coverage` 降为辅助指标（有硬信息时才有值，
且 `verified` 与 `total` 分开统计，"覆盖率低"和"根本没校验"不会混为一谈）。

**真实库实测（三题，真实模型 + 真实检索）**

| 问题 | 候选引用 | 保留 | 剔除 | 引用支撑率 |
|------|---------|------|------|-----------|
| 校招出差前需要做哪些准备 | 7 | 6 | 1 | **85.7%** |
| 校招流程中下一步该做什么 | 7 | 1 | 6 | **14.3%** |
| 校园招聘接洽函怎么处理 | 8 | 2 | 6 | **25.0%** |

第 2、3 行的低支撑率**不是 bug 而是有价值的诊断信号**：流程节点类引用与答案的
相关性确实很差，说明**检索环节仍有优化空间**（此前完全看不出来）。
这正是"把体感变成数字"的意义——改造 3 交付的不只是过滤，更是一把尺子。

**前端体现**

`citations[].verified` 三态徽标：`true` → 绿框 ✅ 已核验；
`"partial"` → 橙框 ⚠️ 部分支持；未通过校验的引用**直接不出现在引用列表**。
消息顶部显示 `已核验 87%`（覆盖率），悬停给出解释。历史消息与固定管线的引用
没有 `verified`，按"未校验"呈现，行为与改造前一致。

**配置（默认值即为推荐值）**

```bash
AGENT_VERIFIER_ENABLED=true          # 总开关（false 一键回滚到改造前行为）
AGENT_VERIFIER_REPAIR_ENABLED=true   # 是否允许"回炉补检索"（关掉则只做生成后过滤）
AGENT_VERIFIER_PASS_THRESHOLD=0.85   # 规则覆盖率 ≥ 该值判通过
AGENT_VERIFIER_REPAIR_THRESHOLD=0.55 # < 该值判不合格（回炉 / 生成后降级标记）
AGENT_VERIFIER_MAX_REPAIR_ROUNDS=2   # 最多回炉几轮（防死循环）
AGENT_VERIFIER_USE_LLM=false         # 规则存疑时是否调模型复核（默认省 token）
AGENT_VERIFIER_LLM_TIMEOUT=15        # LLM 复核软超时（超时按放行处理）
AGENT_VERIFIER_MIN_CITATIONS=1       # 过滤引用时至少保留几条
```

**新增 API**

| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/api/agent/verify` | 离线校验（管理员）：对任意「回答 + 证据」跑同一套校验，用于调阈值与复盘。证据可直接给，也可给 `item_ids` 按可见范围取正文 |
| GET | `/api/evaluation/citation-stats` | 引用质量统计：`citation_support_rate` / `avg_coverage` / 达标率 / 保留与剔除引用数（`scope=all` 仅超管） |

**落库**

`chat_messages` 增 `citation_coverage` / `citation_dropped` / `agent_trace_id`；
`agent_runs` 增 `citation_coverage` / `citation_dropped`（幂等迁移，老库无需重建）。
Agent 路径的每次运行同时落一条 `agent_runs`，并把轨迹 id 回写到消息上，
管理员可从某条回答反查完整的决策轨迹。

**已知边界（诚实说明）**

- 中文数词不收（"三天" vs "3 天"）：等价改写需要语义判断，规则层强行匹配会引入误判。
  代价是少量漏判（→ 覆盖率偏低 → 可能多回炉一次检索），方向安全。
- 生成后的校验**不改写答案正文**：文本已经流式发给用户了，事后偷偷改是不诚实的。
  降级结论通过"引用三态 + 覆盖率 + 提示"表达。
- 回炉补检索只在生成前生效；生成后不通过时只标记 `degraded`，没有"重答"（那会翻倍延迟）。

**测试**：新增 `tests/test_11_agent_verifier.py`（45 用例，覆盖事实抽取的边界、
三分支决策、引用三态与保底、无内容话术、LLM 复核的四种失败路径、图内回炉与上限、
runtime 落库、API 权限与统计口径）。**全量 163 用例通过**（原 118 + 新 45）。

---

### M4 · 三层记忆（短期 → 三层记忆）（已完成 2026-10-07）

对应"改造 4"。改造前只有「最近 4 轮原文拼进 prompt」，有三个明确失效场景，
本里程碑逐条对应解决：

| 失效场景 | 归因 | 解法 | 层级 |
|---------|------|------|------|
| 会话开到第 12 轮，第 3 轮定下的结论已滑出窗口 | 窗口太短 | 窗口 4 → **6 轮** + **滚动摘要**（更早的对话压成一段常驻） | L1 / L1.5 |
| "20 轮前提过的那个细节"想不起来 | 窗口是"时间近"不是"语义近" | 历史消息**向量化**，按当前问题召回 top-3 语义相关片段 | L1.6 |
| 同一用户反复问同一类业务，系统每次从零开始 | 没有跨会话画像 | 规则抽取常用流程/系统/主题，作为**检索提示**参与查询扩展 | L2 |
| 知识库答不上来的问题没沉淀 | 缺组织级记忆 | 定稿时顺手采集（零额外成本） | L3 |

**四条设计红线**

1. **关闭即归零**：`AGENT_MEMORY_ENABLED=false` 时短期轮数严格回 4 轮、不注入任何块、
   不召回、不抽取、不采集。有一条用例专门断言"组装出的消息列表与改造前**逐字一致**"。
2. **永不抛异常**：所有入口分层 try/except，任何一层失败都只管自己那一段消失，
   绝不冒泡到问答链路。记忆是加分项，不是失败点。
3. **不阻塞用户**：摘要生成（模型调用）、历史索引（embedding）、偏好抽取全部丢守护线程；
   读侧有硬阈值与超时，读不到就当没有。
4. **L2 只作检索提示，不进 system prompt 全文**：省 token，也避免"用户画像"变成新的幻觉源——
   它只影响**去哪找**，不影响**答案怎么写**。

**三层落点（新增）**

| 层 | 表 / 存储 | 关键实现 |
|----|----------|---------|
| L1 短期 | `chat_messages`（复用） | `get_recent_history(turns=None)` 走记忆配置；关闭时严格 4 轮 |
| L1.5 摘要 | 新表 `conversation_summaries` | 累计 ≥ `12 轮×2` 条才首次触发；之后每新增 `6 轮×2` 条增量刷新；`covered_message_count` 既是覆盖范围也是**增量水位线**；只摘要「最近窗口之前」的部分，与原文不重叠 |
| L1.6 检索式历史 | Qdrant 新 collection `chat_history`（dense-only，与知识条目解耦） | payload 带 `user_id`/`message_id`/`role`/`content`；召回按 `score_threshold` + `top_k` + 自动排除"已在原文窗口内"的消息 |
| L2 用户偏好 | 新表 `user_memory`（JSON） | **纯规则抽取**（jieba + 流程名/系统名匹配，零 token），按 count 排序保留 |
| L3 未答问题 | 新表 `unanswered_questions` | `after_answer(answered=False)` 时写入；**刻意不去重**（保持事件流，改造 5 靠行数取频率）；按用户保留上限自动清理 |

**记忆块（写进 prompt 的唯一出口）**

`memory.memory_block()` 组装成一段，注入**最后一条 HumanMessage 的头部**，而不是 system：

```
【长期记忆·仅供参考】
- 更早的对话摘要：…
- 与当前问题相关的历史对话片段：
  · 你（用户）：…
  · 助手：…
- 你的一贯关注点：常用流程：…；关注系统：…
（以上长期记忆只用于理解上下文与你的用词习惯；回答内容必须以「知识库内容」为准，不得据此编造）
```

放用户消息不放 system，是因为 system 会污染所有行为（含闲聊、写操作约束）；
块内自带"仅供参考、以知识库为准"的约束，避免变成新的幻觉源。

**L2 的用法边界（重要）**

偏好里的**流程名/系统名**参与查询扩展（与现有 `SYNONYMS` 同义词扩展机制合并），
**主题词刻意排除**——"出差""报销"太泛，参与扩展会把检索范围拉散、降低精度。
且扩展词排在问题自身关键词**之后**、**最多补 2 个**（`MAX_PREFERENCE_TERMS`）：
偏好只是锦上添花，绝不能把用户这次真正问的词挤出关键词表。

**遗忘（真实链路冒烟才发现的缺口）**

删掉的对话如果只从 `chat_messages` 删、向量留在 Qdrant 里，下次提问就会把
"已经删掉的内容"当历史片段**召回回来**——既是残留数据也是隐私问题。因此：

- 删单条 / 批量删消息 → 按 `user_id + has_id` 双条件删对应向量点（防越权删他人向量）；
- 清空对话 → 删该用户全部历史向量 + 会话摘要（L1 / L1.5 / L1.6 都是**会话级**记忆）；
- **不动** L2 长期偏好与 L3 缺口台账：前者是用户画像（有独立自助清空接口
  `DELETE /api/memory/me/preference`），后者是组织级台账，都不属于"这一段对话"。

**真实库实测（真实 embedding + 真实 Qdrant + 真实模型 qwen3.7-plus）**

造 16 轮有信息量的对话，把关键细节埋在第 2 轮（离窗口很远），然后问
「报销的提交时限是多久」：

| 项 | 结果 |
|----|------|
| L1.5 摘要 | ✅ 生成成功，799 字（≤ 800 上限），**摘要里就含"报销须在行程结束后 5 个工作日内提交"** |
| L1.6 入库 | ✅ 40/40 条写入 `chat_history`（~70 s，后台线程，不阻塞用户） |
| L1.6 召回 | ✅ 命中 3 条（429 ms），**第 1 条 0.949 正是埋在第 2 轮的"那报销要提前多久提交"** |
| 记忆块 | ✅ 3/3 段齐全 |
| L2 偏好 | ✅ 主题词按热度排序正确 |
| L3 缺口 | ✅ 采集成功 |
| 遗忘 | ✅ 删掉该消息后向量被清理，再召回**不再复现** |

**顺带发现并修掉的一个基础设施缺陷（影响面比记忆更大）**

冒烟时历史索引报 `timed out`、40 条只写入 0 条。排查根因：`QDRANT_URL=http://localhost:6333`
在 Windows 上 `localhost` 优先解析为 IPv6 `::1`，而 Qdrant 只监听 `0.0.0.0:6333`（IPv4），
于是每次请求都要先等 IPv6 连接超时。实测对比：

| URL | 一次 `get_collections()` 耗时 |
|-----|------------------------------|
| `http://localhost:6333` | **2066 ms** |
| `http://127.0.0.1:6333` | **7 ms** |

这意味着**改造前每一次向量检索都在白等约 4 秒**（混合检索要发 2 次请求），
严重时直接超时降级为关键词搜索——这也解释了 M3 里"流程节点类引用质量差"的部分成因。
已把 `backend/.env` 改为 `127.0.0.1`（回滚方式：改回 `localhost`，代价是重新变慢）。

**配置（`backend/.env`）**

```ini
AGENT_MEMORY_ENABLED=true            # 总开关（false = 一键回滚到 4 轮历史）
AGENT_MEMORY_SHORT_TURNS=6           # L1 短期窗口轮数（下限 4，不会比改造前更短）
AGENT_MEMORY_SUMMARY_TRIGGER_TURNS=12  # 满 12 轮才开始生成摘要
AGENT_MEMORY_SUMMARY_INTERVAL_TURNS=6  # 之后每新增 6 轮增量刷新
AGENT_MEMORY_SUMMARY_MAX_CHARS=800     # 摘要正文上限
AGENT_MEMORY_SUMMARY_TIMEOUT=20        # 摘要生成软超时（超时放弃本次，不影响问答）
AGENT_MEMORY_RECALL_ENABLED=true       # L1.6 开关
AGENT_MEMORY_RECALL_TOP_K=3            # 召回条数
AGENT_MEMORY_RECALL_MIN_SCORE=0.55     # 召回最低相似度（低于此不注入）
AGENT_MEMORY_RECALL_MAX_CHARS=300      # 单条召回片段上限
AGENT_MEMORY_PREFERENCE_TERMS=5        # 参与查询扩展的实体词条数上限
AGENT_MEMORY_PREFERENCE_MIN_COUNT=2    # 至少出现几次才算偏好
AGENT_MEMORY_SYSTEM_TERMS=             # 系统名白名单（逗号分隔，留空则只用正则识别）
AGENT_MEMORY_UNANSWERED_KEEP=500       # 每用户未答问题保留条数
```

**新增 API（全部为新增，旧接口不动）**

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| GET | `/api/memory/me` | 登录 | 我的记忆状态（短期轮数/摘要/偏好/未答数） |
| GET | `/api/memory/summary` | 登录 | 我的会话摘要正文 |
| DELETE | `/api/memory/me/preference` | 登录 | 清空我的长期偏好（隐私自助，立即生效） |
| GET | `/api/memory/unanswered` | 登录 | 未答问题（普通用户仅自己；`scope=all` 仅超管） |
| POST | `/api/memory/rebuild` | 管理员 | 重建指定用户的摘要与历史索引（离线校准，换过模型/调过阈值时用） |
| GET | `/api/memory/stats` | 管理员 | 记忆层统计（有偏好的用户数/摘要数/未答数） |

`GET /api/agent/status` 新增 `memory` 子对象，暴露全部记忆配置便于运维确认。

**落库**

三张新表由 `Base.metadata.create_all()` 自动创建（沿用现有机制，无需 ALTER）：

```sql
CREATE TABLE conversation_summaries (
  id VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, summary TEXT,
  covered_message_count INT, model VARCHAR, error TEXT, updated_at DATETIME
);
CREATE TABLE user_memory (
  user_id VARCHAR PRIMARY KEY, data JSON, updated_at DATETIME
);
CREATE TABLE unanswered_questions (
  id VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, question TEXT,
  retrieved_nothing BOOLEAN, source VARCHAR, created_at DATETIME
);
```

**已知边界（诚实说明）**

- 摘要与索引都是**后台异步**：用户当轮提问不会等它们，代价是"刚问过的内容"
  要等下一轮才可能被召回（当前轮的原文本来就在窗口里，不影响）。
- L1.6 索引当前是**逐条 embed**（40 条 ≈ 20~70 s）。因为跑在守护线程里所以不阻塞用户，
  但确实是可优化项（DashScope 支持批量 `embed_documents`，一次调用搞定一批）。
- L2 的流程名识别依赖"流程名像业务实体"（如「校园招聘流程」）。实测真实库里的流程名
  是「筹备/出发前」这类**阶段名**，识别不到——此时 L2 扩展词为空，退化为不影响（安全方向）。
  想让 L2 真正生效，需要把流程名起得更像业务对象。
- `chat_history` 与知识条目集合完全解耦，维度不一致时召回静默返回空（不会污染知识库检索）。

**测试**：新增 `tests/test_12_agent_memory.py`（90 用例，9 组：总开关与口径、
关闭时与改造前逐字一致、摘要触发/增量/失败回退、召回阈值与排除、偏好抽取与
查询扩展上限、未答问题采集与清理、记忆块组装与只读约束、遗忘清理、API 权限）。
**全量 253 用例通过**（原 163 + 新 90）。

---

### M5 · 主动巡检 Agent（从被动到主动）（已完成 2026-10-07）

对应"改造 5"。M0~M4 把系统做成了"**会思考**的被动应答者"：问得好、答得准、
记得住、会自检。但它仍然**只在被问时才开口**——过期知识继续被引用、
反复答不上来的问题烂在日志里、有用率很低的条目没人改、流程描述与条目矛盾没人发现。

M5 把系统翻到主动侧：**定时/手动扫一遍，把问题整理成报告 + 草稿，交人裁决**。

| 检查器 | 规则 | 产出 | 成本 |
|--------|------|------|------|
| ① 过期检查 | `status=published` 且 `updated_at` 距今 > `AGENT_PATROL_STALE_DAYS`（默认 180 天） | 待复核清单（按"最久未更新"排序） | **零模型调用**（纯 DB） |
| ② 缺口聚类 | 消费 M4 的 `unanswered_questions`：逐条向量化 → 余弦 ≥ 0.8 **贪心归簇** → 簇内 ≥ 3 条 | **草稿条目**（模型生成 title/steps 骨架）+ 缺口报告 | 1 次 embedding/条 + 1 次模型/簇 |
| ③ 质量异常 | 某条目被引用 ≥ 5 次，且**有反馈的那部分**有用率 < 50% | 改进建议（"引用得多但用户说没用"） | **零模型调用**（纯 DB） |
| ④ 一致性检查 | 同一流程节点的 `description` 与其关联条目内容矛盾（LLM 判定） | 冲突清单，人工裁决 | 1 次模型/对（**默认关闭**） |

**四条设计红线**

1. **只产出草稿，绝不自动发布**：所有知识产物一律 `status=draft`，复用现有
   「草稿 → 人工复核 → 发布」链路。`create_item` 只在 `published` 时才写向量库，
   所以草稿天然进不了检索——**这是"不引入错误知识"的底线，有专门的用例守护**
   （断言"巡检跑完后已发布条目数一个都没变"）。
2. **分段兜底**：四个检查器各自 `try/except`，某段崩溃只让该段缺席、
   报告状态降级为 `partial`（全挂才 `error`）。离线批处理不该因为一段失败就整份作废。
3. **失败即降级，绝不硬造**：embedding 不可用 → 问题各自成簇 → 达不到最小簇大小
   → **不产出缺口**（安全方向）；模型不可用/输出不可解析 → 只出缺口报告、
   **不建空壳草稿**（没内容的草稿没有复核价值，只会污染知识库列表）。
4. **不碰问答链路**：巡检只在被显式调用（API / 定时任务）时运行，与用户请求完全解耦；
   `AGENT_PATROL_ENABLED=false` 即一键停用（再加一句就是"停用定时任务"）。

**为什么聚类用 leader-based 而不是单链**

单链聚类会把"A 像 B、B 像 C"串成一个大簇（而 A 与 C 其实不像），
缺口报告立刻被灌水；leader-based 只跟**簇代表**比，结果确定、可预期、可解释，
也便于测试（有一条用例专门断言"不允许串联"）。无向量的条目各自成簇，不强行合并。

**为什么有用率只用"有反馈的那部分"算**

`useful_rate = useful ÷ feedback`，分母**只数用户真的点过有用/没用的回答**。
若按总引用数当分母，"用户根本懒得评价"会被误判成"问答质量差"——
这会直接污染质量报告，让管理员去改本来没问题的条目。

**真实库实测（真实库副本 + 真实 embedding + 真实模型 qwen3.7-plus）**

在副本里人为制造三类病灶（把一条已发布条目改成 400 天前更新 / 插 4 条相似未答问题 /
造 5 条引用全是"没用"的对话），再跑一轮 `scope=me` 巡检：

| 项 | 结果 |
|----|------|
| ① 过期检查 | ✅ 命中 1 条（"已发布 400 天未更新（阈值 180 天）"） |
| ② 缺口聚类 | ✅ 扫描 4 条 → 向量化 4 条 → **1 簇（大小 4）** → 生成草稿 1 条 |
| ③ 质量异常 | ✅ 命中 1 条（"被引用 8 次，但 4 条反馈中仅 1 条有用（25.0% < 50%）"） |
| ④ 一致性 | ⏭ 默认关闭（配置开启后需要模型逐对判定） |
| 总览 | ✅ 真实模型生成的克制文案："本次巡检发现 1 处过期内容，建议优先更新该条目…" |
| 耗时 | 23.7 s（几乎全花在 1 次草稿生成 + 1 次总览生成上；规则检查 < 100 ms） |
| **草稿状态** | ✅ 全部 `draft` |
| **未误发布** | ✅ 已发布条目数不变 |
| **草稿未进向量库** | ✅ 直接在 Qdrant 里按 id retrieve 检不到（不可被检索） |
| 报告落库 | ✅ 可按 id 取回四个区块完整内容 |
| 草稿自带来源标注 | ✅ `caution` 里写明"【主动巡检生成·待人工确认】来源：4 条相似未答问题…" |

实测草稿样例（人工复核后才应发布）：

```
标题：报销单提交时限确认与提交
步骤：登录财务报销系统。 / 查阅系统首页公告或财务制度文件，确认当前适用的报销提交时限。
      / 在规定的时限内，填写报销单并上传合规的发票及附件。 / 提交报销单并跟进审批进度。
系统：财务报销系统
注意：超过规定时限提交可能导致报销被拒；超期报销需补充超期说明并走特殊审批流程。
```

**⚠️ 一个真实链路才暴露的口径问题**

模型的草稿**没有编造具体天数**（它写"确认当前适用的报销提交时限"而不是"5 个工作日"），
这正是 prompt 里"只写你确信的通用流程，不确定的细节不要编造"起了作用——
草稿是**骨架**，具体数字要人工填。反过来说：**不要把巡检草稿当成可以直接发布的内容**，
它的价值在于"把缺口摆到人面前 + 给出一个可编辑的起点"。

**配置（`backend/.env`）**

```ini
AGENT_PATROL_ENABLED=true             # 总开关（false = 一键停用巡检入口）
AGENT_PATROL_STALE_DAYS=180           # ① 过期阈值（天）
AGENT_PATROL_MAX_ITEMS=50             # 各区块最多输出多少条
AGENT_PATROL_GAP_MAX_QUESTIONS=200    # ② 每次最多取多少条未答问题聚类
AGENT_PATROL_GAP_SIMILARITY=0.8       # 归簇的余弦阈值
AGENT_PATROL_GAP_MIN_CLUSTER=3        # 簇内条数下限（1~2 次提问更像个案）
AGENT_PATROL_GAP_MAX_CLUSTERS=10      # 最多产出多少簇
AGENT_PATROL_DRAFT_ENABLED=true       # 是否为缺口簇生成草稿（false=只出报告）
AGENT_PATROL_QUALITY_MIN_CITED=5      # ③ 被引用次数下限
AGENT_PATROL_QUALITY_MAX_USEFUL_RATE=0.5   # 有用率上限（低于才算异常）
AGENT_PATROL_QUALITY_MIN_FEEDBACK=2   # 至少几条反馈才判定（防"没人评价"误判）
AGENT_PATROL_QUALITY_LLM=false        # 是否让模型额外指出可能过时的步骤
AGENT_PATROL_CONSISTENCY_ENABLED=false # ④ 一致性检查（成本最高，默认关）
AGENT_PATROL_LLM_TIMEOUT=30           # 巡检内模型调用软超时（秒）
AGENT_PATROL_REPORT_KEEP=200          # 报告保留份数
```

**定时触发（每天 09:00 全站巡检）**

巡检不在问答链路上，最省事的触发方式是 HTTP 定时任务（crash 了也不影响用户）：

```bash
# crontab -e（或者用平台的定时自动化能力）
0 9 * * * curl -s -X POST http://127.0.0.1:8000/api/agent/patrol \
  -H "Authorization: Bearer $SUPER_ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"scope":"all"}' >> /var/log/patrol.log 2>&1
```

**新增 API（全部为新增，旧接口不动）**

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| GET | `/api/agent/patrol/status` | 登录 | 巡检配置 + 我这次会巡检什么范围 |
| POST | `/api/agent/patrol` | 管理员/超管 | 跑一轮巡检（`scope` 留空=自动：超管全站、其他管理员自己的；显式 `all` 仅超管） |
| GET | `/api/agent/patrol/reports` | 管理员/超管 | 报告列表（只含计数与总览） |
| GET | `/api/agent/patrol/reports/{id}` | 管理员/超管 | 单份报告详情（四个区块完整内容 + 生成的草稿 id） |

`GET /api/agent/status` 新增 `patrol` 子对象，暴露全部巡检配置便于运维确认。

**落库**

新表由 `Base.metadata.create_all()` 自动创建（沿用现有机制，无需 ALTER）：

```sql
CREATE TABLE patrol_reports (
  id VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, scope VARCHAR NOT NULL,
  run_at DATETIME, status VARCHAR,           -- ok | partial | error
  stale_items JSON, gap_clusters JSON, quality_issues JSON, consistency_issues JSON,
  summary TEXT, counts JSON, duration_ms INT, error JSON
);
```

`unanswered_questions` 是 M4 就已建好的表（这里只消费，不改结构）——
这也是 M4 里说的"顺手采集、零额外成本"的兑现：**M5 一行采集代码都没写，直接用**。

**已知边界（诚实说明）**

- **缺口聚类依赖 embedding**：没配 Embedding Key 时该区块直接跳过（报告里标注原因），
  其余三个规则检查照常。这是刻意的——**宁可不报，也不要瞎报**。
- **质量异常依赖用户反馈**：反馈少的时候（`AGENT_PATROL_QUALITY_MIN_FEEDBACK`）不判定，
  所以冷启动阶段这一区块基本为空，属正常。
- **一致性检查默认关闭**：它是四个检查器里唯一"每对都要调模型"的，成本最高、
  且矛盾判定本身有主观性；开启前建议先跑一轮看误报率。
- **报告不清理正文之外的东西**：只按份数裁剪，不归档；要留存长期趋势得另做统计表。
- **没有前端页面**：目前只能走 API（与 M2 的 HITL 审批卡片同一状态）。
  前端「巡检报告」页是后续可选项，不影响本里程碑验收。

**测试**：新增 `tests/test_13_agent_patrol.py`（50 用例，9 组：开关与范围口径、
余弦与聚类纯函数、过期检查边界、缺口聚类与草稿产出、质量异常口径与边界、
一致性检查、主流程与报告、API 权限与结构、只读免疫）。
**全量 303 用例通过**（原 253 + 新 50）。

---

## 十二、一句话总结（可用于简历/答辩）

> 把 RAG 应用从「固定检索管线」重构为「带引用校验的三层记忆 + 主动巡检 Agent 系统」：LangGraph 编排 Router→Planner→ToolExecutor→Verifier 循环，10 个工具覆盖检索/流程/入库/待办；两级引用校验把「引用不相关」从体感问题变成可度量指标（引用支撑率 ≥ 90%）；三层记忆（短期窗口 + 滚动摘要 + 检索式历史 + 用户偏好）让 20 轮长对话后仍能召回埋在第 2 轮的细节；主动巡检把系统从"只会被动应答"变成"每天自己找问题"，四个检查器（过期/缺口/质量/一致性）产出**报告 + 草稿**且绝不自动发布；`shadow` 灰度让 Agent 在真实流量上验证后再切换，全链路零用户风险。**全程 303 个离线回归用例守护，每个里程碑都可一键回滚。**

---

*配套文档：`项目设计方案.md`（原始设计）、`功能清单.md`（现有能力）、`开发问题记录.md`（踩坑记录）*
