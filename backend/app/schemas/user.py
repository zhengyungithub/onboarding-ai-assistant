"""用户相关的请求/响应模型"""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    """管理员创建用户"""
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=100)
    email: Optional[str] = None
    role: str = Field("user", pattern="^(user|admin|super_admin)$")  # 超管唯一，接口层面不可分配


class UserUpdate(BaseModel):
    """管理员更新用户信息"""
    username: Optional[str] = Field(None, min_length=3, max_length=50)
    password: Optional[str] = Field(None, min_length=6, max_length=100)
    email: Optional[str] = None
    role: Optional[str] = Field(None, pattern="^(user|admin|super_admin)$")


class UserLogin(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: str
    username: str
    email: Optional[str]
    role: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class APIKeyUpdate(BaseModel):
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    embedding_api_key: Optional[str] = None
    embedding_model: Optional[str] = None


class APIKeyResponse(BaseModel):
    llm_base_url: Optional[str]
    llm_model: Optional[str]
    embedding_model: Optional[str]
    llm_api_key_configured: bool  # 是否已配置（不返回明文）
    embedding_api_key_configured: bool


class ForgotPasswordRequest(BaseModel):
    """申请重置密码：发送验证码"""
    username: str
    email: str


class ResetPasswordRequest(BaseModel):
    """重置密码：验证码 + 新密码"""
    username: str
    email: str
    code: str
    new_password: str = Field(..., min_length=6, max_length=100)
