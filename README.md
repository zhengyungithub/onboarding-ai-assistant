# 新员工入职 AI 业务问答助手

面向新员工入职场景的企业内部知识问答系统：管理员将制度文档、业务流程沉淀为结构化知识库，员工通过 AI 对话获得**带引用来源、可溯源**的回答。配套流程引擎、待办备忘录、评测系统和三级权限体系，形成「知识入库 → 智能问答 → 反馈 → 评测优化」的完整闭环。

> 详细文档：[功能清单](./功能清单.md) | [用户使用手册](./用户使用手册.md) | [部署指南](./部署指南.md)

## 核心特性

- **AI 问答（RAG）**：Qdrant 混合检索（dense 语义 + sparse 关键词 + RRF 融合）；两阶段检索（先知识库、后资料文档，IDF 加权过滤）；流程节点自动补全与近重复去重；多轮追问自动带上下文（启发式识别 + LLM 问题改写，带降级）
- **引用可溯源**：回答附带命中来源，按类型（知识条目/文档/流程节点）跳转到对应管理页并滚动定位高亮
- **知识生产 SOP**：上传 Word/Excel 文档或粘贴文本 → AI 结构化抽取 → 人工校对 → 草稿评审 → 发布，非技术同事可自助维护
- **细粒度权限**：三级角色（普通用户/管理员/超管）× 三态可见范围（仅自己/全员共享/定向共享），权限贯穿 REST API、数据库查询与向量库 payload 过滤三层
- **质量闭环**：用户「没用」反馈可填原因并一键转为评测测试用例；一键批量评测 + 统计看板，知识更新后可回归验证
- **流程引擎**：业务流程配置 + 节点管理 + 「流程下一步」智能提醒（LangGraph 状态机 + LLM 生成操作要点）
- **备忘录**：个人待办管理，支持文件夹分组、优先级、截止时间、统计卡片与分页
- **工程化**：55 个用例的 pytest 回归测试套件（隔离测试库）、幂等数据库迁移、SSE 流式输出、一键启停脚本

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI + SQLAlchemy 2.x + SQLite（`backend/hr_ai.db`） |
| 检索 | Qdrant（本机原生二进制，dense+sparse 混合检索）+ jieba 关键词降级 |
| 大模型 | LangChain 0.3+ / LangGraph 1.2+，兼容 OpenAI 接口的大模型与 Embedding API（如 DashScope） |
| 前端 | Vue 3 + Vite + Element Plus + Pinia |
| 测试 | pytest（隔离测试库，55 用例） |

## 快速开始

双击 **`启动.bat`**（或 `start.ps1`）一键启动全部服务：

1. **Qdrant 向量库**（6333 端口）：自动检测并拉起 `qdrant-native\qdrant.exe`，数据落 `data\qdrant`
2. **后端**（8000 端口）：uvicorn 启动，首次启动自动建表/迁移并回灌向量
3. **前端**（5173 端口）：Vite 开发服务器

启动完成后访问 http://localhost:5173 ，默认超管账号 `admin`（密码见部署指南 / 首次登录后修改）。

停止：双击 **`停止.bat`**（或 `stop.ps1`），一并停止 Qdrant、后端、前端。

> 依赖：Node.js、Python 3.13（后端虚拟环境位于 `backend/venv`）。首次部署与手工步骤见 [部署指南](./部署指南.md)。

## 项目结构

```
├── 启动.bat / 停止.bat      # 一键启停（Qdrant + 后端 + 前端）
├── backend/                 # FastAPI 后端
│   ├── app/api/             # 路由（auth/items/chat/flows/evaluation/memos...）
│   ├── app/services/        # 业务逻辑（chat_service 检索编排、qdrant_service...）
│   ├── app/models/          # SQLAlchemy 模型
│   ├── tests/               # pytest 回归测试（55 用例，隔离库）
│   └── venv/                # Python 虚拟环境
├── frontend/                # Vue 3 前端（views/api/stores/router）
├── qdrant-native/           # Qdrant 原生二进制（免 Docker）
├── data/qdrant/             # Qdrant 数据持久化
├── docker-compose.yml       # 备用：Docker 方式部署基础设施
└── 功能清单.md / 用户使用手册.md / 部署指南.md
```

## 开发

```bash
# 后端（手动启动）
cd backend && venv\Scripts\activate
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 前端（手动启动）
cd frontend && npm run dev

# 回归测试（不依赖外部服务，约 2 分半）
cd backend && venv\Scripts\python.exe tests/run_all.py
```
