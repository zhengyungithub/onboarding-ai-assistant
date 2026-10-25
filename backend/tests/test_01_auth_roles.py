"""角色权限矩阵回归（两级角色模型，2026-10-25 收敛）

守护点：超管唯一性与不可删除不可降级、普通用户无法访问后台接口、
历史 admin 角色不可再创建/分配、API Key 配置仅超管可用。
"""
from tests.helpers import (
    create_user, expect, hdr, login, raw_create_user, user_obj, DEFAULT_PASSWORD,
)
from tests.conftest import RUN_TAG

SUPER_ROLE = "super_admin"


def _list_users(client, token):
    r = client.get("/api/auth/users", headers=hdr(token))
    expect(r, 200, "查询用户列表")
    return r.json()


def _super_row(client, ctx):
    return next(
        u for u in _list_users(client, ctx["sa_token"])
        if u["role"] == SUPER_ROLE and u["username"] == "admin"
    )


def test_builtin_account_is_super_admin(client, ctx):
    """内置账号必须是超管（唯一性由 API 层守护：见 test_nobody_can_create_super_admin）"""
    me = client.get("/api/auth/me", headers=hdr(ctx["sa_token"]))
    expect(me, 200, "读取自身信息")
    assert me.json()["role"] == SUPER_ROLE, f"内置账号角色应为超管，实际 {me.json()['role']}"

    supers = [u for u in _list_users(client, ctx["sa_token"]) if u["role"] == SUPER_ROLE]
    assert any(u["username"] == "admin" for u in supers), "内置超管必须在列"


def test_legacy_admin_role_is_no_longer_assignable(client, ctx):
    """历史 admin 角色已取消：超管也无法再创建 admin 角色账号"""
    r = raw_create_user(client, ctx["sa_token"], f"{RUN_TAG}_legacy_admin", "admin")
    assert r.status_code in (400, 403), (
        f"创建 admin 角色应被拒绝，实际 {r.status_code}；响应：{r.text[:200]}"
    )


def test_super_admin_can_list_all_users(client, ctx):
    """超管查用户列表：能看到普通用户（含自己）"""
    roles = {u["role"] for u in _list_users(client, ctx["sa_token"])}
    assert "user" in roles, f"超管应能看到普通用户，实际角色集合 {roles}"


def test_normal_user_cannot_list_users(client, ctx):
    """普通用户无权访问用户管理"""
    expect(
        client.get("/api/auth/users", headers=hdr(ctx["ua"]["token"])),
        403, "普通用户查询用户列表",
    )


def test_nobody_can_create_super_admin(client, ctx):
    """超管唯一：任何角色都不能创建第二个超管"""
    for label, token in (("普通用户", ctx["ua"]["token"]), ("超管", ctx["sa_token"])):
        r = raw_create_user(client, token, f"{RUN_TAG}_illegal_super_{label}", SUPER_ROLE)
        expect(r, 403, f"{label}创建超管")


def test_super_admin_can_create_normal_user(client, ctx):
    """超管可以创建普通用户"""
    u = create_user(client, ctx["sa_token"], f"{RUN_TAG}_by_sa", "user")
    assert u["role"] == "user"


def test_super_admin_cannot_be_deleted_or_demoted(client, ctx):
    """超管账号不可删除、不可降级 —— 权限体系的锚点"""
    sa = _super_row(client, ctx)
    expect(
        client.delete(f"/api/auth/users/{sa['id']}", headers=hdr(ctx["sa_token"])),
        400, "删除超管",
    )
    # 降级：先被「不能改自己角色」拦（403），即使绕过也会被「超管不可降级」兜底（400）
    r = client.put(f"/api/auth/users/{sa['id']}", json={"role": "user"}, headers=hdr(ctx["sa_token"]))
    assert r.status_code in (400, 403), (
        f"降级超管应被拒绝，实际 {r.status_code}；响应：{r.text[:200]}"
    )
    # 两次尝试后超管身份必须原样保留
    assert _super_row(client, ctx)["id"] == sa["id"], "超管账号必须始终存在"


def test_cannot_delete_self(client, ctx):
    """不能删除自己（避免把自己踢出系统）"""
    expect(
        client.delete(f"/api/auth/users/{ctx['admin']['user']['id']}", headers=hdr(ctx["admin"]["token"])),
        400, "删除自己",
    )


def test_normal_user_cannot_manage_users(client, ctx):
    """普通用户对其他账号无任何写权限"""
    target = ctx["uc"]["user"]["id"]
    token = ctx["ua"]["token"]
    expect(
        client.put(f"/api/auth/users/{target}", json={"email": "a@b.c"}, headers=hdr(token)),
        403, "普通用户修改他人信息",
    )
    expect(
        client.delete(f"/api/auth/users/{target}", headers=hdr(token)),
        403, "普通用户删除他人",
    )


def test_role_change_to_legacy_admin_rejected(client, ctx):
    """角色变更收敛：把普通用户改成历史 admin 角色应被拒绝"""
    u = create_user(client, ctx["sa_token"], f"{RUN_TAG}_promote", "user")
    token = login(client, u["username"], DEFAULT_PASSWORD)
    expect(client.get("/api/auth/users", headers=hdr(token)), 403, "普通用户访问用户管理")

    r = client.put(
        f"/api/auth/users/{u['id']}",
        json={"role": "admin"},
        headers=hdr(ctx["sa_token"]),
    )
    assert r.status_code in (400, 403, 422), (
        f"改成历史 admin 角色应被拒绝，实际 {r.status_code}；响应：{r.text[:200]}"
    )
    # 权限不变：仍是普通用户，无法访问用户管理
    expect(client.get("/api/auth/users", headers=hdr(token)), 403, "角色未变仍无权限")


def test_password_reset_by_super_admin(client, ctx, db):
    """超管改密码后新密码可用、旧密码失效"""
    u = create_user(client, ctx["sa_token"], f"{RUN_TAG}_pwd", "user")
    new_pwd = "new-regr-pass-456"
    expect(
        client.put(f"/api/auth/users/{u['id']}", json={"password": new_pwd}, headers=hdr(ctx["sa_token"])),
        200, "重置密码",
    )
    assert login(client, u["username"], new_pwd), "新密码应可登录"
    r = client.post("/api/auth/login", json={"username": u["username"], "password": DEFAULT_PASSWORD})
    expect(r, 401, "旧密码登录应失败")


def test_staff_only_endpoints_reject_normal_user(client, ctx):
    """评测数据属后台功能：普通用户一律 403"""
    token = ctx["ua"]["token"]
    for path in ("/api/evaluation/stats", "/api/evaluation/cases", "/api/evaluation/pending-feedback"):
        expect(client.get(path, headers=hdr(token)), 403, f"普通用户访问 {path}")
    expect(
        client.post("/api/evaluation/cases",
                    json={"question": "x", "expected_item_id": "y"},
                    headers=hdr(token)),
        403, "普通用户创建测试用例",
    )


def test_api_keys_super_admin_only(client, ctx):
    """API Key 配置仅超管可用：普通用户读写都 403"""
    expect(
        client.get("/api/auth/api-keys", headers=hdr(ctx["ua"]["token"])),
        403, "普通用户读取 API Key 配置",
    )
    expect(
        client.put("/api/auth/api-keys",
                   json={"llm_base_url": "https://x", "llm_api_key": "sk-x"},
                   headers=hdr(ctx["ua"]["token"])),
        403, "普通用户修改 API Key 配置",
    )
    expect(
        client.get("/api/auth/api-keys", headers=hdr(ctx["sa_token"])),
        200, "超管读取 API Key 配置",
    )


def test_created_user_is_usable(client, ctx, db):
    """创建的用户能登录且角色正确落库"""
    u = create_user(client, ctx["sa_token"], f"{RUN_TAG}_login_ok", "user")
    assert login(client, u["username"], DEFAULT_PASSWORD)
    assert user_obj(db, u["username"]).role == "user"


def test_llm_falls_back_to_super_admin_config(client, ctx, db):
    """两级角色兜底链：普通用户未配 Key 时，自动使用超管的模型配置"""
    from app.services.llm_service import get_llm_for_user
    from app.core.security import encrypt_api_key

    sa_user = user_obj(db, "admin")
    ua = user_obj(db, ctx["ua"]["user"]["username"])
    assert not ua.llm_api_key, "前提：ua 未自己配置 Key"

    old = (sa_user.llm_api_key, sa_user.llm_base_url, sa_user.llm_model)
    sa_user.llm_api_key = encrypt_api_key("sk-super-test")
    sa_user.llm_base_url = "https://dashscope.example.com/compatible-mode/v1"
    sa_user.llm_model = "qwen-test"
    db.commit()
    try:
        llm = get_llm_for_user(ua, db)
        assert llm.model_name == "qwen-test", "模型名应跟随超管配置"
        assert "dashscope.example.com" in str(llm.openai_api_base), "Base URL 应跟随超管配置"
        assert llm.openai_api_key.get_secret_value() == "sk-super-test", "Key 应使用超管的"
    finally:
        sa_user.llm_api_key, sa_user.llm_base_url, sa_user.llm_model = old
        db.commit()
