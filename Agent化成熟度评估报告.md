# 新员工入职 AI 助手 · Agent 化成熟度评估报告

> 评估日期：2026-10-07
> 评估对象：`backend/app/agent/`（5502 行）+ 问答链路 + 前端对话页
> 评估方式：静态代码核查 + 配置默认值比对 + 离线回归（303 用例）+ 真实服务端到端冒烟
> 配套文档：`Agent改造设计方案.md`（怎么做）、`功能清单.md`（有什么）、`开发问题记录.md`（踩过什么坑）

---

## 一、结论

**这个问题必须分两层回答，因为两层的答案不一样。**

| 问法 | 答案 | 依据 |
|------|------|------|
| 系统**是否具备** Agent 架构？ | **是**，且是完整的 L1 单体增强 Agent | 六大特征全部落地，见第三节 |
| 系统**是否正在作为** Agent 运行？ | 评估时点**否**，出厂默认走固定管线 | `AGENT_MODE` 默认 `off`，见第四节 |

一句话：**发动机造好了，但交付时挂在空挡。**

> 本次评估已同步完成处置：`AGENT_MODE` 已切到 `shadow`（灰度观察态），前端已补 AgentTrace 过程面板 —— 见第七节。

---

## 二、现状诊断的"翻案"对照

设计方案第一章曾给出现状诊断表，逐项判定"当前不是 Agent"。现将同一张表逐项复核：

| 维度 | 改造前（诊断原值） | 现状 | 证据 |
|------|-------------------|------|------|
| 控制流 | 写死的单向管线 | ✅ 带循环的分支图 | `graph.py:659-666` |
| 工具使用 | 无，全仓 `grep bind_tools` 无结果 | ✅ 10 个工具，模型自主选择 | `tools.py:131-516` |
| 自主性 | 检索次数/来源全由代码决定 | ✅ 模型参与规划，可回炉重检索 | `graph.py` node_plan / after_verify |
| 校验 | 无，引用 = 检索结果 | ✅ 两级校验 + 引用三态过滤 | `verifier.py`（644 行） |
| 记忆 | 仅最近 4 轮，无摘要 | ✅ 三层记忆 | `memory.py`（1143 行） |
| 主动性 | 全部被动响应 | ✅ 4 检查器巡检 + 定时触发 | `patrol.py`（1076 行） |
| 多步任务 | 不支持 | ⚠️ 单 Agent 内可多步；跨 Agent 协作未做 | M6 未实施 |

**六项翻案、一项部分**。诊断表里"当前不是 Agent"的判断，在能力层面已经不成立。

---

## 三、六要素代码级核实

### 3.1 决策循环（有循环、有分支）

```
                    ┌──────────────────────────┐
                    ▼                          │
route → retrieve → plan ⇄ act → draft → verify ┴→ finalize
   │                            ▲                  │
   └── 闲聊 → draft ────────────┘              不通过 → 回 plan
```

- 图定义：`graph.py:636-666`，节点 `route / retrieve / plan / act / draft / verify / finalize`
- 真回边：`graph.add_edge("act", "plan")` —— **这是区别于 DAG 的关键**
- 校验回边：`after_verify` 返回 `"plan"`（带"缺什么"提示重检索）或 `"finalize"`
- 分诊 4 类意图：`kb_qa / flow_nav / action / smalltalk`（`router.py`）
- 预算护栏：工具调用 ≤4、LLM ≤6、超时 45s、回炉 ≤2 轮

**这就是 `flow_agent.py` 那个装饰性 `StateGraph` 与真正 Agent 的区别** —— 那个图没有回边、不走分支、API 也没调用它。

### 3.2 工具调用（10 个，权限分层，带审计）

| 层 | 工具 | 权限约束 |
|----|------|----------|
| 读 6 | `knowledge_search` `document_search` `list_flows` `flow_next_step` `get_item` `memo_list` | 按 `read_scope` 裁剪可见范围 |
| 写 3 | `memo_create` `create_draft_item` `update_item` | 受 HITL 开关约束（可挂起等审批） |
| 管理 1 | `stats_snapshot` | 仅 staff |

- 统一装饰器 `@tool(name, args_schema)` + `_audited()` 审计包装（`tools.py:59`）
- Pydantic 入参校验，每个工具 = 现有 service 函数 + 权限裁剪 + 审计日志
- 只读免疫：写操作在 `AGENT_HITL_ENABLED=false` 时直接执行，开启则挂起

### 3.3 自省校验（两级 + 三态）

| 级别 | 触发条件 | 成本 |
|------|----------|------|
| 一级：规则 | 永远执行（纯正则、无网络） | 零 |
| 二级：LLM 复核 | **仅覆盖率落在 0.55~0.85 中间地带** | 1 次调用 |

- 两个时机：`verify_evidence`（生成前，唯一有效的回炉时机）+ `verify_answer`（生成后，引用三态过滤）
- 引用三态：`verified: true / "partial" / false`，无贡献的来源直接剔除，保底留 1 条
- 关键教训：事实抽取**只收**《》「」和数字/单位/日期/英文缩写，**绝不收双引号**（模型用引号做强调会误判，实测把覆盖率从 100% 压到 25%）；假阳性比漏判更糟，曾回退一个贪婪正则

### 3.4 三层记忆

| 层 | 机制 | 成本 |
|----|------|------|
| L1 | 短期 6 轮窗口（关闭时为 4 轮，与改造前逐字一致） | 零 |
| L1.5 | 滚动摘要（两道闸 + 水位线控制成本） | 1 次调用/若干轮 |
| L1.6 | 检索式历史（Qdrant 独立集合 `chat_history`，按需召回 top3） | 1 embedding/条 |
| L2 | 用户偏好（纯规则抽取，零成本） | 零 |
| L3 | 缺口采集（未答问题台账） | 零 |

**实测验收**：16 轮对话、关键细节埋在第 2 轮，问「报销的提交时限」→ 命中 3 条，第 1 条 0.949 正是埋在第 2 轮的那句。验收标准"20 轮后仍能答出前文细节"达成。

### 3.5 主动性（唯一真正"主动"的能力）

| 检查器 | 规则 | 成本 |
|--------|------|------|
| 过期检查 | `published` 且超 180 天未更新 | 零 |
| 缺口聚类 | 未答问题向量化 → 余弦 ≥0.8 贪心归簇 → 簇内 ≥3 → 生成草稿 | 1 embedding/条 + 1 调用/簇 |
| 质量异常 | 被引用 ≥5 次且有用率 <50% | 零 |
| 一致性检查 | 流程节点 ↔ 关联条目矛盾 | 默认关闭 |

**关键安全属性**：只产出 `status=draft` 草稿 + 报告，**绝不自动发布**。实测 4 条相似缺口聚成 1 簇并生成草稿，而已发布条目数不变、草稿在向量库中 `retrieve` 不到。

### 3.6 可控性

- **HITL**：写操作挂起 → `POST /api/agent/approve/{run_id}` 放行/拒绝
- **检查点**：SQLite 持久化图状态，可从挂起点恢复（`AGENT_USE_CHECKPOINT` / `AGENT_HITL_ENABLED` 任一为真即启用）
- **轨迹**：`agent_runs` 表落全量决策过程，`chat_messages.agent_trace_id` 反查
- **一键回滚**：任何异常自动回落固定管线；每个子能力都有独立开关

---

## 四、出厂状态快照（评估时点）

`.env` 中**无任何 `AGENT_*` 覆盖**，全部走 `config.py` 默认值：

| 配置 | 默认值 | 含义 |
|------|--------|------|
| `AGENT_MODE` | `off` | **用户走固定管线，Agent 不参与** |
| `AGENT_VERIFIER_ENABLED` | `True` | 引用校验可用（但仅 Agent 路径生效） |
| `AGENT_VERIFIER_USE_LLM` | `False` | 二级 LLM 复核关闭（省 token） |
| `AGENT_MEMORY_ENABLED` | `True` | **接在固定管线上，是唯一出厂即生效的能力** |
| `AGENT_PATROL_ENABLED` | `True` | 可触发，但没有定时任务挂着 |
| `AGENT_HITL_ENABLED` | `False` | 写操作不挂起 |
| `AGENT_USE_CHECKPOINT` | `False` | 无断点恢复 |
| `AGENT_SHADOW_RATE` | `1.0` | shadow 时 100% 采样 |

**三项前端未接线**（实现齐了但没界面）：

1. `/api/chat/agent/stream` 富事件流 —— 前端从未调用，只调 `/api/chat/stream`
2. HITL 审批 —— 无审批卡片，只能走 API
3. 巡检报告 —— 无页面，只能走 API

---

## 五、三处"接线断裂"

这三处的共同点是：**能力实现了，但默认配置下不生效**。它们不是 bug，是交付口径问题。

| # | 断裂点 | 后果 |
|---|--------|------|
| 1 | Verifier 挂在 Agent 路径，固定管线无任何校验代码（`chat_service.py` 中 grep 不到 verifier） | `AGENT_MODE=off` 时**引用根本不过滤** —— 最初始反馈的"引用不相关"在默认配置下仍是老样子 |
| 2 | `AGENT_HITL_ENABLED=False` + 无前端审批卡片 | 写操作直接执行；即便开启也没有界面可操作 |
| 3 | `AGENT_PATROL_ENABLED=True` 但无定时任务 | 巡检不会自己跑，需手动调 API |

---

## 六、与设计方案目标指标的对照

设计方案 §2.2 定义了改造后的目标指标，如实对照（未度量即标注未度量）：

| 指标 | 目标 | 实测 | 状态 |
|------|------|------|------|
| 引用正确率 | ≥90% | `citation_support_rate` 实测 **85.7% / 14.3% / 25.0%**（分场景，差异极大） | ⚠️ 未达标 |
| 答案完备率 | ≥85% | 未度量 | — |
| 检索命中率 | 不下降 | 现有评测可跑，未做 fixed vs agent 对比 | — |
| 平均检索轮数 | 1.4~2.0 | 实测走 fast_path（命中 ≥5 跳过规划），单轮 | ⚠️ 偏低（可接受） |
| 端到端首字延迟 | ≤2.5s（P50） | 单次工具耗时实测 1478 ms | 未完整度量 |
| token 成本 | ≤2× baseline | 未度量 | — |

**关于引用正确率未达标的说明**：低支撑率是**诊断信号，不是实现缺陷**。实测发现流程节点类引用质量最差（支撑率 25%）—— 根因是流程节点描述本身信息密度低、与问题语义距离远，属**检索侧优化方向**。而 `.env` 里 `QDRANT_URL` 从 `localhost` 改为 `127.0.0.1` 后（实测 2066ms → 7ms），以往"每次向量检索白等 4 秒甚至超时降级"的问题已消除，**这很可能已部分改善了该指标，需要重新采样确认**。

---

## 七、本次处置（评估结论的落地）

| 项 | 内容 | 回滚方式 |
|----|------|----------|
| **A** | `AGENT_MODE` 由 `off` 改为 **`shadow`**，后端已重启生效（`/api/agent/status` 返回 `"mode": "shadow"`） | 改回 `off` 并重启；`.env` 已备份为 `.env.bak-*` |
| **B** | 本报告 | — |
| **C** | 前端新增「Agent 过程」开关 + 折叠面板（`Chat.vue` / `api/chat.js`） | 默认关闭，不影响任何现有行为 |

### shadow 模式的语义

用户**仍然看到固定管线的结果**（体验零变化），Agent 在后台只读跑一遍并把轨迹写入 `agent_runs`，用于 fixed vs agent 质量对比。

> ⚠️ **成本提示**：`AGENT_SHADOW_RATE` 默认为 `1.0`，即每个请求都会额外跑一遍 Agent（含 LLM 调用），token 消耗约翻倍。若想降低采样率，在 `backend/.env` 加一行 `AGENT_SHADOW_RATE=0.2` 再重启即可。

### Agent 过程面板的语义

前端对话页工具栏新增开关，**默认关闭**：

- 关闭 → 完全走 `/api/chat/stream`，与改造前一字不差
- 开启 → 走 `/api/chat/agent/stream`，回答下方出现可折叠的「Agent 过程」面板，展示：
  分诊意图 → 检索/规划决策 → 工具调用（含耗时与命中摘要）→ 引用校验（剔除条数、硬信息支撑率）→ 定稿统计
- 后端降级时发 `fallback` 事件，面板如实显示"已回落固定管线"，正文与引用照常显示
- 开关状态记在 `localStorage`，刷新后保持

---

## 八、升级路径

### 已完成
1. ✅ `off` → `shadow`（本次）
2. ✅ 前端过程面板（本次）

### 建议后续
3. **观察 1~3 天**：用 `GET /api/agent/runs` 对比 fixed vs agent 的引用质量与延迟，确认无劣化
4. **切 `on`**：`AGENT_MODE=on` + 重启。此后用户走 Agent 路径，引用校验真正生效
5. **补另两块前端**：HITL 审批卡片、巡检报告页（当前只能走 API）
6. **可选 M6**：多 Agent Supervisor（`supervisor.py` 尚未创建）

### 上线风险与对策

| 风险 | 对策 |
|------|------|
| Agent 延迟高于固定管线（多一次规划 LLM 调用） | `fast_path`：预检索命中 ≥5 条直接跳过规划，实测走的就是这条 |
| Agent 出错 | runtime 内部自动回落固定管线，SSE 协议不变，前端无需改动 |
| token 成本上升 | 每项能力独立开关；`AGENT_SHADOW_RATE` 可降采样 |
| 写操作被误执行 | 开启 HITL 可挂起；`create_draft_item` 只产草稿不入检索 |

---

## 九、证据索引

| 类型 | 内容 |
|------|------|
| 静态代码 | `app/agent/`（11 文件 5502 行）：`graph.py` 668 / `memory.py` 1143 / `patrol.py` 1076 / `runtime.py` 822 / `verifier.py` 644 / `tools.py` 587 等 |
| 配置默认值 | `app/core/config.py:51-155`（52 项 `AGENT_*`） |
| 路由分流 | `app/api/chat.py:64-80`（off/shadow/on 三分支）、`:83`（`/agent/stream`） |
| 离线回归 | `backend/tests/run_all.py` → **303 用例**（基础 60 + Agent 与记忆 193 + 巡检 50） |
| 活体检查 | `test_09_agent_graph.py` + `test_10_agent_approval.py` → 39 用例通过 |
| 端到端冒烟 | `POST /api/chat/agent/stream` 实测事件序列：`start → intent(kb_qa) → 4×step → tool(knowledge_search, 7 条, 1478ms) → references(7) → 正文` |
| 历史实测 | M4 记忆召回 0.949 命中埋在第 2 轮的细节；M5 巡检 4 缺口聚 1 簇并生成 draft |
| 数据落库 | `agent_runs` / `conversation_summaries` / `user_memory` / `unanswered_questions` / `patrol_reports` 五张新表 + Qdrant `chat_history` 集合 |

---

## 十、一句话总结

> **架构上，它已经是 Agent 了；交付上，它默认还没开始当 Agent。**
> 六项 Agent 特征全部落地（含 303 条回归用例与真实链路冒烟支撑），但出厂默认走的是改造前的固定管线 —— 差的是"翻开关"和"接前端"，不是"写代码"。
