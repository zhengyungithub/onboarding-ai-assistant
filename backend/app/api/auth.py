"""认证与用户相关 API"""
import random
import time
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models import User
from app.schemas.user import (
    UserLogin, UserResponse, TokenResponse,
    APIKeyUpdate, APIKeyResponse,
    UserCreate, UserUpdate,
    ForgotPasswordRequest, ResetPasswordRequest,
)
from app.services.user_service import get_user_by_username, create_user, authenticate_user, update_api_keys
from app.core.security import create_access_token, decrypt_api_key, hash_password
from app.core.deps import get_current_user, get_current_staff, get_current_super_admin
from app.core.roles import (
    ROLE_USER, ROLE_ADMIN, ROLE_SUPER_ADMIN, ROLE_LABELS,
    is_staff, is_super_admin, assignable_roles, can_manage_user, can_change_role,
)
from app.services.email_service import send_reset_password_code

router = APIRouter(prefix="/api/auth", tags=["认证"])

# 验证码存储：{username: {"code": str, "expire_at": float, "email": str}}
_reset_codes: dict = {}


def is_staff_role(role: str) -> bool:
    return role in (ROLE_ADMIN, ROLE_SUPER_ADMIN)


@router.post("/login", response_model=TokenResponse)
def login(user_in: UserLogin, db: Session = Depends(get_db)):
    user = authenticate_user(db, user_in.username, user_in.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token({"sub": user.id})
    return TokenResponse(access_token=token, user=UserResponse.model_validate(user))


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


# 管理员创建用户
@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user_by_admin(
    user_in: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    """创建用户：超管可建「普通用户 / 管理员」，管理员只能建「普通用户」。

    超管唯一（内置账号），任何情况下都不允许通过接口创建或提升。
    """
    if user_in.role not in assignable_roles(current_user):
        raise HTTPException(
            status_code=403,
            detail=f"无权限创建「{ROLE_LABELS.get(user_in.role, user_in.role)}」，可分配角色："
                   + "、".join(ROLE_LABELS[r] for r in assignable_roles(current_user)),
        )
    if get_user_by_username(db, user_in.username):
        raise HTTPException(status_code=400, detail="用户名已存在")
    user = create_user(db, user_in, role=user_in.role)
    return UserResponse.model_validate(user)


# 获取用户列表
@router.get("/users", response_model=list)
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    """用户列表：超管可见全部；管理员只可见普通用户"""
    query = db.query(User)
    if not is_super_admin(current_user):
        query = query.filter(User.role == ROLE_USER)
    users = query.order_by(User.created_at.desc()).all()
    return [UserResponse.model_validate(u) for u in users]


# 更新用户
@router.put("/users/{user_id}", response_model=UserResponse)
def update_user_by_admin(
    user_id: str,
    user_in: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    """更新用户信息（用户名、密码、邮箱、角色）

    权限：超管可管理管理员与普通用户；管理员只能管理普通用户；超管账号不可被修改/降级。
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not can_manage_user(current_user, user):
        raise HTTPException(status_code=403, detail="无权限修改该用户")

    update_data = user_in.model_dump(exclude_unset=True)

    # 角色变更校验
    if "role" in update_data and update_data["role"] != user.role:
        if not can_change_role(current_user, user, update_data["role"]):
            raise HTTPException(status_code=403, detail="无权限分配该角色")

    # 超管账号本身不可被降级（即使有其它字段一起提交）
    if user.role == ROLE_SUPER_ADMIN and update_data.get("role", ROLE_SUPER_ADMIN) != ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=400, detail="不能修改超级管理员的角色")

    # 修改用户名时检查是否重复
    if "username" in update_data and update_data["username"] != user.username:
        existing = get_user_by_username(db, update_data["username"])
        if existing:
            raise HTTPException(status_code=400, detail="用户名已存在")
        user.username = update_data["username"]

    # 修改密码
    if "password" in update_data and update_data["password"]:
        user.password_hash = hash_password(update_data["password"])

    # 修改邮箱
    if "email" in update_data:
        user.email = update_data["email"]

    # 修改角色
    if "role" in update_data:
        new_role = update_data["role"]
        if new_role != user.role:
            # 可见范围(visibility)是每条内容的显式设置，角色变更不批量改写；
            # 需要时由管理员在对应页面手动调整该用户名下内容的可见范围。
            user.role = new_role

    db.commit()
    db.refresh(user)
    return UserResponse.model_validate(user)


# 删除用户
@router.delete("/users/{user_id}", status_code=204)
def delete_user_by_admin(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_staff),
):
    """删除用户：超管可删管理员与普通用户；管理员只能删普通用户；超管账号不可删"""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == ROLE_SUPER_ADMIN:
        raise HTTPException(status_code=400, detail="不能删除超级管理员")
    if not can_manage_user(current_user, user):
        raise HTTPException(status_code=403, detail="无权限删除该用户")
    db.delete(user)
    db.commit()


@router.get("/api-keys", response_model=APIKeyResponse)
def get_api_keys(current_user: User = Depends(get_current_super_admin)):
    """API Key 配置仅超管可用；普通用户的回答走系统默认 Key（.env 的 DEFAULT_*）"""
    return APIKeyResponse(
        llm_base_url=current_user.llm_base_url,
        llm_model=current_user.llm_model,
        embedding_model=current_user.embedding_model,
        llm_api_key_configured=bool(current_user.llm_api_key),
        embedding_api_key_configured=bool(current_user.embedding_api_key),
    )


@router.put("/api-keys", response_model=APIKeyResponse)
def update_api_keys_endpoint(keys: APIKeyUpdate, current_user: User = Depends(get_current_super_admin), db: Session = Depends(get_db)):
    user = update_api_keys(db, current_user, keys)
    return APIKeyResponse(
        llm_base_url=user.llm_base_url,
        llm_model=user.llm_model,
        embedding_model=user.embedding_model,
        llm_api_key_configured=bool(user.llm_api_key),
        embedding_api_key_configured=bool(user.embedding_api_key),
    )


@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """忘记密码：验证用户名+邮箱，发送验证码"""
    user = get_user_by_username(db, req.username)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not user.email or user.email.lower() != req.email.lower():
        raise HTTPException(status_code=400, detail="用户名与邮箱不匹配")

    # 生成 6 位验证码
    code = f"{random.randint(0, 999999):06d}"
    _reset_codes[req.username] = {
        "code": code,
        "email": req.email.lower(),
        "expire_at": time.time() + 600,  # 10 分钟有效
    }

    ok = send_reset_password_code(req.email, code)
    if not ok:
        raise HTTPException(status_code=500, detail="验证码邮件发送失败，请检查 SMTP 配置")

    return {"message": "验证码已发送到您的邮箱，10 分钟内有效"}


@router.post("/reset-password")
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    """重置密码：验证验证码 + 更新密码"""
    stored = _reset_codes.get(req.username)
    if not stored:
        raise HTTPException(status_code=400, detail="请先获取验证码")
    if stored["email"] != req.email.lower():
        raise HTTPException(status_code=400, detail="邮箱不匹配")
    if time.time() > stored["expire_at"]:
        del _reset_codes[req.username]
        raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
    if stored["code"] != req.code:
        raise HTTPException(status_code=400, detail="验证码错误")

    user = get_user_by_username(db, req.username)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    user.password_hash = hash_password(req.new_password)
    db.commit()

    # 清除验证码
    del _reset_codes[req.username]

    return {"message": "密码重置成功，请使用新密码登录"}
