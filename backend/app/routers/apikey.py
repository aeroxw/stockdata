"""APIKey 自助管理：创建 / 列表 / 吊销 / 轮换。

核心安全设计：明文密钥**只在创建（或轮换）成功时返回一次**，
数据库只存 sha256 哈希；列表接口返回脱敏前缀。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import generate_api_key
from app.db import get_db
from app.deps import get_current_user
from app.models import ApiKey, User
from app.plans import get_plan
from app.ratelimit import blacklist_key
from app.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiResponse

router = APIRouter(prefix="/apikey", tags=["API Key"])


def _to_out(k: ApiKey) -> dict:
    return ApiKeyOut(
        id=k.id,
        name=k.name,
        key_prefix=k.key_prefix,
        masked=f"{k.key_prefix}****",
        scopes=k.scopes or [],
        rate_limit=k.rate_limit,
        is_active=k.is_active,
        total_calls=k.total_calls or 0,
        last_used=k.last_used,
        expires_at=k.expires_at,
        created_at=k.created_at,
    ).model_dump()


@router.post("", response_model=ApiResponse, summary="创建 API Key")
def create_key(payload: ApiKeyCreate, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    #: 配额一律从 plans 表读，后台改套餐即时生效（不再硬编码 free/pro/vip 三档）
    plan = get_plan(db, user.tier)
    max_keys = plan["max_keys"]

    existing = db.query(ApiKey).filter(
        ApiKey.user_id == user.id, ApiKey.is_active.is_(True)
    ).count()
    if existing >= max_keys:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"当前档位（{plan['name']}）最多创建 {max_keys} 个 API Key，"
            f"如需更多请签到攒积分兑换更高档位，或联系管理员调整",
        )

    raw, kh, prefix = generate_api_key()
    key = ApiKey(
        user_id=user.id,
        name=payload.name,
        key_hash=kh,
        key_prefix=prefix,
        scopes=payload.scopes,
        #: 未显式指定时取套餐默认限流
        rate_limit=payload.rate_limit or plan["rate_limit"],
        expires_at=(
            datetime.utcnow() + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days
            else None
        ),
        created_at=datetime.utcnow(),
    )
    db.add(key)
    db.commit()
    db.refresh(key)

    data = _to_out(key)
    data["secret"] = raw      # ⚠️ 唯一一次返回明文
    return ApiResponse(data=data)


@router.get("", response_model=ApiResponse, summary="我的 API Key 列表")
def list_keys(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    keys = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user.id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )
    return ApiResponse(data=[_to_out(k) for k in keys])


@router.delete("/{key_id}", response_model=ApiResponse, summary="吊销 API Key")
def revoke_key(key_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    key = db.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API Key 不存在")

    key.is_active = False
    db.commit()
    blacklist_key(key.key_hash)   # 即时生效，无需等待缓存过期
    return ApiResponse(msg="已吊销", data={"id": key_id})


@router.delete("/{key_id}/purge", response_model=ApiResponse, summary="永久删除已吊销的 API Key")
def purge_key(key_id: int, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """物理删除记录。

    与「吊销」的区别：
      吊销 = 软删除，保留记录与调用次数，可追溯；
      删除 = 从库里抹掉，不可恢复。

    因此**只允许删除已吊销的密钥** —— 还在生效的密钥必须先吊销，
    避免误操作让线上程序突然 401 却查不到是谁在用。

    注：api_logs 里的历史记录会保留（key_id 成为悬空值），
    这是有意的：删除密钥不应该连带抹掉审计轨迹。
    """
    key = db.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API Key 不存在")

    if key.is_active:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "只能删除已吊销的密钥，请先吊销再删除",
        )

    db.delete(key)
    db.commit()
    return ApiResponse(msg="已永久删除", data={"id": key_id})


@router.post("/{key_id}/rotate", response_model=ApiResponse, summary="轮换 API Key")
def rotate_key(key_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """生成新密钥替换旧的，旧密钥立即失效。"""
    key = db.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API Key 不存在")

    old_hash = key.key_hash
    raw, kh, prefix = generate_api_key()
    key.key_hash = kh
    key.key_prefix = prefix
    db.commit()
    blacklist_key(old_hash)
    db.refresh(key)

    data = _to_out(key)
    data["secret"] = raw
    return ApiResponse(msg="已轮换，请妥善保存新密钥", data=data)
