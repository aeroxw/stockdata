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
    #: 余额（分）
    balance: int = 0
    plan_expires_at: datetime | None = None
    #: 是否处于新用户试用期
    is_trial: bool = False


# ---------------- APIKey ----------------
class ApiKeyCreate(BaseModel):
    name: str = "default"
    scopes: list[str] = Field(default_factory=lambda: ["quote", "kline", "special"])
    #: 不传则取套餐默认限流
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


# ---------------- 套餐 / 计费 ----------------
class PlanOut(ORMModel):
    id: int | None = None
    code: str
    name: str
    level: int = 0
    price_month: int = 0
    price_year: int = 0
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
    """后台新建套餐。code 用小写字母/数字/下划线，因为它就是 users.tier。"""

    code: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_]{1,31}$")
    name: str = Field(min_length=1, max_length=64)
    level: int = 0
    price_month: int = Field(default=0, ge=0)
    price_year: int = Field(default=0, ge=0)
    max_keys: int = Field(default=3, ge=0)
    rate_limit: int = Field(default=60, ge=1)
    #: -1 = 不限
    daily_quota: int = Field(default=1000, ge=-1)
    features: list[str] = Field(default_factory=list)
    description: str = ""
    is_public: bool = True
    is_active: bool = True
    sort_order: int = 0


class PlanUpdateIn(BaseModel):
    """后台改套餐：只传要改的字段。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    level: int | None = None
    price_month: int | None = Field(default=None, ge=0)
    price_year: int | None = Field(default=None, ge=0)
    max_keys: int | None = Field(default=None, ge=0)
    rate_limit: int | None = Field(default=None, ge=1)
    daily_quota: int | None = Field(default=None, ge=-1)
    features: list[str] | None = None
    description: str | None = None
    is_public: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class RechargeIn(BaseModel):
    """创建充值订单。amount 单位「分」。

    channel：
      mock    —— 模拟支付（仅本地/测试，公网部署会被 ALLOW_MOCK_PAY 闸门拦掉）
      manual  —— 线下转账，等后台确认
      wechat  —— 微信支付 Native 扫码
      alipay  —— 支付宝当面付扫码
    """

    amount: int = Field(gt=0, le=10_000_000, description="充值金额，单位分")
    channel: str = Field(default="mock",
                         pattern="^(mock|manual|wechat|alipay)$")


class BuyPlanIn(BaseModel):
    """用余额购买 / 续费套餐。"""

    plan_code: str = Field(min_length=1, max_length=32)
    period: str = Field(default="month", pattern="^(month|year)$")


class PayIn(BaseModel):
    """模拟支付。真实渠道接入时把 trade_no 换成渠道回调流水号即可。"""

    trade_no: str | None = None


class BalanceAdjustIn(BaseModel):
    """后台手工调整余额。amount 单位「分」，正=赠送，负=扣减。"""

    amount: int = Field(description="变动金额（分），正数为赠送，负数为扣减")
    remark: str = ""


class BalanceLogOut(ORMModel):
    id: int
    user_id: int
    amount: int
    balance_after: int
    type: str
    ref: str | None = None
    remark: str | None = None
    created_at: datetime | None = None


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
