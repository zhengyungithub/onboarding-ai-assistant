"""角色与数据可见范围

角色模型（三级分隔）：
- user        普通用户：消费端（AI 问答 / 流程步骤 / 我的任务 / 备忘录），
              知识库等页面可见，但只能操作自己的数据，检索范围 = 自己的 + 被共享的
- admin       管理员：业务内容生产者（知识库 / 资料入库 / 流程配置 / 评测数据），
              可管理普通用户，数据全局可见，创建内容时可设置可见范围
- super_admin 超管：唯一内置账号，额外拥有角色分配、管理员管理能力，不可删除 / 降级

内容可见范围（三态，管理员/超管可设置，普通用户一律仅自己）：
- private 仅自己
- all     全员共享（所有用户/管理员/超管可见可检索）
- custom  定向共享（仅 shared_user_ids 列表中的用户可见，只读）
"""
from sqlalchemy import or_, and_, cast, String

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_SUPER_ADMIN = "super_admin"

STAFF_ROLES = (ROLE_ADMIN, ROLE_SUPER_ADMIN)
ALL_ROLES = (ROLE_USER, ROLE_ADMIN, ROLE_SUPER_ADMIN)

# ===== 内容可见范围（三态） =====
VIS_PRIVATE = "private"   # 仅自己可见
VIS_ALL = "all"           # 全员共享（所有用户/管理员/超管可见可检索）
VIS_CUSTOM = "custom"     # 定向共享（仅 shared_user_ids 里的用户可见）

VISIBILITY_CHOICES = (VIS_PRIVATE, VIS_ALL, VIS_CUSTOM)

ROLE_LABELS = {
    ROLE_USER: "普通用户",
    ROLE_ADMIN: "管理员",
    ROLE_SUPER_ADMIN: "超级管理员",
}


def is_staff(user) -> bool:
    """管理员及以上：可进后台、可见全局数据、创建的内容默认共享"""
    return getattr(user, "role", None) in STAFF_ROLES


def is_super_admin(user) -> bool:
    return getattr(user, "role", None) == ROLE_SUPER_ADMIN


def assignable_roles(operator) -> list:
    """当前操作者可分配的角色（超管唯一，不可被创建或提升）"""
    if is_super_admin(operator):
        return [ROLE_USER, ROLE_ADMIN]
    if is_staff(operator):
        return [ROLE_USER]
    return []


def can_manage_user(operator, target) -> bool:
    """用户管理矩阵：超管管理管理员与普通用户；管理员只管理普通用户。

    自己的资料可以改（角色另判），所以本人直接放行。
    """
    if operator.id == target.id:
        return True
    if is_super_admin(operator):
        return target.role in (ROLE_USER, ROLE_ADMIN)
    if getattr(operator, "role", None) == ROLE_ADMIN:
        return target.role == ROLE_USER
    return False


def can_change_role(operator, target, new_role: str) -> bool:
    """能否把 target 的角色改为 new_role

    - 只能是当前操作者可分配的角色；
    - 超管本身不可被改角色（系统必须始终有超管）；
    - 自己不能改自己的角色（避免把自己降级后失去管理能力）。
    """
    if new_role not in assignable_roles(operator):
        return False
    if target.role == ROLE_SUPER_ADMIN:
        return False
    if operator.id == target.id:
        return False
    return can_manage_user(operator, target)


def normalize_visibility(user, visibility=None, shared_user_ids=None) -> tuple:
    """归一化可见范围设置

    - 普通用户：一律强制「仅自己」（需求：普通用户内容仅个人可见）
    - 管理员/超管：默认「全员共享」；可选「指定用户」
    - 选了「指定用户」但没选任何人 → 收敛为「仅自己」
    """
    if not is_staff(user):
        return VIS_PRIVATE, []
    vis = visibility if visibility in VISIBILITY_CHOICES else VIS_ALL
    ids: list = []
    if vis == VIS_CUSTOM:
        seen = set()
        for uid in (shared_user_ids or []):
            if uid and uid not in seen:
                seen.add(uid)
                ids.append(uid)
        if not ids:
            vis = VIS_PRIVATE
    return vis, ids


def apply_visibility(obj, user, visibility=None, shared_user_ids=None):
    """把归一化后的可见范围写到模型实例上（is_shared 作为派生兼容值同步维护）"""
    vis, ids = normalize_visibility(user, visibility, shared_user_ids)
    obj.visibility = vis
    obj.shared_user_ids = ids if ids else None
    obj.is_shared = vis != VIS_PRIVATE
    return obj


def filter_valid_user_ids(db, ids) -> list:
    """过滤掉不存在的用户 ID（保持传入顺序、去重）"""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return []
    from app.models import User
    valid = {r[0] for r in db.query(User.id).filter(User.id.in_(ids)).all()}
    return [i for i in ids if i in valid]


def read_scope(model, user):
    """读范围过滤条件

    - 管理员/超管：返回 None（全局可见）
    - 普通用户：自己的 + 全员共享的 + 定向共享给自己的
    """
    if is_staff(user):
        return None
    me = user.id
    conds = [model.user_id == me]
    if hasattr(model, "visibility"):
        conds.append(model.visibility == VIS_ALL)
        # 定向共享：JSON 数组文本包含自己的 id（带引号精确匹配）
        conds.append(and_(
            model.visibility == VIS_CUSTOM,
            cast(model.shared_user_ids, String).like(f'%"{me}"%'),
        ))
    else:
        conds.append(model.is_shared.is_(True))
    return or_(*conds)


def write_scope(model, user):
    """写范围过滤条件：staff 返回 None（可改所有人的）；普通用户只能改自己的"""
    if is_staff(user):
        return None
    return model.user_id == user.id
