"""后台管理（需管理员权限）。

对外提供管理控制台需要的全部数据：
  - /admin/stats   总览指标（含 24h 趋势、错误率、延迟分位、Top 接口、系统信息）
  - /admin/users   用户列表（搜索 / 档位 / 状态 筛选 + 分页）
  - /admin/keys    全量 API Key（筛选 + 分页）
  - /admin/logs    调用日志（筛选 + 分页）
  - /admin/plans   档位管理（新建 / 改配额 / 上下架 / 删除）
  - 各类写操作：指派档位、启禁用、重置密码、删除用户、吊销/删除密钥

开源版本不售卖任何东西，所以这里没有订单、账单、余额相关接口。
「档位（plan）」只是配额档位，不是商品。

注意：**统计全部走 SQL 聚合，不在 Python 里全表遍历**，
只有分位数（P95）这种 SQL 方言差异大的才拉回内存算，并设了上限保护。
"""

from __future__ import annotations

import platform
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import settings
from app.db import get_db
from app.deps import get_admin
from app.models import ApiKey, ApiLog, Plan, StockMeta, User
from app.plans import (
    DEFAULT_PLANS,
    FALLBACK_PLAN,
    get_plan,
    get_plan_row,
    invalidate_plan_cache,
    invalidate_usage_cache,
    list_plans,
)
from app.ratelimit import blacklist_key, redis_status
from app.schemas import (
    ApiResponse,
    PlanIn,
    PlanUpdateIn,
)

router = APIRouter(prefix="/admin", tags=["管理后台"])

#: 进程启动时刻，用于计算运行时长
_STARTED_AT = time.time()

#: 兜底档位代码。库里档位被删光时至少还有 free 可用。
TIERS = tuple(p["code"] for p in DEFAULT_PLANS) or ("free",)


# ---------------------------------------------------------------- 工具
def _paginate(db: Session, query, offset: int, limit: int) -> tuple[int, list]:
    """先算总数再取当页。

    用 subquery + count 而不是 query.count()，后者是 SQLAlchemy 1.x 遗留写法，
    带上 join/filter 时容易算出错误的总数。
    """
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = query.offset(offset).limit(limit).all()
    return total, rows


def _key_counts(db: Session) -> dict[int, int]:
    """每个用户的活跃密钥数。"""
    return dict(
        db.query(ApiKey.user_id, func.count(ApiKey.id))
        .filter(ApiKey.is_active.is_(True))
        .group_by(ApiKey.user_id)
        .all()
    )


def _calls_by_user(db: Session, since: datetime) -> dict[int, int]:
    return dict(
        db.query(ApiLog.user_id, func.count(ApiLog.id))
        .filter(ApiLog.ts >= since, ApiLog.user_id.isnot(None))
        .group_by(ApiLog.user_id)
        .all()
    )


def _plan_map(db: Session) -> dict[str, dict]:
    """code -> 档位配置。查不到时补一个兜底，避免展示层 KeyError。"""
    m = {p["code"]: p for p in list_plans(db)}
    m.setdefault(FALLBACK_PLAN["code"], dict(FALLBACK_PLAN))
    return m


def _user_row(u: User, kc: dict[int, int], calls: dict[int, int],
              plans: dict[str, dict] | None = None) -> dict:
    p = (plans or {}).get(u.tier) or {"name": u.tier, "max_keys": 3}
    return {
        "id": u.id,
        "email": u.email,
        "tier": u.tier,
        "tier_name": p.get("name", u.tier),
        "is_admin": u.is_admin,
        "is_active": u.is_active,
        "created_at": u.created_at,
        "last_login": u.last_login,
        "active_keys": kc.get(u.id, 0),
        "key_quota": p.get("max_keys", 3),
        "calls_24h": calls.get(u.id, 0),
    }


def _key_row(k: ApiKey, email: str) -> dict:
    return {
        "id": k.id,
        "user_id": k.user_id,
        "user_email": email,
        "name": k.name,
        "masked": f"{k.key_prefix}****",
        "scopes": k.scopes or [],
        "rate_limit": k.rate_limit,
        "is_active": k.is_active,
        "total_calls": k.total_calls or 0,
        "last_used": k.last_used,
        "created_at": k.created_at,
        "expires_at": k.expires_at,
    }


# ---------------------------------------------------------------- 总览
@router.get("/stats", summary="系统总览")
def stats(db: Session = Depends(get_db), _u=Depends(get_admin)):
    now = datetime.utcnow()
    d1 = now - timedelta(days=1)
    d2 = now - timedelta(days=2)
    d7 = now - timedelta(days=7)

    # ---- 用户
    users = db.query(func.count(User.id)).scalar() or 0
    users_7d = (
        db.query(func.count(User.id)).filter(User.created_at >= d7).scalar() or 0
    )
    active_7d = (
        db.query(func.count(User.id)).filter(User.last_login >= d7).scalar() or 0
    )
    disabled = (
        db.query(func.count(User.id)).filter(User.is_active.is_(False)).scalar() or 0
    )
    plans = list_plans(db)
    #: 档位是后台可自定义的，分布字典必须按库里的档位动态建，不能写死三档
    tiers_meta = [
        {"code": p["code"], "name": p["name"], "count": 0, "level": p["level"]}
        for p in sorted(plans, key=lambda x: (x["sort_order"], x["level"]))
    ]
    tier_counts = {p["code"]: 0 for p in plans}
    tier_rows = db.query(User.tier, func.count(User.id)).group_by(User.tier).all()
    for t, c in tier_rows:
        tier_counts[t] = tier_counts.get(t, 0) + c
    for item in tiers_meta:
        item["count"] = tier_counts.get(item["code"], 0)

    # ---- 密钥
    keys = db.query(func.count(ApiKey.id)).scalar() or 0
    active_keys = (
        db.query(func.count(ApiKey.id)).filter(ApiKey.is_active.is_(True)).scalar() or 0
    )

    # ---- 调用量：今日 vs 昨日（用于环比）
    calls_24h = db.query(func.count(ApiLog.id)).filter(ApiLog.ts >= d1).scalar() or 0
    calls_prev = (
        db.query(func.count(ApiLog.id))
        .filter(ApiLog.ts >= d2, ApiLog.ts < d1)
        .scalar() or 0
    )
    if calls_prev:
        calls_delta = round((calls_24h - calls_prev) / calls_prev * 100, 1)
    else:
        calls_delta = None if calls_24h == 0 else 100.0

    # ---- 错误率 / 延迟
    errors_24h = (
        db.query(func.count(ApiLog.id))
        .filter(ApiLog.ts >= d1, ApiLog.status >= 400)
        .scalar() or 0
    )
    avg_lat = (
        db.query(func.avg(ApiLog.latency_ms)).filter(ApiLog.ts >= d1).scalar() or 0
    )
    # P95：SQL 方言差异大，拉回内存算，最多取 5 万条防止大表拖垮接口
    lats = [
        x[0]
        for x in db.query(ApiLog.latency_ms)
        .filter(ApiLog.ts >= d1, ApiLog.latency_ms.isnot(None))
        .order_by(ApiLog.latency_ms)
        .limit(50000)
        .all()
    ]
    p95 = 0
    if lats:
        lats.sort()
        p95 = lats[min(int(len(lats) * 0.95), len(lats) - 1)]

    # ---- 24 小时趋势（按整点分桶）
    hour_start = (now - timedelta(hours=23)).replace(
        minute=0, second=0, microsecond=0
    )
    buckets = [
        {"hour": (hour_start + timedelta(hours=i)).strftime("%H:00"), "total": 0, "errors": 0}
        for i in range(24)
    ]
    rows = (
        db.query(ApiLog.ts, ApiLog.status)
        .filter(ApiLog.ts >= hour_start)
        .all()
    )
    for ts, st in rows:
        idx = int((ts - hour_start).total_seconds() // 3600)
        if 0 <= idx < 24:
            buckets[idx]["total"] += 1
            if st >= 400:
                buckets[idx]["errors"] += 1

    # ---- Top 接口
    top_paths = [
        {"path": p, "count": c}
        for p, c in (
            db.query(ApiLog.path, func.count(ApiLog.id))
            .filter(ApiLog.ts >= d1)
            .group_by(ApiLog.path)
            .order_by(func.count(ApiLog.id).desc())
            .limit(8)
            .all()
        )
    ]

    # ---- 状态码分布
    # 注意：不要用 func.iif / CASE WHEN 这类方言相关写法，SQLite 与 PG 行为不一致，
    # 这里拆成三次简单 count，跨库都稳。
    status_dist = {"s2xx": 0, "s4xx": 0, "s5xx": 0}
    status_dist["s2xx"] = (
        db.query(func.count(ApiLog.id))
        .filter(ApiLog.ts >= d1, ApiLog.status < 400)
        .scalar() or 0
    )
    status_dist["s4xx"] = (
        db.query(func.count(ApiLog.id))
        .filter(ApiLog.ts >= d1, ApiLog.status >= 400, ApiLog.status < 500)
        .scalar() or 0
    )
    status_dist["s5xx"] = (
        db.query(func.count(ApiLog.id))
        .filter(ApiLog.ts >= d1, ApiLog.status >= 500)
        .scalar() or 0
    )

    # ---- 数据资产
    stock_meta = db.query(func.count(StockMeta.id)).scalar() or 0

    return ApiResponse(data={
        "users": users,
        "users_new_7d": users_7d,
        "users_active_7d": active_7d,
        "users_disabled": disabled,
        "tier_counts": tier_counts,
        "tiers": tiers_meta,
        "api_keys": keys,
        "active_keys": active_keys,
        "revoked_keys": keys - active_keys,
        "calls_24h": calls_24h,
        "calls_prev_24h": calls_prev,
        "calls_delta_pct": calls_delta,
        "errors_24h": errors_24h,
        "error_rate": round(errors_24h / calls_24h * 100, 2) if calls_24h else 0.0,
        "avg_latency_ms": round(float(avg_lat), 1),
        "p95_latency_ms": int(p95),
        "hourly": buckets,
        "top_paths": top_paths,
        "status_dist": status_dist,
        "stock_meta": stock_meta,
        "system": {
            "app": settings.APP_NAME,
            "debug": settings.DEBUG,
            "env": "生产" if not settings.DEBUG else "开发",
            "database": "SQLite" if settings.is_sqlite else "PostgreSQL",
            "redis": redis_status(),
            "python": platform.python_version(),
            "platform": platform.system(),
            "uptime_s": int(time.time() - _STARTED_AT),
        },
    })


@router.post("/redis/recheck", summary="强制重新探测 Redis 连接")
def redis_recheck(_u=Depends(get_admin)):
    """后台「重新检测」按钮用。

    正常情况不用点：探测失败后每 30 秒会自动重试一次，
    Redis 起来了自己就接上了。这里只是给「想立刻确认」的场景用。
    """
    st = redis_status(force=True)
    return ApiResponse(
        msg="Redis 已连接" if st["available"] else "仍未连接（限流与缓存继续降级）",
        data=st,
    )


# ---------------------------------------------------------------- 用户
@router.get("/users", summary="用户列表（支持搜索/筛选/分页）")
def list_users(
    q: str | None = None,
    tier: str | None = None,
    status: str = Query("all", pattern="^(all|active|disabled)$"),
    offset: int = 0,
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _u=Depends(get_admin),
):
    query = db.query(User)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(User.email.ilike(like), User.id == _as_int(q)))
    #: 档位是后台自定义的，这里不写死枚举，传什么过滤什么
    if tier and tier != "all":
        query = query.filter(User.tier == tier)
    if status == "active":
        query = query.filter(User.is_active.is_(True))
    elif status == "disabled":
        query = query.filter(User.is_active.is_(False))

    total, rows = _paginate(db, query.order_by(User.created_at.desc()), offset, limit)
    kc = _key_counts(db)
    calls = _calls_by_user(db, datetime.utcnow() - timedelta(days=1))
    plans = _plan_map(db)
    return ApiResponse(data={
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_user_row(r, kc, calls, plans) for r in rows],
    })


def _as_int(s: str) -> int:
    """搜索框里输入数字时按 ID 精确匹配，非数字返回 -1（不可能命中）。"""
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return -1


@router.get("/users/{user_id}", summary="用户详情（含密钥与最近调用）")
def user_detail(user_id: int, db: Session = Depends(get_db), _u=Depends(get_admin)):
    u = db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(404, "用户不存在")

    keys = db.query(ApiKey).filter(ApiKey.user_id == u.id).order_by(
        ApiKey.created_at.desc()
    ).all()
    logs = db.query(ApiLog).filter(ApiLog.user_id == u.id).order_by(
        ApiLog.ts.desc()
    ).limit(20).all()
    calls_24h = (
        db.query(func.count(ApiLog.id))
        .filter(ApiLog.user_id == u.id, ApiLog.ts >= datetime.utcnow() - timedelta(days=1))
        .scalar() or 0
    )
    calls_total = (
        db.query(func.count(ApiLog.id)).filter(ApiLog.user_id == u.id).scalar() or 0
    )

    return ApiResponse(data={
        "user": _user_row(u, _key_counts(db), {}, _plan_map(db)),
        "calls_24h": calls_24h,
        "calls_total": calls_total,
        "keys": [_key_row(k, u.email) for k in keys],
        "recent_logs": [{
            "id": l.id, "path": l.path, "status": l.status,
            "latency_ms": l.latency_ms, "ts": l.ts,
        } for l in logs],
    })


@router.post("/users/{user_id}/toggle", summary="启用/禁用用户")
def toggle_user(user_id: int, db: Session = Depends(get_db), _u=Depends(get_admin)):
    u = db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(404, "用户不存在")
    # 不允许把自己禁用 —— 否则后台直接锁死，只能改数据库救回来
    if u.id == _u.id and u.is_active:
        raise HTTPException(400, "不能禁用自己，避免后台被锁死")
    u.is_active = not u.is_active
    db.commit()
    return ApiResponse(msg="ok", data={"id": user_id, "is_active": u.is_active})


class TierIn(BaseModel):
    #: 档位代码可以是后台自定义的任意 code，所以只校验长度与字符集
    tier: str = Field(min_length=1, max_length=32)


@router.post("/users/{user_id}/tier", summary="调整用户配额档位")
def set_tier(user_id: int, payload: TierIn | None = None, tier: str | None = None,
             db: Session = Depends(get_db), _u=Depends(get_admin)):
    """同时兼容 body（{"tier":"pro"}）与 query（?tier=pro）两种调用方式。

    开源版本没有买卖，这里的语义是**管理员给这个用户指派一档额度**，
    而不是"卖套餐"。
    """
    value = (payload.tier if payload else None) or tier
    if not value:
        raise HTTPException(400, "缺少档位代码")
    u = db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(404, "用户不存在")
    plan = get_plan(db, value)
    if not get_plan_row(db, value):
        raise HTTPException(400, f"档位 {value} 不存在，请到「档位管理」先创建")

    u.tier = value
    db.commit()
    invalidate_usage_cache(user_id)
    return ApiResponse(
        msg=f"已调整为「{plan['name']}」（Key 上限 {plan['max_keys']}，"
            f"限流 {plan['rate_limit']}/分钟，日配额 "
            f"{'不限' if plan['daily_quota'] < 0 else plan['daily_quota']}）",
        data={"id": user_id, "tier": value, "tier_name": plan["name"]},
    )


class PasswordIn(BaseModel):
    password: str = Field(min_length=8, max_length=128)


@router.post("/users/{user_id}/password", summary="重置用户密码")
def reset_password(user_id: int, payload: PasswordIn,
                   db: Session = Depends(get_db), _u=Depends(get_admin)):
    u = db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(404, "用户不存在")
    u.password_hash = hash_password(payload.password)
    db.commit()
    # 该用户现有的密钥保持不变，但把其登录态踢掉更安全 —— 这里只重置密码，
    # JWT 无法单点失效（无状态），所以提示前端告知用户重新登录。
    return ApiResponse(msg="密码已重置，该用户需重新登录", data={"id": user_id})


@router.delete("/users/{user_id}", summary="删除用户及其密钥")
def delete_user(user_id: int, db: Session = Depends(get_db), _u=Depends(get_admin)):
    u = db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(404, "用户不存在")
    if u.id == _u.id:
        raise HTTPException(400, "不能删除自己")
    if u.is_admin:
        other_admins = (
            db.query(func.count(User.id))
            .filter(User.is_admin.is_(True), User.id != u.id)
            .scalar() or 0
        )
        if other_admins == 0:
            raise HTTPException(400, "这是最后一个管理员，不能删除")

    n_keys = db.query(func.count(ApiKey.id)).filter(ApiKey.user_id == u.id).scalar() or 0
    db.query(ApiKey).filter(ApiKey.user_id == u.id).delete(synchronize_session=False)
    db.delete(u)
    db.commit()
    # api_logs 保留（调用审计需要），只是 user_id 变成孤儿引用
    return ApiResponse(
        msg="用户已删除",
        data={"id": user_id, "deleted_keys": n_keys,
              "deleted_orders": n_orders, "deleted_bills": n_bills},
    )


# ---------------------------------------------------------------- 密钥
@router.get("/keys", summary="全部 API Key（支持搜索/筛选/分页）")
def list_all_keys(
    q: str | None = None,
    status: str = Query("all", pattern="^(all|active|revoked)$"),
    user_id: int | None = None,
    offset: int = 0,
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _u=Depends(get_admin),
):
    query = db.query(ApiKey)
    if status == "active":
        query = query.filter(ApiKey.is_active.is_(True))
    elif status == "revoked":
        query = query.filter(ApiKey.is_active.is_(False))
    if user_id:
        query = query.filter(ApiKey.user_id == user_id)

    if q:
        like = f"%{q.strip()}%"
        # 允许按密钥名、前缀、或归属者邮箱搜索
        owner_ids = [
            uid for (uid,) in db.query(User.id).filter(User.email.ilike(like)).all()
        ]
        # 注意别写 `or_(..., False)` —— SQLAlchemy 2.0 会把字面量 False 当成一个
        # 布尔列处理，PG 上直接报错。没有匹配用户时就不加这一支条件。
        conds = [
            ApiKey.name.ilike(like),
            ApiKey.key_prefix.ilike(like),
            ApiKey.id == _as_int(q),
        ]
        if owner_ids:
            conds.append(ApiKey.user_id.in_(owner_ids))
        query = query.filter(or_(*conds))

    total, rows = _paginate(db, query.order_by(ApiKey.created_at.desc()), offset, limit)
    emails = {
        u.id: u.email for u in db.query(User).filter(
            User.id.in_([r.user_id for r in rows])
        ).all()
    } if rows else {}
    return ApiResponse(data={
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_key_row(r, emails.get(r.user_id, "-")) for r in rows],
    })


@router.post("/keys/{key_id}/revoke", summary="吊销任意用户的密钥")
def admin_revoke_key(key_id: int, db: Session = Depends(get_db),
                     _u=Depends(get_admin)):
    k = db.scalar(select(ApiKey).where(ApiKey.id == key_id))
    if not k:
        raise HTTPException(404, "API Key 不存在")
    if not k.is_active:
        return ApiResponse(msg="该密钥已是吊销状态", data={"id": key_id})
    k.is_active = False
    db.commit()
    blacklist_key(k.key_hash)   # 即时生效，无需等缓存过期
    return ApiResponse(msg="已吊销", data={"id": key_id})


@router.delete("/keys/{key_id}", summary="永久删除已吊销的密钥")
def admin_purge_key(key_id: int, db: Session = Depends(get_db),
                    _u=Depends(get_admin)):
    k = db.scalar(select(ApiKey).where(ApiKey.id == key_id))
    if not k:
        raise HTTPException(404, "API Key 不存在")
    if k.is_active:
        raise HTTPException(400, "只能删除已吊销的密钥，请先吊销再删除")
    db.delete(k)
    db.commit()
    return ApiResponse(msg="已永久删除", data={"id": key_id})


# ---------------------------------------------------------------- 档位
@router.get("/plans", summary="全部档位（含隐藏档位）")
def list_all_plans(db: Session = Depends(get_db), _u=Depends(get_admin)):
    plans = list_plans(db)
    #: 统计每个档位下有多少用户，删除前给个提示
    used = dict(db.query(User.tier, func.count(User.id)).group_by(User.tier).all())
    for p in plans:
        p["users"] = used.get(p["code"], 0)
    return ApiResponse(data={"items": plans, "total": len(plans)})


@router.post("/plans", summary="新建档位")
def create_plan(payload: PlanIn, db: Session = Depends(get_db), _u=Depends(get_admin)):
    if get_plan_row(db, payload.code):
        raise HTTPException(400, f"档位代码 {payload.code} 已存在")
    p = Plan(
        **payload.model_dump(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(p)
    db.commit()
    invalidate_plan_cache()
    return ApiResponse(msg=f"档位「{p.name}」已创建", data={"code": p.code})


@router.put("/plans/{code}", summary="修改档位")
def update_plan(code: str, payload: PlanUpdateIn,
                db: Session = Depends(get_db), _u=Depends(get_admin)):
    p = get_plan_row(db, code)
    if not p:
        raise HTTPException(404, "档位不存在")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    p.updated_at = datetime.utcnow()
    db.commit()
    invalidate_plan_cache()
    return ApiResponse(
        msg=f"档位「{p.name}」已更新，配额即时生效",
        data={"code": p.code, "max_keys": p.max_keys,
              "rate_limit": p.rate_limit, "daily_quota": p.daily_quota},
    )


@router.delete("/plans/{code}", summary="删除档位")
def delete_plan(code: str, db: Session = Depends(get_db), _u=Depends(get_admin)):
    p = get_plan_row(db, code)
    if not p:
        raise HTTPException(404, "档位不存在")
    if code in ("free",):
        raise HTTPException(400, "free 是注册默认档位，不能删除（可改为停用）")
    n = db.query(func.count(User.id)).filter(User.tier == code).scalar() or 0
    if n:
        raise HTTPException(
            400, f"还有 {n} 个用户在使用该档位，请先把他们迁移到其他档位再删除"
        )
    db.delete(p)
    db.commit()
    invalidate_plan_cache()
    return ApiResponse(msg=f"档位「{p.name}」已删除", data={"code": code})


# ---------------------------------------------------------------- 日志
@router.get("/logs", summary="调用日志（支持筛选/分页）")
def logs(
    q: str | None = None,
    status: str = Query("all", pattern="^(all|ok|err)$"),
    key_id: int | None = None,
    user_id: int | None = None,
    offset: int = 0,
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
    _u=Depends(get_admin),
):
    query = db.query(ApiLog)
    if status == "ok":
        query = query.filter(ApiLog.status < 400)
    elif status == "err":
        query = query.filter(ApiLog.status >= 400)
    if key_id:
        query = query.filter(ApiLog.key_id == key_id)
    if user_id:
        query = query.filter(ApiLog.user_id == user_id)
    if q:
        query = query.filter(ApiLog.path.ilike(f"%{q.strip()}%"))

    total, rows = _paginate(db, query.order_by(ApiLog.ts.desc()), offset, limit)
    emails = {}
    if rows:
        uids = {r.user_id for r in rows if r.user_id}
        if uids:
            emails = {u.id: u.email for u in db.query(User).filter(User.id.in_(uids)).all()}
    masks = {}
    if rows:
        kids = {r.key_id for r in rows if r.key_id}
        if kids:
            masks = {
                k.id: f"{k.key_prefix}****"
                for k in db.query(ApiKey).filter(ApiKey.id.in_(kids)).all()
            }

    return ApiResponse(data={
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [{
            "id": r.id, "path": r.path, "status": r.status,
            "latency_ms": r.latency_ms, "ts": r.ts,
            "key_id": r.key_id, "key_masked": masks.get(r.key_id),
            "user_id": r.user_id, "user_email": emails.get(r.user_id),
        } for r in rows],
    })
