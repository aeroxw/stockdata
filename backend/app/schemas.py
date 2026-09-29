"""Pydantic 请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    """允许从 SQLAlchemy 实体直接构造（model_validate(orm_obj)）。

    SQLAlchemy 的 Mapped 属性不是 dict，Pydantic 默认拒绝，
    必须显式打开 from_attributes。
    """

    model_config = ConfigDict(from_attributes=True)


# ---------------- 统一响应 ----------------
class Meta(ORMModel):
    source: str | None = None
    cached: bool = False
    ts: datetime | None = None


class ApiResponse(BaseModel):
    code: int = 0
    msg: str = "ok"
    data: object | None = None
    meta: Meta | None = None


# ---------------- 认证 ----------------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshIn(BaseModel):
    refresh_token: str


class UserOut(ORMModel):
    id: int
    email: str
    tier: str
    is_admin: bool = False
    is_active: bool = True
    created_at: datetime | None = None
    last_login: datetime | None = None


# ---------------- APIKey ----------------
class ApiKeyCreate(BaseModel):
    name: str = "default"
    scopes: list[str] = Field(default_factory=lambda: ["quote", "kline", "special"])
    #: 不传则取档位默认限流
    rate_limit: int | None = Field(default=None, ge=1, le=100000)
    expires_in_days: int | None = None


class ApiKeyOut(ORMModel):
    id: int
    name: str
    key_prefix: str
    masked: str
    scopes: list[str]
    rate_limit: int
    is_active: bool
    total_calls: int
    last_used: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None


class ApiKeyCreated(ApiKeyOut):
    #: 仅创建时返回一次，之后不再可见
    secret: str


# ---------------- 档位（配额） ----------------
#: 注意：这里统称「档位」而不是「套餐」。开源版不做任何买卖，
#: 一组 plan 就是一组额度配置：能建几个 Key、每分钟限流多少、每天能调多少次。
class PlanOut(ORMModel):
    id: int | None = None
    code: str
    name: str
    level: int = 0
    max_keys: int = 3
    rate_limit: int = 60
    daily_quota: int = 1000
    features: list[str] = Field(default_factory=list)
    description: str = ""
    is_public: bool = True
    is_active: bool = True
    sort_order: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PlanIn(BaseModel):
    """后台新建档位。code 用小写字母/数字/下划线，因为它就是 users.tier。"""

    code: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_]{1,31}$")
    name: str = Field(min_length=1, max_length=64)
    level: int = 0
    max_keys: int = Field(default=3, ge=0)
    rate_limit: int = Field(default=60, ge=1)
    #: -1 = 不限
    daily_quota: int = Field(default=1000, ge=-1)
    #: 签到积分兑换价。0 或 null 表示不可兑换（免费档/隐藏档）
    redeem_points: int | None = Field(default=None, ge=0)
    redeem_days: int = Field(default=30, ge=1, le=3650)
    features: list[str] = Field(default_factory=list)
    description: str = ""
    is_public: bool = True
    is_active: bool = True
    sort_order: int = 0


class PlanUpdateIn(BaseModel):
    """后台改档位：只传要改的字段。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    level: int | None = None
    max_keys: int | None = Field(default=None, ge=0)
    rate_limit: int | None = Field(default=None, ge=1)
    daily_quota: int | None = Field(default=None, ge=-1)
    redeem_points: int | None = Field(default=None, ge=0)
    redeem_days: int | None = Field(default=None, ge=1, le=3650)
    features: list[str] | None = None
    description: str | None = None
    is_public: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = None


# ---------------- 积分 ----------------
class RedeemIn(BaseModel):
    """用积分兑换档位。plan_code 必须是配了兑换价的公开档位。"""

    plan_code: str = Field(min_length=1, max_length=32)


class AdminPointsIn(BaseModel):
    """管理员调整某个用户的积分。正=补发，负=扣减。

    这是运营通道，**不涉及任何金额**，也不允许把它做成变相收款。
    """

    amount: int = Field(description="变动积分，正数为发放，负数为扣减")
    remark: str = ""


# ---------------- 行情 ----------------
class QuoteOut(BaseModel):
    code: str
    name: str = ""
    price: float = 0.0
    pre_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: int = 0
    amount: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    turnover_rate: float | None = None
    vol_ratio: float | None = None
    circ_mv: float | None = None
    pe: float | None = None
    pb: float | None = None
    ts: datetime | None = None


class KlineOut(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    amount: float = 0.0
