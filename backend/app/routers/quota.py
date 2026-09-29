"""配额与用量：当前档位、三项额度、调用统计。

原来的 billing.py 负责"充值 / 买套餐 / 账单"，开源化改造后整套售卖功能已移除，
这里只留下与计费无关的、**运营上必需**的三个接口：

  * 档位列表    —— 后台可自定义，前端控制台展示用
  * 我的配额    —— max_keys / rate_limit / daily_quota 三项额度及已用情况
  * 用量统计    —— 近 N 天调用曲线、错误率、Top 路径

注意「档位（plan）」这个词虽然沿用至今，但它**只是配额档位，不是商品**。
项目不售卖任何东西，管理员仍然需要靠它给不同用户配不同的额度 ——
没有这层管控，一个注册账号就能把六家数据源撸到集体封 IP。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import ApiKey, ApiLog, User
from app.plans import daily_usage, get_plan, list_plans
from app.points import settle_expired
from app.schemas import ApiResponse

router = APIRouter(prefix="/quota", tags=["配额"])


@router.get("/plans", summary="配额档位列表")
def public_plans(db: Session = Depends(get_db)):
    """只返回 is_public && is_active 的档位；隐藏档位由后台手动指派。"""
    plans = list_plans(db, only_public=True, only_active=True)
    return ApiResponse(data={"items": plans})


@router.get("/me", summary="我的配额与用量")
def my_plan(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """当前档位的三项额度 + 已用情况。

    这三项是限流链路的唯一真源：鉴权处读的是同一份 plan，
    所以控制台看到的数字和实际被拦下的时机永远一致。
    """
    #: 先结算到期：否则会出现"控制台显示还有 3 天、接口已经按免费档限流"。
    #: 和 deps.py 鉴权链用的是同一个函数、同一份判断，两边永远一致。
    settle_expired(db, user)

    plan = get_plan(db, user.tier)
    used = daily_usage(db, user.id)
    quota = plan["daily_quota"]
    active_keys = (
        db.query(func.count(ApiKey.id))
        .filter(ApiKey.user_id == user.id, ApiKey.is_active.is_(True))
        .scalar() or 0
    )

    exp = user.plan_expires_at
    return ApiResponse(data={
        "user_id": user.id,
        "email": user.email,
        "tier": user.tier,
        "plan": plan,
        #: 积分与到期信息一并给出，控制台一次请求就能渲染完整
        "points": user.points or 0,
        "plan_expires_at": exp,
        "plan_days_left": (exp - datetime.utcnow()).days if exp else None,
        "quota": {
            "max_keys": plan["max_keys"],
            "rate_limit": plan["rate_limit"],
            "daily_quota": quota,
            "unlimited": quota < 0,
        },
        "usage": {
            "active_keys": active_keys,
            "calls_24h": used,
            "daily_quota": quota,
            "unlimited": quota < 0,
            "pct": 0 if quota < 0 else (round(used / quota * 100, 1) if quota else 0),
        },
    })


@router.get("/usage", summary="我的用量统计")
def my_usage(days: int = Query(14, ge=1, le=60),
             db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    now = datetime.utcnow()
    since = now - timedelta(days=days)
    plan = get_plan(db, user.tier)
    quota = plan["daily_quota"]

    rows = (
        db.query(ApiLog.ts, ApiLog.status)
        .filter(ApiLog.user_id == user.id, ApiLog.ts >= since)
        .all()
    )
    daily: dict[str, dict] = {}
    for i in range(days):
        d = (now - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        daily[d] = {"date": d, "total": 0, "errors": 0}
    for ts, st in rows:
        key = ts.strftime("%Y-%m-%d")
        if key in daily:
            daily[key]["total"] += 1
            if st >= 400:
                daily[key]["errors"] += 1

    calls_24h = (
        db.query(func.count(ApiLog.id))
        .filter(ApiLog.user_id == user.id, ApiLog.ts >= now - timedelta(days=1))
        .scalar() or 0
    )
    calls_7d = (
        db.query(func.count(ApiLog.id))
        .filter(ApiLog.user_id == user.id, ApiLog.ts >= now - timedelta(days=7))
        .scalar() or 0
    )
    calls_total = (
        db.query(func.count(ApiLog.id))
        .filter(ApiLog.user_id == user.id)
        .scalar() or 0
    )
    errors_24h = (
        db.query(func.count(ApiLog.id))
        .filter(ApiLog.user_id == user.id, ApiLog.ts >= now - timedelta(days=1),
                ApiLog.status >= 400)
        .scalar() or 0
    )
    top_paths = [
        {"path": p, "count": c}
        for p, c in (
            db.query(ApiLog.path, func.count(ApiLog.id))
            .filter(ApiLog.user_id == user.id, ApiLog.ts >= since)
            .group_by(ApiLog.path)
            .order_by(func.count(ApiLog.id).desc())
            .limit(6)
            .all()
        )
    ]

    return ApiResponse(data={
        "plan": plan,
        "daily": list(daily.values()),
        "calls_24h": calls_24h,
        "calls_7d": calls_7d,
        "calls_total": calls_total,
        "errors_24h": errors_24h,
        "error_rate": round(errors_24h / calls_24h * 100, 2) if calls_24h else 0.0,
        "top_paths": top_paths,
        "quota": {
            "daily_quota": quota,
            "unlimited": quota < 0,
            "used": calls_24h,
            "pct": 0 if quota < 0 else (round(calls_24h / quota * 100, 1) if quota else 0),
        },
    })
