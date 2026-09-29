"""套餐与配额服务。

这里是**配额的唯一真源**：`max_keys / rate_limit / daily_quota` 一律从
plans 表读，代码里不再硬编码 free/pro/vip 三档。
后台改了套餐，缓存 30 秒内自动失效（管理员写入时主动清缓存，实际是即时的）。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select

from app.config import settings
from app.models import ApiLog, Plan, User

log = logging.getLogger("stockdata.plans")


# ---------------------------------------------------------------- 内置套餐
#: 首次启动写入库的三个默认套餐。后台可任意增删改。
DEFAULT_PLANS: list[dict] = [
    {
        "code": "free",
        "name": "免费版",
        "level": 0,
        "price_month": 0,
        "price_year": 0,
        "max_keys": 3,
        "rate_limit": 60,
        "daily_quota": 1000,
        "features": [
            "实时行情快照（全市场）",
            "日/周/月 K 线",
            "3 个 API Key",
            "每分钟 60 次调用",
            "每日 1,000 次调用",
        ],
        "description": "适合个人开发与功能验证，无需付费即可接入。",
        "is_public": True,
        "sort_order": 1,
    },
    {
        "code": "pro",
        "name": "专业版",
        "level": 10,
        "price_month": 9900,      # ¥99/月
        "price_year": 99000,      # ¥990/年（买 10 送 2）
        "max_keys": 10,
        "rate_limit": 300,
        "daily_quota": 50000,
        "features": [
            "包含免费版全部能力",
            "龙虎榜 / 涨停池等特色数据",
            "10 个 API Key",
            "每分钟 300 次调用",
            "每日 5 万次调用",
            "优先数据源切换",
        ],
        "description": "适合量化爱好者与小型团队的主力档位。",
        "is_public": True,
        "sort_order": 2,
    },
    {
        "code": "vip",
        "name": "旗舰版",
        "level": 20,
        "price_month": 29900,     # ¥299/月
        "price_year": 299000,     # ¥2,990/年
        "max_keys": 50,
        "rate_limit": 1200,
        "daily_quota": -1,
        "features": [
            "包含专业版全部能力",
            "50 个 API Key",
            "每分钟 1,200 次调用",
            "每日调用不限量",
            "历史数据批量导出",
            "专属技术支持",
        ],
        "description": "面向机构与高频场景，不限调用量。",
        "is_public": True,
        "sort_order": 3,
    },
]

#: plans 表被清空 / 套餐被删时的兜底，保证鉴权链路不会因为查不到套餐而 500
FALLBACK_PLAN: dict = {
    "code": "free",
    "name": "免费版",
    "level": 0,
    "price_month": 0,
    "price_year": 0,
    "max_keys": 3,
    "rate_limit": 60,
    "daily_quota": 1000,
    "features": [],
    "description": "",
}


def seed_plans(db, plans: list[dict] | None = None) -> int:
    """写入默认套餐，已存在的跳过（不覆盖后台的自定义改动）。返回新增数量。"""
    added = 0
    for p in plans or DEFAULT_PLANS:
        row = db.scalar(select(Plan).where(Plan.code == p["code"]))
        if row:
            continue
        db.add(Plan(**p))
        added += 1
    if added:
        db.commit()
        log.info("已初始化 %d 个默认套餐", added)
    return added


# ---------------------------------------------------------------- 缓存
#: 套餐读多写少，但每次 API 调用都要查配额，所以做一层进程内短缓存
_PLAN_TTL = 30.0
_plan_cache: dict[str, tuple[float, dict]] = {}


def invalidate_plan_cache() -> None:
    """后台增删改套餐后调用，让新配额立即生效。"""
    _plan_cache.clear()


def plan_to_dict(p: Plan) -> dict:
    return {
        "code": p.code,
        "name": p.name,
        "level": p.level or 0,
        "price_month": p.price_month or 0,
        "price_year": p.price_year or 0,
        "max_keys": p.max_keys if p.max_keys is not None else 3,
        "rate_limit": p.rate_limit or 60,
        "daily_quota": p.daily_quota if p.daily_quota is not None else 1000,
        "features": list(p.features or []),
        "description": p.description or "",
        "is_public": bool(p.is_public),
        "is_active": bool(p.is_active),
        "sort_order": p.sort_order or 0,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def get_plan_row(db, code: str) -> Plan | None:
    return db.scalar(select(Plan).where(Plan.code == code))


def get_plan(db, code: str) -> dict:
    """按 code 取套餐，查不到就回落到免费版兜底（绝不返回 None）。"""
    now = time.time()
    hit = _plan_cache.get(code)
    if hit and now - hit[0] < _PLAN_TTL:
        return hit[1]

    row = get_plan_row(db, code)
    data = plan_to_dict(row) if row else dict(FALLBACK_PLAN)
    _plan_cache[code] = (now, data)
    return data


def list_plans(db, only_public: bool = False, only_active: bool = False) -> list[dict]:
    q = select(Plan)
    if only_public:
        q = q.where(Plan.is_public.is_(True))
    if only_active:
        q = q.where(Plan.is_active.is_(True))
    rows = db.scalars(q.order_by(Plan.sort_order, Plan.level, Plan.id)).all()
    return [plan_to_dict(r) for r in rows]


def quota_for(db, code: str) -> dict:
    """取某套餐的三项配额。"""
    p = get_plan(db, code)
    return {
        "max_keys": p["max_keys"],
        "rate_limit": p["rate_limit"],
        "daily_quota": p["daily_quota"],
    }


# ---------------------------------------------------------------- 用量
_USAGE_TTL = 30.0
_usage_cache: dict[int, tuple[float, int]] = {}
#: 缓存命中期间本地累加的调用次数，避免"缓存里是旧值"导致配额形同虚设
_usage_delta: dict[int, int] = {}


def daily_usage(db, user_id: int) -> int:
    """近 24 小时调用次数（带短缓存 + 本地增量补偿）。"""
    now = time.time()
    cached = _usage_cache.get(user_id)
    if cached and now - cached[0] < _USAGE_TTL:
        return cached[1] + _usage_delta.get(user_id, 0)

    since = datetime.utcnow() - timedelta(hours=24)
    base = db.scalar(
        select(func.count()).select_from(ApiLog).where(
            ApiLog.user_id == user_id, ApiLog.ts >= since
        )
    ) or 0
    _usage_cache[user_id] = (now, base)
    _usage_delta[user_id] = 0
    return base


def bump_usage(user_id: int) -> None:
    """鉴权通过后调用：本地计数 +1，让配额在下一次落库前也生效。"""
    _usage_delta[user_id] = _usage_delta.get(user_id, 0) + 1


def check_daily_quota(db, user: User) -> tuple[bool, int, int]:
    """返回 (是否放行, 已用次数, 配额)。配额 -1 表示不限。"""
    plan = get_plan(db, user.tier)
    quota = plan["daily_quota"]
    if quota < 0:
        return True, daily_usage(db, user.id), -1
    used = daily_usage(db, user.id)
    return used < quota, used, quota


# ---------------------------------------------------------------- 权益/有效期
def entitlement(db, user: User) -> dict:
    """算清楚用户「现在到底有什么权益」，前后台和各种判断都读这一份。

    返回：
        expired     是否已过期（到期时间已过）
        is_trial    是否处于新用户试用期（含已过期的试用）
        days_left   剩余天数（None = 长期有效）
        expires_at  到期时间
        blocked     是否应当停止 API 调用
        reason      被拦截的原因（给前端展示）
    """
    now = datetime.utcnow()
    exp = user.plan_expires_at
    is_trial = bool(user.is_trial)

    if not exp:
        return {
            "expired": False, "is_trial": is_trial, "days_left": None,
            "expires_at": None, "blocked": False, "reason": "",
            "label": "长期有效",
        }

    expired = exp < now
    #: 用 max(0, ...) —— 直接 (exp-now).days 对负数会向下取整（-0.5 天 -> -1），
    #: 显示成"还剩 -1 天"很怪。
    days_left = max(0, (exp - now).days)

    blocked = False
    reason = ""
    if expired:
        blocked = bool(settings.TRIAL_EXPIRE_BLOCK)
        #: 管理员豁免：否则哪天管理员自己账号过期，连后台都进不去，
        #: 只能去数据库里改，纯属给自己找麻烦。
        if user.is_admin:
            blocked = False
        if blocked:
            reason = ("新用户试用期已结束，请购买套餐后继续使用"
                      if is_trial else "套餐已到期，请续费后继续使用")

    return {
        "expired": expired,
        "is_trial": is_trial,
        "days_left": days_left,
        "expires_at": exp,
        "blocked": blocked,
        "reason": reason,
        "label": (f"试用剩余 {days_left} 天" if is_trial and not expired
                  else (f"{days_left} 天后到期" if not expired else "已到期")),
    }


def enforce_entitlement(user: User) -> None:
    """到期拦截：试用到期 / 套餐过期后停止调用数据接口（HTTP 402）。

    只在**数据接口**的鉴权处调用（deps.authenticate_*），
    /auth 与 /billing 不走这里 —— 否则用户过期后连充值续费的入口都进不去，
    等于把人锁死在外面。
    """
    st = entitlement(None, user)
    if st["blocked"]:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            st["reason"] or "套餐已到期，请续费后继续使用",
        )


def invalidate_usage_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _usage_cache.clear()
        _usage_delta.clear()
    else:
        _usage_cache.pop(user_id, None)
        _usage_delta.pop(user_id, None)


# ---------------------------------------------------------------- 金额工具
def yuan(cents: int) -> str:
    """分 -> 元，保留两位。"""
    return f"{(cents or 0) / 100.0:.2f}"


def yuan_f(cents: int) -> float:
    return round((cents or 0) / 100.0, 2)


def to_cents(yuan_value: float) -> int:
    """元 -> 分，四舍五入，防止 19.9*100=1989.9999 这种误差。"""
    return int(round(float(yuan_value) * 100))
