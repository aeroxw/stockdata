"""数据库模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 套餐代码，对应 plans.code（free / pro / vip / 后台自定义套餐）
    tier: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    #: 管理员后台权限
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 账户余额。**单位「分」**（整数存储，杜绝浮点误差）
    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 当前套餐到期时间；NULL = 长期有效（免费版或永久授权）
    #:
    #: 试用期内这个字段同样有效 —— 新用户注册即拿到 15 天体验期，
    #: 到期时间就记在这里，和付费套餐共用一套判断逻辑，不用两处判断。
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 是否处于**新用户试用期**。
    #:
    #: 单独存一列（而不是靠 tier 猜）的原因：试用套餐和付费套餐可能是同一个
    #: code（比如试用给 pro、买的也是 pro），只看 tier 分不清"体验中"还是"已付费"，
    #: 而两者的到期策略不同 —— 试用到期直接停 API，付费到期才走续费提醒。
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    @property
    def balance_yuan(self) -> float:
        return round((self.balance or 0) / 100.0, 2)


class ApiKey(Base):
    """API 密钥。

    安全设计：**只存哈希**，明文仅在创建成功时返回一次。
    key_prefix 单独存一列用于界面展示（sk_live_xxxx****xxxx）。
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="default")
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 权限范围
    scopes: Mapped[list] = mapped_column(JSON, default=lambda: ["quote", "kline"])
    #: 每分钟限流
    rate_limit: Mapped[int] = mapped_column(Integer, default=60)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_used: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 累计调用次数
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QuoteRow(Base):
    """行情快照。数据量增长快，生产建议按月分区。"""

    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(32))
    price: Mapped[float | None] = mapped_column(Float)
    pre_close: Mapped[float | None] = mapped_column(Float)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    #: 必须是 BigInteger。指数/大盘股的成交量轻松超过 int32 上限（21.5 亿），
    #: 例如深证成指单日 volume 约 236 亿 —— 用 Integer 在 PostgreSQL 上会直接
    #: `NumericValueOutOfRange: integer out of range`，整批写入失败。
    #: SQLite 是动态类型所以本地永远测不出来，只有上了 PG 才暴露。
    volume: Mapped[int | None] = mapped_column(BigInteger)
    amount: Mapped[float | None] = mapped_column(Float)
    change_pct: Mapped[float | None] = mapped_column(Float)
    turnover_rate: Mapped[float | None] = mapped_column(Float)
    vol_ratio: Mapped[float | None] = mapped_column(Float)
    circ_mv: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(String(16))


class KlineRow(Base):
    __tablename__ = "klines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    period: Mapped[str] = mapped_column(String(8), default="day", nullable=False)
    adjust: Mapped[str] = mapped_column(String(4), default="qfq", nullable=False)
    date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(BigInteger)   # 同上，指数会超 int32
    amount: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(String(16))


class SpecialData(Base):
    """特色数据（龙虎榜 / 涨停池等）。各源字段差异大，用 JSON 存明细最灵活。"""

    __tablename__ = "special_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: dragon_tiger / limit_up / limit_down / limit_break / limit_up_ladder ...
    kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StockMeta(Base):
    """股票代码 - 名称映射。"""

    __tablename__ = "stock_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(32), default="")
    exchange: Mapped[str | None] = mapped_column(String(8))
    asset_type: Mapped[str | None] = mapped_column(String(24))
    list_date: Mapped[str | None] = mapped_column(String(10))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------- 计费
class Plan(Base):
    """套餐。后台可自由增删改（"定制套餐版本"），用户的 tier 即 plans.code。

    设计要点：
      * 价格一律存**分**（Integer），展示时再除以 100，避免 0.1+0.2 那种经典误差；
      * level 决定升级/降级方向，不靠 code 字符串比较；
      * max_keys / rate_limit / daily_quota 三档配额**从这张表读**，
        不再散落在代码里硬编码（否则后台改了套餐却不生效）。
      * is_public=false 可做"隐藏套餐"，只由后台手动指派给指定用户。
    """

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 套餐代码，与 users.tier 一一对应
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 层级：数字越大套餐越高，用于判断升级 / 降级
    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    #: 月度价格（分）
    price_month: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 年度价格（分）；通常 = price_month * 10（买 10 送 2）
    price_year: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 可创建的活跃 API Key 上限
    max_keys: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    #: 单 Key 每分钟限流
    rate_limit: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    #: 每日调用配额，-1 表示不限
    daily_quota: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    #: 卖点列表，前端直接渲染成勾选项
    features: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    #: false = 隐藏套餐，不在用户端展示，仅后台指派
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 停用后不允许新购/升级，已购用户保留权益
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Order(Base):
    """订单：充值 / 套餐升级 / 续费。

    kind:
      recharge —— 纯充值，支付成功后金额进余额；
      upgrade  —— 用余额购买套餐（不下单支付，直接扣余额，也记一笔订单便于对账）；
      renew    —— 续费当前套餐。
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="recharge", nullable=False)
    title: Mapped[str] = mapped_column(String(128), default="")
    #: 金额（分）
    amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: upgrade / renew 时的目标套餐
    plan_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: month / year
    period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: mock / manual / wechat / alipay
    pay_channel: Mapped[str] = mapped_column(String(16), default="mock", nullable=False)
    #: pending / paid / failed / cancelled / refunded
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    #: 支付渠道流水号
    trade_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BalanceLog(Base):
    """余额流水（账单）。只读追加，不做物理删除，保证可审计。"""

    __tablename__ = "balance_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    #: 变动金额（分）：正 = 入账，负 = 出账
    amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 变动后的余额（分），便于逐笔核对
    balance_after: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: recharge / consume / refund / grant / deduct
    type: Mapped[str] = mapped_column(String(24), default="recharge", nullable=False, index=True)
    #: 关联订单号
    ref: Mapped[str | None] = mapped_column(String(32), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ApiLog(Base):
    """调用日志。按天清理，避免无限膨胀。"""

    __tablename__ = "api_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_id: Mapped[int | None] = mapped_column(Integer, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    path: Mapped[str] = mapped_column(String(128))
    status: Mapped[int] = mapped_column(Integer, default=200)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
