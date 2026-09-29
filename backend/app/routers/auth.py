"""用户注册 / 登录 / 资料。"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.plans import get_plan_row
from app.schemas import ApiResponse, LoginIn, RefreshIn, RegisterIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["用户"])


@router.post("/register", response_model=ApiResponse, summary="注册")
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该邮箱已注册")

    # 第一个注册的用户自动成为管理员，方便开箱即用
    is_first = db.scalar(select(User).limit(1)) is None

    #: 新用户试用期：注册即给 TRIAL_DAYS 天体验，到期时间记在 plan_expires_at，
    #: 和付费套餐共用一套判断逻辑。
    now = datetime.utcnow()
    tier, expires, is_trial = "free", None, False
    if settings.TRIAL_ENABLED and settings.TRIAL_DAYS > 0:
        # 试用套餐可能已被后台删除/改名，查不到就退回 free ——
        # 不能让"配了个不存在的套餐"把注册流程搞成 500。
        row = get_plan_row(db, settings.TRIAL_PLAN or "free")
        tier = row.code if row else "free"
        expires = now + timedelta(days=settings.TRIAL_DAYS)
        is_trial = True

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        tier=tier,
        is_admin=is_first,
        created_at=now,
        plan_expires_at=expires,
        is_trial=is_trial,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return ApiResponse(data=UserOut.model_validate(user).model_dump())


@router.post("/login", response_model=ApiResponse, summary="登录")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码错误")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用")

    user.last_login = datetime.utcnow()
    db.commit()

    return ApiResponse(
        data=TokenOut(
            access_token=create_access_token(str(user.id)),
            refresh_token=create_refresh_token(str(user.id)),
        ).model_dump()
    )


@router.post("/refresh", response_model=ApiResponse, summary="刷新访问令牌")
def refresh(payload: RefreshIn, db: Session = Depends(get_db)):
    """用 refresh_token 换一对新的令牌。

    access_token 只有 2 小时寿命，前端在 401 时会拿 refresh_token 来这里续期，
    避免用户每两小时被踢回登录页。refresh_token 本身有 7 天寿命。
    """
    claims = decode_token(payload.refresh_token)
    if not claims or claims.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "刷新令牌无效或已过期")

    user = db.get(User, int(claims["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在或已被禁用")

    return ApiResponse(
        data=TokenOut(
            access_token=create_access_token(str(user.id)),
            refresh_token=create_refresh_token(str(user.id)),
        ).model_dump()
    )


@router.get("/me", response_model=ApiResponse, summary="当前用户")
def me(user: User = Depends(get_current_user)):
    return ApiResponse(data=UserOut.model_validate(user).model_dump())
