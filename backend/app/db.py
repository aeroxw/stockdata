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
#: 所以给旧库升级必须显式 ALTER。
_SCHEMA_UPGRADES: dict[str, list[tuple[str, str, str]]] = {
    "users": [
        ("balance", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
        ("plan_expires_at", "DATETIME", "TIMESTAMP"),
        #: 新用户试用期标记（见 models.User.is_trial）。存量用户回填 False，
        #: 表示"不是试用"，配合 plan_expires_at=NULL 即长期有效，不会误伤老账号。
        ("is_trial", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
    ],
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
            # 存量数据回填，避免 NULL 破坏"单位分"的整数约定
            if "balance" in {c for c, _, _ in cols}:
                conn.execute(text(f'UPDATE "{table}" SET "balance" = 0 WHERE "balance" IS NULL'))
            #: is_trial 是 NOT NULL 列，理论上 DEFAULT 已经填上了，
            #: 但 SQLite 老版本可能留 NULL，显式回填一次更稳。
            #: **PG 是强类型**：BOOLEAN 列不能写 0（SQLite 才行），必须写 FALSE，
            #: 否则整条 UPDATE 报 `column "is_trial" is of type boolean but expression
            #: is of type integer`。
            if "is_trial" in {c for c, _, _ in cols}:
                false_lit = "0" if _is_sqlite else "FALSE"
                conn.execute(
                    text(f'UPDATE "{table}" SET "is_trial" = {false_lit} WHERE "is_trial" IS NULL')
                )


def init_db(seed: bool = True) -> None:
    """建表 + 补列 + 初始化套餐（首次启动或测试用）。生产建议用 Alembic 迁移。"""
    from app import models  # noqa: F401  # 确保模型已注册

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    if seed:
        _seed_plans()


def _seed_plans() -> None:
    """写入三个内置套餐。已有的不动，方便后台自定义后不被覆盖。"""
    from app.plans import DEFAULT_PLANS, seed_plans

    session = SessionLocal()
    try:
        seed_plans(session, DEFAULT_PLANS)
    except Exception as exc:  # noqa: BLE001  套餐种子失败不该挡住启动
        log.warning("初始化套餐失败: %s", exc)
    finally:
        session.close()
