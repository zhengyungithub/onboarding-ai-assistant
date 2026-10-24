"""FastAPI 应用入口"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.db.session import engine, Base
from app.models import User
from app.core.config import settings
from app.core.security import hash_password
from app.api.auth import router as auth_router
from app.api.items import router as items_router
from app.api.chat import router as chat_router
from app.api.documents import router as documents_router
from app.api.ingest import router as ingest_router
from app.api.flows import router as flows_router
from app.api.evaluation import router as evaluation_router
from app.api.source_documents import router as source_documents_router
from app.api.tasks import router as tasks_router
from app.api.memos import router as memos_router
from app.api.memo_folders import router as memo_folders_router
from app.api.chat_messages import router as chat_messages_router
from app.api.agent import router as agent_router
from app.api.memory import router as memory_router
from app.api.patrol import router as patrol_router
from app.api.confirmations import (
    doc_router as doc_confirm_router,
    router as confirmations_router,
    public_router as public_confirm_router,
)
from app.services.flow_service import FlowNodeValidationError
from app.services.task_service import WorkflowBlockedError


def init_db():
    """创建所有表"""
    Base.metadata.create_all(bind=engine)


def init_migrate():
    """轻量迁移（幂等）：为三级角色 + 三态可见范围模型补齐字段与数据

    1. 三张内容表补充 visibility / shared_user_ids 字段（老库无需重建表）；
    2. 按旧 is_shared 回填 visibility（is_shared=1 → all），is_shared 改为派生值；
    3. test_cases 补充 source_message_id 字段（负反馈转用例去重）；
    4. 内置账号角色升级为 super_admin（超管唯一）。
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.orm import Session as OrmSession
    from app.core.roles import ROLE_SUPER_ADMIN

    insp = inspect(engine)
    dialect = engine.dialect.name
    default_false = "DEFAULT 0" if dialect == "sqlite" else "DEFAULT FALSE"

    with engine.begin() as conn:
        for table in ("knowledge_items", "flows", "source_documents"):
            if not insp.has_table(table):
                continue
            cols = [c["name"] for c in insp.get_columns(table)]
            if "is_shared" not in cols:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN is_shared BOOLEAN NOT NULL {default_false}"
                ))
                print(f"[迁移] {table} 已增加 is_shared 字段")
            if "visibility" not in cols:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN visibility VARCHAR NOT NULL DEFAULT 'private'"
                ))
                print(f"[迁移] {table} 已增加 visibility 字段")
            if "shared_user_ids" not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN shared_user_ids JSON"))
                print(f"[迁移] {table} 已增加 shared_user_ids 字段")
            # 旧数据回填：原「共享」→ 全员共享；is_shared 改为派生值（visibility != private）
            conn.execute(text(
                f"UPDATE {table} SET visibility = 'all' "
                f"WHERE is_shared = 1 AND (visibility IS NULL OR visibility = 'private')"
            ))
            conn.execute(text(
                f"UPDATE {table} SET is_shared = "
                f"CASE WHEN visibility IS NOT NULL AND visibility != 'private' THEN 1 ELSE 0 END"
            ))

        if insp.has_table("test_cases"):
            cols = [c["name"] for c in insp.get_columns("test_cases")]
            if "source_message_id" not in cols:
                conn.execute(text("ALTER TABLE test_cases ADD COLUMN source_message_id VARCHAR"))
                print("[迁移] test_cases 已增加 source_message_id 字段")
            # 多期望条目（全部命中才算通过）；老数据留空，service 层按单条目兜底
            if "expected_item_ids" not in cols:
                conn.execute(text("ALTER TABLE test_cases ADD COLUMN expected_item_ids JSON"))
                print("[迁移] test_cases 已增加 expected_item_ids 字段")
            if "expected_item_titles" not in cols:
                conn.execute(text("ALTER TABLE test_cases ADD COLUMN expected_item_titles JSON"))
                print("[迁移] test_cases 已增加 expected_item_titles 字段")

        if insp.has_table("chat_messages"):
            cols = [c["name"] for c in insp.get_columns("chat_messages")]
            if "feedback_reason" not in cols:
                conn.execute(text("ALTER TABLE chat_messages ADD COLUMN feedback_reason TEXT"))
                print("[迁移] chat_messages 已增加 feedback_reason 字段")
            # 引用校验（改造 3）：覆盖率 + 关联的 Agent 轨迹 id
            if "citation_coverage" not in cols:
                conn.execute(text("ALTER TABLE chat_messages ADD COLUMN citation_coverage FLOAT"))
                print("[迁移] chat_messages 已增加 citation_coverage 字段")
            if "citation_dropped" not in cols:
                conn.execute(text("ALTER TABLE chat_messages ADD COLUMN citation_dropped INTEGER"))
                print("[迁移] chat_messages 已增加 citation_dropped 字段")
            if "agent_trace_id" not in cols:
                conn.execute(text("ALTER TABLE chat_messages ADD COLUMN agent_trace_id VARCHAR"))
                print("[迁移] chat_messages 已增加 agent_trace_id 字段")

        # 备忘录文件夹：memos 补 folder_id（memo_folders 新表由 create_all 自动创建）
        if insp.has_table("memos"):
            cols = [c["name"] for c in insp.get_columns("memos")]
            if "folder_id" not in cols:
                conn.execute(text("ALTER TABLE memos ADD COLUMN folder_id VARCHAR"))
                print("[迁移] memos 已增加 folder_id 字段")

        # memo_folders 补 parent_id（子文件夹层级）；老表 create_all 不会自动加列
        if insp.has_table("memo_folders"):
            cols = [c["name"] for c in insp.get_columns("memo_folders")]
            if "parent_id" not in cols:
                conn.execute(text("ALTER TABLE memo_folders ADD COLUMN parent_id VARCHAR"))
                print("[迁移] memo_folders 已增加 parent_id 字段")

        # flow_nodes 补工作流字段（依赖 / 负责人 / 门禁）
        if insp.has_table("flow_nodes"):
            cols = [c["name"] for c in insp.get_columns("flow_nodes")]
            if "depends_on" not in cols:
                conn.execute(text("ALTER TABLE flow_nodes ADD COLUMN depends_on JSON"))
                print("[迁移] flow_nodes 已增加 depends_on 字段")
            if "owner_id" not in cols:
                conn.execute(text("ALTER TABLE flow_nodes ADD COLUMN owner_id VARCHAR"))
                print("[迁移] flow_nodes 已增加 owner_id 字段")
            if "is_gate" not in cols:
                conn.execute(text(
                    f"ALTER TABLE flow_nodes ADD COLUMN is_gate BOOLEAN NOT NULL {default_false}"
                ))
                print("[迁移] flow_nodes 已增加 is_gate 字段")

        # source_documents 补版本台账 + 任务归属（迭代 2）
        if insp.has_table("source_documents"):
            cols = [c["name"] for c in insp.get_columns("source_documents")]
            if "version" not in cols:
                conn.execute(text(
                    "ALTER TABLE source_documents ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
                ))
                print("[迁移] source_documents 已增加 version 字段")
            if "supersedes_id" not in cols:
                conn.execute(text("ALTER TABLE source_documents ADD COLUMN supersedes_id VARCHAR"))
                print("[迁移] source_documents 已增加 supersedes_id 字段")
            if "task_id" not in cols:
                conn.execute(text("ALTER TABLE source_documents ADD COLUMN task_id VARCHAR"))
                print("[迁移] source_documents 已增加 task_id 字段")

        # task_instances 补 due_date（任务交付截止时间：到期视图依赖它）
        if insp.has_table("task_instances"):
            cols = [c["name"] for c in insp.get_columns("task_instances")]
            if "due_date" not in cols:
                conn.execute(text("ALTER TABLE task_instances ADD COLUMN due_date DATETIME"))
                print("[迁移] task_instances 已增加 due_date 字段")

        # agent_runs 补 thread_id（HITL 审批后从中断点恢复同一次运行需要）
        if insp.has_table("agent_runs"):
            cols = [c["name"] for c in insp.get_columns("agent_runs")]
            if "thread_id" not in cols:
                conn.execute(text("ALTER TABLE agent_runs ADD COLUMN thread_id VARCHAR"))
                print("[迁移] agent_runs 已增加 thread_id 字段")
            # 引用校验覆盖率（改造 3）：评测页"引用正确率"取它的均值
            if "citation_coverage" not in cols:
                conn.execute(text("ALTER TABLE agent_runs ADD COLUMN citation_coverage FLOAT"))
                print("[迁移] agent_runs 已增加 citation_coverage 字段")
            if "citation_dropped" not in cols:
                conn.execute(text("ALTER TABLE agent_runs ADD COLUMN citation_dropped INTEGER"))
                print("[迁移] agent_runs 已增加 citation_dropped 字段")

    db = OrmSession(bind=engine)
    try:
        admin = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
        if admin and admin.role != ROLE_SUPER_ADMIN:
            admin.role = ROLE_SUPER_ADMIN
            print(f"[迁移] 内置账号 {admin.username} 已升级为超级管理员")
        db.commit()
    finally:
        db.close()


def init_admin():
    """首次启动时创建超级管理员账号"""
    from sqlalchemy.orm import Session
    db = Session(bind=engine)
    try:
        admin = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
        if not admin:
            admin = User(
                username=settings.ADMIN_USERNAME,
                email=settings.ADMIN_EMAIL,
                password_hash=hash_password(settings.ADMIN_PASSWORD),
                role="super_admin",
            )
            db.add(admin)
            db.commit()
            print(f"[初始化] 超级管理员账号已创建: {settings.ADMIN_USERNAME}")
    finally:
        db.close()


def _sync_qdrant_shared_flags():
    """把数据库里的可见范围同步到已有向量 payload（幂等）

    否则老向量缺少 visibility / shared_user_ids 字段，普通用户检索时会被过滤条件排除。
    """
    from sqlalchemy.orm import Session
    from app.services import qdrant_service
    from app.models import KnowledgeItem

    db = Session(bind=engine)
    synced = 0
    try:
        rows = db.query(KnowledgeItem).filter(KnowledgeItem.status == "published").all()
        for it in rows:
            qdrant_service.set_payload_for_points([it.id], {
                "visibility": it.visibility or "private",
                "shared_user_ids": it.shared_user_ids or [],
                "is_shared": bool(it.is_shared),
            })
            synced += 1
        if synced:
            print(f"[初始化] 向量可见范围已同步（{synced} 条已发布条目）")
    except Exception as e:
        print(f"[警告] 向量可见范围同步失败: {e}")
    finally:
        db.close()


def _probe_embedding_dim():
    """探测当前实际使用的 embedding 模型维度（探测失败返回 None，不猜测）

    用第一个可用用户（其 API Key 配置）对样例文本做一次 embedding，
    保证 Qdrant 集合维度与真实模型一致，避免 upsert/检索时维度不匹配。
    失败时返回 None，调用方保持现有集合不动（绝不能盲目用默认维度重建）。
    """
    from app.db.session import SessionLocal
    from app.services.embedding_service import embed_dense

    db = SessionLocal()
    try:
        user = (
            db.query(User).filter(User.role == "super_admin").first()
            or db.query(User).first()
        )
        if user is None:
            return None
        return len(embed_dense("维度探测", user))
    except Exception as e:
        print(f"[警告] embedding 维度探测失败，保持现有 Qdrant 集合不变: {e}")
        return None
    finally:
        db.close()


def _resync_all_vectors():
    """把全部已发布知识条目重新向量化写入 Qdrant（集合新建/重建后调用）"""
    from sqlalchemy.orm import Session as OrmSession
    from app.db.session import SessionLocal
    from app.services import qdrant_service
    from app.services.embedding_service import embed_dense, embed_sparse
    from app.models import KnowledgeItem

    db = SessionLocal()
    try:
        user = (
            db.query(User).filter(User.role == "super_admin").first()
            or db.query(User).first()
        )
        items = db.query(KnowledgeItem).filter(KnowledgeItem.status == "published").all()
        ok = 0
        for it in items:
            try:
                text = it.to_search_text()
                qdrant_service.upsert_item(
                    item_id=it.id,
                    user_id=it.user_id,
                    dense_vector=embed_dense(text, user),
                    sparse_vector=embed_sparse(text),
                    payload={
                        "title": it.title,
                        "content": text,
                        "is_shared": bool(it.is_shared),
                        "visibility": it.visibility or "private",
                        "shared_user_ids": it.shared_user_ids or [],
                    },
                )
                ok += 1
            except Exception as e:
                print(f"[警告] 条目「{it.title}」向量化失败，跳过: {e}")
        print(f"[初始化] 向量回灌完成（{ok}/{len(items)} 条已发布条目）")
    except Exception as e:
        print(f"[警告] 向量回灌失败: {e}")
    finally:
        db.close()


def init_qdrant():
    """确保 Qdrant 集合存在（Qdrant 未启动时跳过，不影响其他功能）"""
    try:
        from app.services import qdrant_service
        dense_size = _probe_embedding_dim()
        created = qdrant_service.ensure_collection(dense_size=dense_size)
        if created:
            _resync_all_vectors()
        _sync_qdrant_shared_flags()
        print(f"[初始化] Qdrant 集合已就绪（dense 维度={dense_size or '沿用现有'}）")
    except Exception as e:
        print(f"[警告] Qdrant 连接失败，向量检索功能暂不可用: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_admin()
    init_migrate()
    init_qdrant()
    yield


app = FastAPI(title="新员工入职 AI 问答助手", version="0.1.0", lifespan=lifespan)

# CORS：生产环境通过 CORS_ORIGINS 显式配置（逗号分隔）；留空则放行所有来源（仅限本机开发）
_allowed_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(items_router)
app.include_router(chat_router)
app.include_router(documents_router)
app.include_router(ingest_router)
app.include_router(flows_router)
app.include_router(evaluation_router)
app.include_router(source_documents_router)
app.include_router(tasks_router)
app.include_router(memos_router)
app.include_router(memo_folders_router)
app.include_router(chat_messages_router)
# Agent 灰度（M1）：状态查询 / 轨迹查看 / 手动运行（新增接口，不影响既有接口）
app.include_router(agent_router)
# 三层记忆（M4）：查看/清空长期记忆、未答问题、记忆重建（全部为新增接口）
app.include_router(memory_router)
# 主动巡检（M5）：手动/定时触发巡检、查看巡检报告（全部为新增接口）
app.include_router(patrol_router)
# 文档确认清单（迭代 4）：发起/查看确认、待我确认、免登录确认通道（全部为新增接口）
app.include_router(doc_confirm_router)
app.include_router(confirmations_router)
app.include_router(public_confirm_router)


# 工作流校验类错误统一转成 4xx：节点依赖/负责人配置不合法 → 400；节点被阻塞 → 409
@app.exception_handler(FlowNodeValidationError)
async def _flow_node_validation_handler(request, exc: FlowNodeValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(WorkflowBlockedError)
async def _workflow_blocked_handler(request, exc: WorkflowBlockedError):
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": exc.code})


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---- 生产部署：由后端直接托管前端构建产物（frontend/dist），单端口部署 ----
# 存在 dist 目录时生效；开发模式（vite 5173 + 代理）不受影响。
# 注意：必须注册在所有 API 路由之后；/api、/docs、/openapi.json 不走 SPA 回退。
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

if _FRONTEND_DIST.is_dir():
    _ASSETS_DIR = _FRONTEND_DIST / "assets"
    if _ASSETS_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="spa-assets")

    _SPA_EXCLUDED = ("api/", "docs", "redoc", "openapi.json")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        if full_path.startswith(_SPA_EXCLUDED):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (_FRONTEND_DIST / full_path).resolve()
        # 只允许命中 dist 目录内的真实文件，防路径穿越
        if (
            full_path
            and str(candidate).startswith(str(_FRONTEND_DIST))
            and candidate.is_file()
        ):
            return FileResponse(candidate)
        return FileResponse(_FRONTEND_DIST / "index.html")
