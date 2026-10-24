# 新员工入职 AI 业务问答助手 — 项目交接文档

> 文档版本：v1.0 | 更新日期：2026-10-05

---

## 一、项目概述

### 1.1 项目目标

构建一个基于 RAG（检索增强生成）的 AI 业务问答系统，帮助新员工快速查询入职相关的业务知识、流程规范和操作指引。系统支持多租户数据隔离，每个用户拥有独立的知识库、流程、任务和对话记录。

### 1.2 核心能力

- **AI 问答**：基于知识库的智能问答，支持混合检索（向量 + BM25 关键词）和流式输出
- **知识库管理**：结构化知识条目（标题、步骤、注意事项、关联流程）
- **资料入库**：上传 Word/Excel/PDF 文档，自动解析并向量化
- **流程配置与任务**：配置业务流程模板，创建任务实例跟踪节点完成状态
- **备忘录**：个人待办事项管理
- **评测数据**：测试用例管理 + 一键评测检索命中率
- **多租户**：所有数据按 `user_id` 隔离，管理员可管理用户

---

## 二、技术栈

### 后端

| 类别 | 技术 | 版本/说明 |
|------|------|-----------|
| 语言 | Python | 3.13 |
| Web 框架 | FastAPI | RESTful API |
| ORM | SQLAlchemy | 数据库操作 |
| 数据库 | SQLite（开发）/ PostgreSQL（生产） | 数据持久化 |
| 向量数据库 | Qdrant | 混合向量检索（可降级为数据库关键词搜索） |
| LLM 框架 | LangChain | 对话链管理 |
| LLM 接入 | OpenAI 兼容 API | 阿里云通义千问（用户可自定义） |
| 文档解析 | Docling | Word/Excel/PDF 解析 |
| 认证 | JWT + bcrypt | 密码哈希 + Token 鉴权 |
| API Key 加密 | Fernet | 用户 LLM API Key 加密存储 |
| 邮件 | SMTP | 忘记密码验证码发送 |

### 前端

| 类别 | 技术 |
|------|------|
| 框架 | Vue 3 + Composition API |
| UI 组件库 | Element Plus |
| 状态管理 | Pinia |
| 路由 | Vue Router |
| HTTP | Axios |
| Markdown 渲染 | markdown-it + highlight.js |
| 构建工具 | Vite |

---

## 三、项目结构

```
d:\新员工入职ai业务问答助手\
├── backend\
│   ├── app\
│   │   ├── api\              # API 路由层
│   │   │   ├── auth.py           # 认证 + 用户管理 + 忘记密码
│   │   │   ├── chat.py           # AI 问答（同步 + 流式）
│   │   │   ├── chat_messages.py  # 对话消息 CRUD + 反馈
│   │   │   ├── items.py          # 知识条目 CRUD
│   │   │   ├── ingest.py         # 文档入库
│   │   │   ├── documents.py      # 文档管理
│   │   │   ├── flows.py          # 流程配置 CRUD
│   │   │   ├── tasks.py          # 任务实例 CRUD
│   │   │   ├── memos.py          # 备忘录 CRUD
│   │   │   ├── evaluation.py     # 评测用例 + 统计
│   │   │   └── source_documents.py
│   │   ├── services\         # 业务逻辑层
│   │   │   ├── chat_service.py       # RAG 检索 + LLM 生成
│   │   │   ├── knowledge_service.py  # 知识库管理
│   │   │   ├── chat_message_service.py
│   │   │   ├── memo_service.py
│   │   │   ├── task_service.py
│   │   │   ├── flow_service.py
│   │   │   ├── evaluation_service.py
│   │   │   ├── ingest_service.py
│   │   │   ├── llm_service.py        # LLM 客户端（按用户 API Key）
│   │   │   ├── embedding_service.py  # 向量嵌入
│   │   │   ├── qdrant_service.py     # 向量检索
│   │   │   └── email_service.py      # 邮件发送
│   │   ├── models\             # SQLAlchemy 数据模型
│   │   │   ├── user.py
│   │   │   ├── knowledge.py
│   │   │   ├── source_document.py
│   │   │   ├── flow.py
│   │   │   ├── task.py
│   │   │   ├── memo.py
│   │   │   ├── chat_message.py
│   │   │   └── evaluation.py
│   │   ├── schemas\            # Pydantic 请求/响应模型
│   │   ├── core\               # 配置、安全、依赖
│   │   └── db\                 # 数据库连接
│   ├── .env                    # 环境变量（不入库）
│   ├── requirements.txt
│   └── venv\                   # Python 虚拟环境
├── frontend\
│   ├── src\
│   │   ├── views\              # 页面组件
│   │   │   ├── Chat.vue            # AI 问答
│   │   │   ├── Flow.vue            # 流程下一步
│   │   │   ├── Tasks.vue           # 我的任务
│   │   │   ├── Memos.vue           # 备忘录
│   │   │   ├── Settings.vue        # API Key 配置
│   │   │   ├── Login.vue
│   │   │   ├── ForgotPassword.vue
│   │   │   └── admin\
│   │   │       ├── Items.vue       # 知识库管理
│   │   │       ├── Ingest.vue      # 资料入库
│   │   │       ├── Flows.vue       # 流程配置
│   │   │       ├── Evaluate.vue    # 评测数据
│   │   │       └── Users.vue       # 用户管理
│   │   ├── api\                # API 封装
│   │   ├── stores\             # Pinia 状态管理
│   │   ├── router\             # 路由配置
│   │   └── layouts\            # 布局组件
│   └── package.json
├── 用户使用手册.md
├── 用户使用手册.docx
├── start.bat                   # 一键启动脚本
└── stop.bat                    # 一键停止脚本
```

---

## 四、数据模型

### 4.1 用户（User）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | String (UUID) | 主键 |
| username | String | 用户名（唯一） |
| email | String | 邮箱（忘记密码用） |
| password_hash | String | bcrypt 哈希密码 |
| role | String | `admin` / `user` |
| llm_api_key | String | Fernet 加密的 LLM API Key |
| llm_base_url | String | LLM 接口地址 |
| llm_model | String | 模型名称 |
| embedding_api_key | String | Embedding API Key |
| embedding_model | String | Embedding 模型 |

### 4.2 知识条目（KnowledgeItem）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | String | 主键 |
| user_id | String | 多租户隔离 |
| title | String | 标题 |
| steps | JSON | 操作步骤数组 |
| system_or_material | String | 涉及的系统或物料 |
| caution | Text | 注意事项 |
| related_flow | JSON | 关联流程 ID 数组 |
| status | String | `draft` / `published` |

### 4.3 源文档（SourceDocument）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | String | 主键 |
| user_id | String | 所属用户 |
| filename | String | 文件名 |
| content | Text | 解析后的文本内容 |
| file_path | String | 存储路径 |
| status | String | 解析状态 |

### 4.4 流程（Flow）+ 流程节点（FlowNode）

- Flow：id, user_id, name, description
- FlowNode：id, flow_id, name, description, order, related_item_ids

### 4.5 任务实例（TaskInstance）+ 节点进度（TaskNodeProgress）

- TaskInstance：id, user_id, flow_id, name, status, current_node_id
- TaskNodeProgress：id, task_id, node_id, status, completed_at

### 4.6 备忘录（Memo）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | String | 主键 |
| user_id | String | 所属用户 |
| title | String | 标题 |
| content | Text | 内容 |
| is_completed | Boolean | 是否完成 |
| priority | String | `low` / `medium` / `high` |
| due_date | DateTime | 截止日期 |

### 4.7 对话消息（ChatMessage）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | String | 主键 |
| user_id | String | 所属用户 |
| role | String | `user` / `assistant` |
| content | Text | 消息内容 |
| references | JSON | 引用来源 |
| feedback | String | `useful` / `useless` / null |
| created_at | DateTime | 创建时间 |

### 4.8 评测（TestCase / EvaluationResult）

- TestCase：id, user_id, question, expected_item_id, expected_item_title, enabled
- EvaluationResult：id, user_id, total_cases, hit_count, hit_rate, details

---

## 五、API 接口清单

### 认证与用户管理（`/api/auth`）

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| POST | `/login` | 登录 | 公开 |
| GET | `/me` | 当前用户信息 | 已登录 |
| GET | `/users` | 用户列表 | 管理员 |
| POST | `/users` | 创建用户 | 管理员 |
| PUT | `/users/{id}` | 修改用户（用户名/密码/邮箱/角色） | 管理员 |
| DELETE | `/users/{id}` | 删除用户 | 管理员 |
| GET | `/api-keys` | 获取 API Key 配置 | 已登录 |
| PUT | `/api-keys` | 更新 API Key 配置 | 已登录 |
| POST | `/forgot-password` | 发送重置密码验证码 | 公开 |
| POST | `/reset-password` | 重置密码 | 公开 |

### AI 问答（`/api/chat`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `` | 同步问答 |
| POST | `/stream` | 流式问答（SSE） |

### 对话消息（`/api/chat/messages`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `` | 获取对话历史 |
| DELETE | `` | 批量删除消息（body: `{ids: [...]}`） |
| DELETE | `/all` | 清空所有对话 |
| PATCH | `/{id}/feedback` | 设置反馈（body: `{feedback: "useful"|"useless"}`） |

### 知识条目（`/api/items`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `` | 列表（支持 status/keyword 筛选） |
| POST | `` | 创建 |
| GET | `/{id}` | 详情 |
| PUT | `/{id}` | 更新 |
| DELETE | `/{id}` | 删除 |
| POST | `/{id}/publish` | 发布 |

### 资料入库与文档（`/api/ingest`, `/api/documents`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/ingest/upload` | 上传文档并解析入库 |
| GET | `/documents` | 文档列表 |
| GET | `/documents/{id}` | 文档详情 |
| DELETE | `/documents/{id}` | 删除文档 |

### 流程配置（`/api/flows`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `` | 流程列表 |
| POST | `` | 创建流程（含节点） |
| GET | `/{id}` | 流程详情 |
| PUT | `/{id}` | 更新流程 |
| DELETE | `/{id}` | 删除流程 |

### 任务（`/api/tasks`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `` | 任务列表 |
| POST | `` | 创建任务 |
| GET | `/{id}` | 任务详情（含节点进度） |
| DELETE | `/{id}` | 删除任务 |
| POST | `/{id}/nodes/{nodeId}/complete` | 标记节点完成 |
| POST | `/{id}/nodes/{nodeId}/incomplete` | 标记节点未完成 |

### 备忘录（`/api/memos`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `` | 列表（支持 is_completed/priority/keyword 筛选） |
| POST | `` | 创建 |
| PUT | `/{id}` | 更新 |
| PATCH | `/{id}/toggle` | 切换完成状态 |
| DELETE | `/{id}` | 删除 |

### 评测（`/api/evaluation`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/stats` | 统计数据（条目数、用例数、命中率、反馈数） |
| GET | `/cases` | 测试用例列表 |
| POST | `/cases` | 创建测试用例 |
| DELETE | `/cases/{id}` | 删除测试用例 |
| POST | `/run` | 运行评测 |

---

## 六、配置说明

### 6.1 环境变量（`backend/.env`）

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `DATABASE_URL` | 数据库连接 | `sqlite:///./app.db` |
| `QDRANT_URL` | Qdrant 地址 | `http://localhost:6333` |
| `JWT_SECRET_KEY` | JWT 签名密钥 | （随机生成） |
| `ADMIN_USERNAME` | 管理员用户名 | `admin` |
| `ADMIN_PASSWORD` | 管理员密码 | 部署时在 .env 中设置（不写入任何文档） |
| `ADMIN_EMAIL` | 管理员邮箱 | `admin@example.com` |
| `API_KEY_ENCRYPTION_KEY` | Fernet 加密密钥 | （随机生成） |
| `DEFAULT_LLM_BASE_URL` | 默认 LLM 接口 | 通义千问 |
| `DEFAULT_LLM_MODEL` | 默认模型 | `qwen-plus` |
| `SMTP_HOST` | SMTP 服务器 | `smtp.qq.com` |
| `SMTP_PORT` | SMTP 端口 | `465` |
| `SMTP_USER` | 发件邮箱 | — |
| `SMTP_PASSWORD` | 邮箱授权码 | — |

### 6.2 关键设计决策

1. **多租户隔离**：所有业务表都有 `user_id` 字段，查询时强制过滤
2. **向量检索降级**：Qdrant 不可用时自动降级为数据库关键词搜索（jieba 分词 + LIKE）
3. **LLM API Key 隔离**：每个用户配置自己的 LLM API Key，Fernet 加密存储
4. **对话持久化**：对话记录存入数据库（非 localStorage），支持跨浏览器同步
5. **评测反馈统一**：对话页面的「有用/没用」反馈保存在 `ChatMessage.feedback`，评测统计直接读取该字段

---

## 七、部署与启动

### 7.1 一键启动（推荐）

```bash
# 启动前后端
start.bat

# 停止
stop.bat
```

启动后访问：
- 前端：http://localhost:5173
- 后端 API 文档：http://localhost:8000/docs
- 默认管理员：admin（密码部署时在 .env 的 ADMIN_PASSWORD 中设置）

### 7.2 手动启动

```bash
# 后端
cd backend
venv\Scripts\activate
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 前端
cd frontend
npm run dev
```

### 7.3 首次启动自动初始化

- 自动创建数据库表
- 自动创建管理员账号
- 自动创建 Qdrant 集合（Qdrant 未启动时跳过，不影响其他功能）

---

## 八、开发过程中的关键问题与修复

### 8.1 对话记录多租户隔离

**问题**：localStorage 中的迁移逻辑导致租户看到管理员的对话。

**修复**：移除迁移逻辑，引入存储版本号 `v2`，使旧的污染数据失效。

### 8.2 对话记录持久化

**问题**：对话记录存在 localStorage，换浏览器看不到。

**修复**：新建 `ChatMessage` 表，问答完成后自动保存到数据库，前端从后端加载历史。

### 8.3 评测检索无结果

**问题**：`run_evaluation` 调用 `search_relevant_items` 时漏传 `db` 参数，Qdrant 失败后降级搜索因 `db is None` 返回空。

**修复**：传入 `db=db` 参数。

### 8.4 评测反馈数据不显示

**问题**：评测统计读 `ChatFeedback` 表，但反馈实际存在 `ChatMessage.feedback`。

**修复**：统计逻辑改为从 `ChatMessage` 表读取。

### 8.5 用户列表不显示

**问题**：后端返回数组，前端拦截器已提取 `response.data`，但代码又取了 `res.data`。

**修复**：`users.value = res`。

### 8.6 知识库/流程配置租户不可见

**问题**：这些功能放在「后台管理」下并限制管理员。

**修复**：移到主菜单，所有用户可访问（数据按 user_id 隔离）。

---

## 九、已知问题与注意事项

1. **Qdrant 未启动**：向量检索不可用，自动降级为关键词搜索。需安装启动 Qdrant 才能使用向量检索。
2. **SMTP 未配置**：忘记密码功能无法发送验证码。需在 `.env` 中配置 SMTP。
3. **LLM API Key 未配置**：AI 问答无法调用大模型。需在「API Key 配置」页面填写。
4. **Docling 首次加载慢**：文档解析首次需下载模型，后续正常。
5. **开发环境用 SQLite**：生产环境建议切换 PostgreSQL，修改 `DATABASE_URL` 即可。

---

## 十、后续优化建议

1. **对话分页**：当前最多加载 200 条消息，长期使用需加分页或无限滚动
2. **知识库批量操作**：支持批量导入/导出知识条目
3. **对话导出**：支持导出对话记录为 Markdown/PDF
4. **流程节点依赖**：支持节点间的条件分支和依赖关系
5. **评测报告**：生成评测报告并支持下载
6. **操作日志**：记录关键操作（创建/删除/修改）便于审计
7. **性能优化**：知识库条目较多时，考虑全量重建向量索引

---

## 十一、文档清单

| 文档 | 位置 | 说明 |
|------|------|------|
| 用户使用手册.md | 项目根目录 | 完整功能使用说明 |
| 用户使用手册.docx | 项目根目录 | Word 版使用手册 |
| HANDOFF.md | 项目根目录 | 本文档 |
