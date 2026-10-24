# 后端回归测试套件

把每次改动手写的临时验证断言，沉淀为**可重复运行的常驻测试**。改完代码跑一遍，就能知道有没有把已有功能弄坏。

## 怎么跑

在本目录的上一级（`backend/`）执行：

```bash
./venv/Scripts/python.exe tests/run_all.py               # 全量回归
./venv/Scripts/python.exe tests/run_all.py -k visibility # 只跑名字含 visibility 的用例
./venv/Scripts/python.exe tests/run_all.py -x            # 首个失败立即停止
```

首次使用需装测试依赖（已在 `requirements-dev.txt` 声明）：

```bash
./venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

## 三条关键设计

1. **独立测试库**：`conftest.py` 会在导入任何 app 模块**之前**把 `DATABASE_URL` 指向 `test_regression.db`，并在每次运行前重建。测试**永远不会读写真实的 `hr_ai.db`**，也不需要备份数据。
2. **进程内调用**：用 FastAPI `TestClient`，不启动真实服务、不占用 8000 端口；`with TestClient(app)` 会自动触发 startup（建表 / 迁移 / 初始化超管）。
3. **完全离线**：不依赖 Qdrant、Embedding、LLM。检索相关用例会主动让向量检索失败，从而稳定走「关键词降级检索」这条路——也就是说，测试跑的是 Qdrant 掉线时的兜底逻辑。

## 用例分布

| 文件 | 守护内容 |
|------|----------|
| `test_01_auth_roles.py` | 三角色权限矩阵：管理员只能建/管普通用户、超管唯一且不可删不可降级、后台接口角色门槛 |
| `test_02_visibility.py` | 三态可见范围：普通用户内容强制私有、管理员默认全员共享、定向共享仅被选者可见、范围修改立即生效、AI 检索范围一致 |
| `test_03_feedback_loop.py` | 负反馈闭环：待处理列表配对提问与引用来源、一键转用例、去重 409、类型校验 400、普通用户 403 |
| `test_04_regression_bugfix.py` | 历史 Bug 的回归守护：列表权限字段、共享条目只读详情、引用来源定位、节点顺序重排、资料删除、批量删除、任务引用共享流程 |

## 测试账号

由 `conftest.py` 的 `ctx` fixture 自动创建（名字统一带 `regr_` 前缀，只在测试库中）：

- `admin`（内置超管，来自应用初始化）
- `regr_admin`（管理员）、`regr_ua` / `regr_ub` / `regr_uc`（普通用户，用于交叉验证“谁能看到谁的”）

## 新增用例的注意事项

- 用 `helpers.py` 里的封装（`make_item` / `upload_doc` / `expect` 等），失败信息会带出实际响应体；
- **负数断言用 `expect(resp, 403, "说明")`**，正数断言用普通 `assert`，别只断言状态码而不解释业务含义；
- 涉及「已发布」的检索用例请用 `publish_directly(db, item_id)` 直接改库，避免触发向量化（需要 API Key 才能跑）；
- 断言里写清“为什么”，例如 `assert it["id"] not in ids, "乙不应看到甲的私有条目"` —— 失败时一眼能看出是业务规则被破坏。
