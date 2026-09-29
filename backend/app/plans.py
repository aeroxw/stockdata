"""配额档位服务。

这里是**配额的唯一真源**：`max_keys / rate_limit / daily_quota` 一律从 plans
表读，代码里不再硬编码 free/pro/vip 三档。后台改了档位，缓存 30 秒内自动失效
（管理员写入时主动清缓存，实际是即时的）。

关于「档位」这个词：
  开源版本不售卖任何东西，所以 plans 里没有价格字段。它只是一组额度配置 ——
  「这个用户能建几个 Key、每分钟限流多少、每天能调多少次」。
  **这层管控不能省**：没有它，任何一个注册账号都能在几分钟内把六家数据源
  撸到集体封 IP，整站就废了。管理员仍然可以在后台给不同用户指派不同档位。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.models import ApiLog, Plan, User

log = logging.getLogger("stockdata.plans")


# ---------------------------------------------------------------- 内置档位
#: 首次启动写入库的三个默认档位。后台可任意增删改。
DEFAULT_PLANS: list[dict] = [
    {
        "code": "free",
        "name": "默认档",
        "level": 0,
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
        "description": "新用户注册即用的默认额度，适合个人开发与功能验证。",
        "is_public": True,
        "sort_order": 1,
    },
    {
        "code": "pro",
        "name": "进阶档",
        "level": 10,
        "max_keys": 10,
        "rate_limit": 300,
        "daily_quota": 50000,
        "features": [
            "包含默认档全部能力",
            "龙虎榜 / 涨停池等特色数据",
            "10 个 API Key",
            "每分钟 300 次调用",
            "每日 5 万次调用",
            "优先数据源切换",
        ],
        "description": "适合量化爱好者与小型团队的日常用量。",
        "is_public": True,
        "sort_order": 2,
    },
    {
        "code": "vip",
        "name": "宽松档",
        "level": 20,
        "max_keys": 50,
        "rate_limit": 1200,
        "daily_quota": -1,
        "features": [
            "包含进阶档全部能力",
            "50 个 API Key",
            "每分钟 1,200 次调用",
            "每日调用不限量",
            "历史数据批量导出",
        ],
        "description": "面向自建部署与高频场景，不限调用量。",
        "is_public": True,
        "sort_order": 3,
    },
]

#: plans 表被清空 / 档位被删时的兜底，保证鉴权链路不会因为查不到档位而 500
FALLBACK_PLAN: dict = {
    "code": "free",
    "name": "默认档",
    "level": 0,
    "max_keys": 3,
    "rate_limit": 60,
    "daily_quota": 1000,
    "features": [],
    "description": "",
}


def seed_plans(db, plans: list[dict] | None = None) -> int:
    """写入默认档位，已存在的跳过（不覆盖后台的自定义改动）。返回新增数量。"""
    added = 0
    for p in plans or DEFAULT_PLANS:
        row = db.scalar(select(Plan).where(Plan.code == p["code"]))
        if row:
            continue
        db.add(Plan(**p))
        added += 1
    if added:
        db.commit()
        log.info("已初始化 %d 个默认档位", added)
    return added


# ---------------------------------------------------------------- 缓存
#: 读多写少，但每次 API 调用都要查配额，所以做一层进程内短缓存
_PLAN_TTL = 30.0
_plan_cache: dict[str, tuple[float, dict]] = {}


def invalidate_plan_cache() -> None:
    """后台增删改档位后调用，让新配额立即生效。"""
    _plan_cache.clear()


def plan_to_dict(p: Plan) -> dict:
    return {
        "code": p.code,
        "name": p.name,
        "level": p.level or 0,
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
    """按 code 取档位，查不到就回落到默认档兜底（绝不返回 None）。"""
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
    """取某档位的三项配额。"""
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


def invalidate_usage_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _usage_cache.clear()
        _usage_delta.clear()
    else:
        _usage_cache.pop(user_id, None)
        _usage_delta.pop(user_id, None)
