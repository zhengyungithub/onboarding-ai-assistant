"""回归测试公共设施（fixture）

隔离原则：在任何 app 模块被导入之前，把 DATABASE_URL 指向独立测试库，
因此测试永远不会读写真实的 hr_ai.db（pydantic-settings 中环境变量优先级高于 .env）。
"""
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

TEST_DB_PATH = BACKEND_DIR / "test_regression.db"

# 必须早于 app.* 的导入
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB_PATH.as_posix()
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:6333")
# 测试密码显式固定，与真实 .env 解耦（pydantic-settings 中环境变量优先级高于 .env 文件）
os.environ["ADMIN_PASSWORD"] = "change-me-on-first-login"

# 每次运行重建测试库，保证结果可重复、不受上一次残留影响
for _p in [TEST_DB_PATH, Path(str(TEST_DB_PATH) + "-wal"), Path(str(TEST_DB_PATH) + "-shm")]:
    if _p.exists():
        _p.unlink()

import pytest                                    # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402

from app.main import app                         # noqa: E402
from app.db.session import SessionLocal          # noqa: E402
from tests.helpers import create_user, login     # noqa: E402

SUPER_ADMIN_USERNAME = "admin"
SUPER_ADMIN_PASSWORD = "change-me-on-first-login"  # 与 config 默认值保持一致；线上真实密码由 .env 提供
RUN_TAG = "regr"


@pytest.fixture(scope="session")
def client():
    """进程内应用客户端（with 语句会触发 lifespan：建表 / 迁移 / 初始化超管）"""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    """独立数据库会话（每个用例一个，避免对象缓存导致脏读）"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="session")
def ctx(client):
    """账号矩阵：内置超管 + 1 个后台账号（regr_admin）+ 3 个普通用户（ua / ub / uc）

    两级角色模型（2026-10-25）：历史 admin 角色已取消。
    大量工具级用例通过用户名 regr_admin 取「一个具备后台权限的账号」，
    故该账号照常创建（API 层建为普通用户），再在库中直接提升为超管——
    超管唯一性约束只存在于 API 层（不可创建/提升），库中多个超管身份仅供测试。
    """
    sa_token = login(client, SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD)
    sa_me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {sa_token}"}).json()

    # 兼容槽位：regr_admin（工具级用例的固定主角）
    legacy = create_user(client, sa_token, f"{RUN_TAG}_admin", "user")
    legacy_token = login(client, legacy["username"])
    from app.db.session import SessionLocal
    from app.models import User as UserModel
    s = SessionLocal()
    try:
        _u = s.query(UserModel).filter(UserModel.username == legacy["username"]).first()
        _u.role = "super_admin"
        s.commit()
    finally:
        s.close()
    legacy_me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {legacy_token}"}).json()

    users = {}
    for name in ("ua", "ub", "uc"):
        u = create_user(client, sa_token, f"{RUN_TAG}_{name}", "user")
        users[name] = {"user": u, "token": login(client, u["username"])}

    return {
        "sa_token": sa_token,
        "super": {"username": SUPER_ADMIN_USERNAME},
        "admin": {"user": legacy_me, "token": legacy_token},
        "ua": users["ua"],
        "ub": users["ub"],
        "uc": users["uc"],
    }
