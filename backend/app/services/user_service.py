"""用户服务"""
from sqlalchemy.orm import Session
from app.models import User
from app.schemas.user import UserCreate, APIKeyUpdate
from app.core.security import hash_password, verify_password, create_access_token, encrypt_api_key


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter(User.username == username).first()


def create_user(db: Session, user_in: UserCreate, role: str = "user") -> User:
    user = User(
        username=user_in.username,
        email=getattr(user_in, 'email', None),
        password_hash=hash_password(user_in.password),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = get_user_by_username(db, username)
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


def update_api_keys(db: Session, user: User, keys: APIKeyUpdate) -> User:
    if keys.llm_api_key is not None:
        user.llm_api_key = encrypt_api_key(keys.llm_api_key) if keys.llm_api_key else None
    if keys.llm_base_url is not None:
        user.llm_base_url = keys.llm_base_url
    if keys.llm_model is not None:
        user.llm_model = keys.llm_model
    if keys.embedding_api_key is not None:
        user.embedding_api_key = encrypt_api_key(keys.embedding_api_key) if keys.embedding_api_key else None
    if keys.embedding_model is not None:
        user.embedding_model = keys.embedding_model
    db.commit()
    db.refresh(user)
    return user
