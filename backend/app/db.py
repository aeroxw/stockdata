"""数据库会话与初始化。

生产用 PostgreSQL，本地冒烟测试可切 SQLite（DATABASE_URL=sqlite:///./stockdata.db）。
注意：分区表是 PG 特性，一期为了跨库兼容先用普通表 + 索引，
后续数据量上来再迁移到分区表。
"""

from __future__ import annotations

import logging
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

log = logging.getLogger("stockdata.db")

_is_sqlite = settings.is_sqlite

engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    # SQLite 不支持连接池参数
    **({} if _is_sqlite else {"pool_size": 10, "max_overflow": 20}),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI 依赖：请求级数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


#: 需要补齐的新列：{表名: [(列名, sqlite DDL, postgres DDL)]}
#: create_all 只会建"不存在的表"，**不会给已存在的表加列**，
#: 所以给旧库升级必须显式 ALTER。开源版暂无需要补的列，留空保持机制可用。
_SCHEMA_UPGRADES: dict[str, list[tuple[str, str, str]]] = {}

#: 开源化改造后遗留的计费结构。项目不再售卖任何东西，这些表和列一并清掉：
#:   orders / balance_logs —— 订单与余额流水（按决策连历史数据一起删）
#:   users.balance / plan_expires_at / is_trial —— 余额、到期时间、试用期标记
#:   plans.price_month / price_year —— 价格（tier 降级为纯配额档位后不再需要）
#: create_all 不会删表删列，所以必须在这里显式 DROP。
_LEGACY_BILLING_TABLES = ["orders", "balance_logs"]
_LEGACY_BILLING_COLUMNS: dict[str, list[str]] = {
    "users": ["balance", "plan_expires_at", "is_trial"],
    "plans": ["price_month", "price_year"],
}


def _ensure_columns() -> None:
    """给已存在的表补列（幂等，可重复执行）。"""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table, cols in _SCHEMA_UPGRADES.items():
        if table not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        with engine.begin() as conn:
            for col, ddl_sqlite, ddl_pg in cols:
                if col in have:
                    continue
                ddl = ddl_sqlite if _is_sqlite else ddl_pg
                try:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {ddl}'))
                    log.info("已为表 %s 补齐列 %s", table, col)
                except Exception as exc:  # noqa: BLE001
                    log.warning("补列 %s.%s 失败（可能已存在）: %s", table, col, exc)


def _drop_legacy_billing() -> None:
    """清理遗留的计费表与列。幂等：不存在就直接跳过，反复执行无害。

    为什么不能靠 alembic 之类的迁移：这套库是 create_all + 手工 ALTER 养出来的，
    没有迁移历史可依。这里直接 DROP，代价是数据不可恢复 —— 这是改造前
    明确确认过的取舍。
    """
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    with engine.begin() as conn:
        for table in _LEGACY_BILLING_TABLES:
            if table not in existing_tables:
                continue
            try:
                conn.execute(text(f'DROP TABLE "{table}"'))
                log.info("已删除遗留表 %s", table)
            except Exception as exc:  # noqa: BLE001
                log.warning("删除遗留表 %s 失败: %s", table, exc)

        for table, cols in _LEGACY_BILLING_COLUMNS.items():
            if table not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for col in cols:
                if col not in have:
                    continue
                try:
                    conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{col}"'))
                    log.info("已删除遗留列 %s.%s", table, col)
                except Exception as exc:  # noqa: BLE001
                    # SQLite 3.35 以下不支持 DROP COLUMN，老库会走到这里。
                    # 模型里已经不引用这些列了，留着也不影响运行，只记告警。
                    log.warning("删除遗留列 %s.%s 失败（可忽略）: %s", table, col, exc)


def init_db(seed: bool = True) -> None:
    """建表 + 补列 + 清理遗留计费结构 + 初始化档位（首次启动或测试用）。

    生产建议用 Alembic 迁移。
    """
    from app import models  # noqa: F401  # 确保模型已注册

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _drop_legacy_billing()
    if seed:
        _seed_plans()


def _seed_plans() -> None:
    """写入三个内置档位。已有的不动，方便后台自定义后不被覆盖。"""
    from app.plans import DEFAULT_PLANS, seed_plans

    session = SessionLocal()
    try:
        seed_plans(session, DEFAULT_PLANS)
    except Exception as exc:  # noqa: BLE001  档位种子失败不该挡住启动
        log.warning("初始化套餐失败: %s", exc)
    finally:
        session.close()
