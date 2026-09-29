"""认证与 APIKey 工具。

密码    : argon2 哈希
会话    : JWT（access + refresh）
APIKey  : sk_live_<32位随机>，数据库只存 sha256 哈希
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")


# ---------------------------------------------------------------- 密码


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(raw, hashed)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- JWT


def create_access_token(subject: str, expires_minutes: int | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return jwt.encode(
        {"sub": subject, "exp": expire, "type": "access"},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_refresh_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    return jwt.encode(
        {"sub": subject, "exp": expire, "type": "refresh"},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    except JWTError:
        return None


# ---------------------------------------------------------------- APIKey


def generate_api_key() -> tuple[str, str, str]:
    """返回 (明文, sha256哈希, 展示前缀)。明文只在创建时返回一次。"""
    raw = "sk_live_" + secrets.token_urlsafe(28)
    return raw, hash_api_key(raw), raw[:16]


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mask_key(prefix: str) -> str:
    """sk_live_xxxx -> sk_live_xxxx****"""
    return f"{prefix}****"
