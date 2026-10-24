"""角色权限矩阵回归

守护点：三级角色（普通用户 / 管理员 / 超管）的用户管理权限分隔、
超管唯一性与不可删除不可降级、后台管理接口的角色门槛。
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
        u for u in _list_users(client, ctx["sa_token"]) if u["role"] == SUPER_ROLE
    )


def test_builtin_account_is_super_admin(client, ctx):
    """内置账号必须是超管，且全系统只有一个超管"""
    me = client.get("/api/auth/me", headers=hdr(ctx["sa_token"]))
    expect(me, 200, "读取自身信息")
    assert me.json()["role"] == SUPER_ROLE, f"内置账号角色应为超管，实际 {me.json()['role']}"

    supers = [u for u in _list_users(client, ctx["sa_token"]) if u["role"] == SUPER_ROLE]
    assert len(supers) == 1, f"超管必须唯一，实际 {len(supers)} 个"


def test_super_admin_can_create_admin(ctx):
    """超管可创建管理员（由 ctx 构造，这里校验结果）"""
    assert ctx["admin"]["user"]["role"] == "admin"


def test_admin_sees_only_normal_users(client, ctx):
    """管理员查用户列表：只能看到普通用户"""
    roles = {u["role"] for u in _list_users(client, ctx["admin"]["token"])}
    assert roles <= {"user"}, f"管理员不应看到管理员/超管，实际角色集合 {roles}"


def test_super_admin_sees_admin_and_users(client, ctx):
    """超管查用户列表：管理员与普通用户都能看到"""
    roles = {u["role"] for u in _list_users(client, ctx["sa_token"])}
    assert {"user", "admin"} <= roles, f"超管应同时看到管理员与普通用户，实际 {roles}"


def test_normal_user_cannot_list_users(client, ctx):
    """普通用户无权访问用户管理"""
    expect(
        client.get("/api/auth/users", headers=hdr(ctx["ua"]["token"])),
        403, "普通用户查询用户列表",
    )


def test_admin_cannot_create_admin(client, ctx):
    """管理员不能创建管理员"""
    r = raw_create_user(client, ctx["admin"]["token"], f"{RUN_TAG}_illegal_admin", "admin")
    expect(r, 403, "管理员创建管理员")


def test_nobody_can_create_super_admin(client, ctx):
    """超管唯一：任何角色都不能创建第二个超管"""
    for label, token in (("管理员", ctx["admin"]["token"]), ("超管", ctx["sa_token"])):
        r = raw_create_user(client, token, f"{RUN_TAG}_illegal_super", SUPER_ROLE)
        expect(r, 403, f"{label}创建超管")


def test_admin_can_create_normal_user(client, ctx):
    """管理员可以创建普通用户"""
    u = create_user(client, ctx["admin"]["token"], f"{RUN_TAG}_by_admin", "user")
    assert u["role"] == "user"


def test_admin_cannot_manage_other_admin(client, ctx):
    """管理员不能修改/删除另一个管理员；超管可以"""
    other = create_user(client, ctx["sa_token"], f"{RUN_TAG}_admin2", "admin")
    admin_token = ctx["admin"]["token"]

    expect(
        client.put(f"/api/auth/users/{other['id']}", json={"email": "x@y.z"}, headers=hdr(admin_token)),
        403, "管理员修改另一个管理员",
    )
    expect(
        client.delete(f"/api/auth/users/{other['id']}", headers=hdr(admin_token)),
        403, "管理员删除另一个管理员",
    )
    expect(
        client.delete(f"/api/auth/users/{other['id']}", headers=hdr(ctx["sa_token"])),
        204, "超管删除管理员",
    )


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


def test_role_change_takes_effect(client, ctx):
    """角色变更生效：普通用户提升为管理员后能进后台管理"""
    u = create_user(client, ctx["sa_token"], f"{RUN_TAG}_promote", "user")
    token = login(client, u["username"], DEFAULT_PASSWORD)
    expect(client.get("/api/auth/users", headers=hdr(token)), 403, "提升前访问用户管理")

    expect(
        client.put(f"/api/auth/users/{u['id']}", json={"role": "admin"}, headers=hdr(ctx["sa_token"])),
        200, "提升为管理员",
    )
    expect(client.get("/api/auth/users", headers=hdr(token)), 200, "提升后访问用户管理")

    # 降回普通用户，权限自动收回
    expect(
        client.put(f"/api/auth/users/{u['id']}", json={"role": "user"}, headers=hdr(ctx["sa_token"])),
        200, "降级为普通用户",
    )
    expect(client.get("/api/auth/users", headers=hdr(token)), 403, "降级后访问用户管理")


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


def test_created_user_is_usable(client, ctx, db):
    """创建的用户能登录且角色正确落库"""
    u = create_user(client, ctx["sa_token"], f"{RUN_TAG}_login_ok", "user")
    assert login(client, u["username"], DEFAULT_PASSWORD)
    assert user_obj(db, u["username"]).role == "user"
