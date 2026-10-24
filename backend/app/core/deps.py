"""认证依赖：JWT 鉴权 + 当前用户获取"""
from fastapi import Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from typing import Optional
from app.db.session import get_db
from app.core.security import decode_access_token
from app.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
# 可选 token：用于图片接口，<img> 标签无 Authorization header 时回退到 query 参数
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _get_user_from_token(token: str, db: Session) -> User:
    """根据 token 解析用户，失败抛 401"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception
    user_id: str = payload.get("sub")
    if user_id is None:
        raise credentials_exception
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception
    return user


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    return _get_user_from_token(token, db)


def get_current_user_with_token_param(
    token: Optional[str] = Query(None, description="JWT token（用于 <img> 等无法携带 header 的场景）"),
    auth_token: Optional[str] = Depends(oauth2_scheme_optional),
    db: Session = Depends(get_db),
) -> User:
    """支持从 Authorization header 或 token query 参数获取 token

    用于图片接口：<img> 标签无法设置 Authorization header，
    需通过 URL ?token=xxx 传递 token。
    """
    # 优先用 header 中的 token，其次用 query 参数
    real_token = auth_token or token
    if not real_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无法验证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _get_user_from_token(real_token, db)


def get_current_staff(current_user: User = Depends(get_current_user)) -> User:
    """管理员或超管（可进后台、可见全局数据）"""
    from app.core.roles import is_staff

    if not is_staff(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


def get_current_admin(current_user: User = Depends(get_current_staff)) -> User:
    """兼容旧命名：等同 get_current_staff"""
    return current_user


def get_current_super_admin(current_user: User = Depends(get_current_user)) -> User:
    """仅超管（角色分配、管理员管理）"""
    from app.core.roles import is_super_admin

    if not is_super_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要超级管理员权限",
        )
    return current_user
