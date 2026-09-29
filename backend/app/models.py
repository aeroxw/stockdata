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
    #: 配额档位代码，对应 plans.code（free / pro / vip / 后台自定义档位）
    #:
    #: 注意：tier **不是商品**，只是运营用的配额档位。项目开源后不售卖任何套餐，
    #: 它唯一的作用是决定这个用户能建几个 Key、每分钟限流多少、每天能调多少次。
    tier: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    #: 管理员后台权限
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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


# ---------------------------------------------------------------- 配额
class Plan(Base):
    """配额档位。后台可自由增删改，用户的 tier 即 plans.code。

    设计要点：
      * 开源版本**不卖任何东西**，所以这里没有价格字段 —— 它只是一组
        「能建几个 Key / 每分钟限流多少 / 每天能调多少次」的额度配置；
      * level 决定档位高低，不靠 code 字符串比较；
      * max_keys / rate_limit / daily_quota 三档配额**从这张表读**，
        不再散落在代码里硬编码（否则后台改了档位却不生效）；
      * is_public=false 可做"隐藏档位"，只由后台手动指派给指定用户。
    """

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 档位代码，与 users.tier 一一对应
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 层级：数字越大档位越高
    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    #: 可创建的活跃 API Key 上限
    max_keys: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    #: 单 Key 每分钟限流
    rate_limit: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    #: 每日调用配额，-1 表示不限
    daily_quota: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    #: 卖点列表，前端直接渲染成勾选项
    features: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    #: false = 隐藏档位，不在用户端展示，仅后台指派
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 停用后不允许再指派给新用户
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


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
