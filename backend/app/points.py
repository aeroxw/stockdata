"""签到积分与档位兑换。

这套机制为什么存在
------------------
**保护上游六家数据源**，而不是赚钱。

项目不售卖任何东西：用户不用花钱，也**不能**花钱买任何东西。
更高额度唯一的获取方式是「每天来签到一次」——让配额流向真正持续使用的人，
而不是注册完就甩个脚本、几分钟把六家源撸到集体封 IP 的人。
15 天连续签到才换一个月专业版，"门槛"是耐心，不是钱。

合规边界（改动前务必先读 models.User.points 那段注释）
----------------------------------------------------
  * 积分**只能**签到获得，不可购买 —— 一旦能充值买积分，整套机制的法律
    性质就从"无偿运营手段"变成"有偿信息服务"，要 ICP 许可证；
  * 积分不可转让、赠送、交易 —— 能转让就形成二级市场，反过来证明它有财产价值；
  * 档位到期后**回落到免费版继续服务**，不停用账号、不拒绝调用 ——
    停服就变成付费墙特征了。

这三条是底线，不是建议。这里的任何一个改动都应该先回头对照一遍。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from app.models import PointLog, User
from app.plans import get_plan, list_plans

log = logging.getLogger("stockdata.points")

#: 默认档 code —— 不可兑换，也是到期后的回落目标
FREE_PLAN = "free"

#: 每次签到发放的积分
CHECKIN_POINTS = 1

#: 中国只有一个时区且 1991 年后不再实行夏令时，所以固定 +8 偏移即可，
#: 不需要 zoneinfo。
#: **别改成 ZoneInfo("Asia/Shanghai")**：Windows 和 alpine 基础镜像默认都没有
#: tz 数据库，会直接抛 ZoneInfoNotFoundError —— 而且只在容器里才炸，
#: 本地开发机有 tzdata 时一切正常，属于典型的"本地好线上挂"。
CN_OFFSET = timedelta(hours=8)


def today() -> date:
    """当前自然日（北京时间）。签到的粒度就是"一天一次"。"""
    return (datetime.utcnow() + CN_OFFSET).date()


# ---------------------------------------------------------------- 到期回落
def settle_expired(db, user: User) -> bool:
    """档位到期就把用户降回免费版。返回是否真的降了。

    **刻意不做任何拒绝**：配额可以变小，服务不能断。
    用户到期后仍然能用，只是额度回到免费版那一档。
    """
    if not user.plan_expires_at or user.tier == FREE_PLAN:
        return False
    if user.plan_expires_at > datetime.utcnow():
        return False

    old = user.tier
    user.tier = FREE_PLAN
    user.plan_expires_at = None
    db.commit()
    log.info("用户 %s 的档位 %s 已到期，回落 %s（服务不中断）", user.id, old, FREE_PLAN)
    return True


# ---------------------------------------------------------------- 签到
def checkin(db, user: User) -> dict:
    """每日签到。同一自然日重复调用返回 already=True，不会重复给分。

    幂等是关键：前端按钮没做禁用时用户会狂点，如果每次都加分，
    签到就直接变成刷分入口了。
    """
    d = today()
    if user.last_checkin_date == d:
        return {
            "already": True,
            "gained": 0,
            "points": user.points or 0,
            "date": d.isoformat(),
        }

    user.points = (user.points or 0) + CHECKIN_POINTS
    user.points_earned = (user.points_earned or 0) + CHECKIN_POINTS
    user.last_checkin_date = d
    _log(db, user.id, "checkin", CHECKIN_POINTS, user.points, remark="每日签到")
    db.commit()

    return {
        "already": False,
        "gained": CHECKIN_POINTS,
        "points": user.points,
        "date": d.isoformat(),
    }


def can_checkin(user: User) -> bool:
    """今天还能不能签到。UI 用它决定按钮是亮着还是禁用。"""
    return user.last_checkin_date != today()


# ---------------------------------------------------------------- 兑换
def redeem_options(db) -> list[dict]:
    """对外可兑换的档位（公开 + 启用 + 配了兑换价）。按 level 升序。

    免费档的 redeem_points 是 None，会被自然过滤掉。
    """
    plans = list_plans(db, only_public=True, only_active=True)
    opts = [p for p in plans if (p.get("redeem_points") or 0) > 0]
    return sorted(opts, key=lambda p: (p.get("level") or 0, p.get("code")))


def redeem(db, user: User, plan_code: str) -> dict:
    """用积分兑换档位。

    规则很简单：
      * 同档再兑换 = 续期，在剩余天数上累加，不会亏掉已攒的天数；
      * 换成更高的档 = 从今天起重新算一个月；
      * 不允许降级兑换，没意义。
    """
    plan = get_plan(db, plan_code)
    cost = plan.get("redeem_points") or 0
    if cost <= 0:
        raise ValueError(f"档位「{plan['name']}」不支持积分兑换")

    cur = get_plan(db, user.tier)
    if (plan.get("level") or 0) < (cur.get("level") or 0):
        raise ValueError(
            f"当前已是更高的「{cur['name']}」，无需降级兑换。"
            f"等它到期或联系管理员调整"
        )

    have = user.points or 0
    if have < cost:
        raise ValueError(f"积分不足：需要 {cost} 分，当前 {have} 分")

    days = plan.get("redeem_days") or 30

    #: 同档续期 -> 在现有到期日上顺延；换档 -> 从现在起算。
    #: 顺延很重要：否则用户在还剩 20 天时续期，等于白扔了 20 天，
    #: 没人敢在到期前兑换。
    if user.tier == plan["code"] and user.plan_expires_at:
        base = max(user.plan_expires_at, datetime.utcnow())
        expires = base + timedelta(days=days)
        action = "续期"
    else:
        expires = datetime.utcnow() + timedelta(days=days)
        action = "兑换"

    user.points = have - cost
    user.tier = plan["code"]
    user.plan_expires_at = expires

    _log(db, user.id, "redeem", -cost, user.points,
         plan_code=plan["code"], expires_at=expires,
         remark=f"{action}「{plan['name']}」 {days} 天")
    db.commit()

    return {
        "plan": plan,
        "cost": cost,
        "days": days,
        "action": action,
        "expires_at": expires,
        "points_left": user.points,
    }


# ---------------------------------------------------------------- 管理员调整
def admin_adjust(db, user: User, delta: int, remark: str = "") -> dict:
    """管理员手动调整积分（补发 / 扣减）。

    这是运营通道，**不是收款通道** —— 不牵扯任何金额。
    """
    if not delta:
        raise ValueError("变动值不能为 0")
    if delta < 0 and (user.points or 0) + delta < 0:
        raise ValueError("扣减后积分不能为负")

    user.points = (user.points or 0) + delta
    if delta > 0:
        user.points_earned = (user.points_earned or 0) + delta
    _log(db, user.id, "grant" if delta > 0 else "deduct", delta, user.points,
         remark=remark or ("管理员发放" if delta > 0 else "管理员扣减"))
    db.commit()
    return {"points": user.points, "delta": delta}


# ---------------------------------------------------------------- 查询
def summary(db, user: User) -> dict:
    """控制台用的一份概览：余额、签到状态、可兑换项（含差额与进度）。"""
    settle_expired(db, user)
    cur = get_plan(db, user.tier)
    points = user.points or 0

    opts = []
    for p in redeem_options(db):
        cost = p["redeem_points"]
        opts.append({
            **p,
            "affordable": points >= cost,
            "lack": max(0, cost - points),
            #: 还差几天签到能攒够 —— 比干巴巴的"差 8 分"更直观
            "days_needed": max(0, -(-(cost - points) // CHECKIN_POINTS)),
            "progress": min(100, round(points / cost * 100, 1)) if cost else 100,
            "is_current": p["code"] == user.tier,
        })

    exp = user.plan_expires_at
    return {
        "points": points,
        "points_earned": user.points_earned or 0,
        "can_checkin": can_checkin(user),
        "last_checkin_date": user.last_checkin_date.isoformat()
        if user.last_checkin_date else None,
        "checkin_points": CHECKIN_POINTS,
        "tier": user.tier,
        "plan": cur,
        "plan_expires_at": exp,
        "plan_days_left": (exp.date() - today()).days if exp else None,
        "options": opts,
    }


def recent_logs(db, user_id: int, limit: int = 20) -> list[PointLog]:
    from sqlalchemy import select
    return list(db.scalars(
        select(PointLog).where(PointLog.user_id == user_id)
        .order_by(PointLog.id.desc()).limit(limit)
    ).all())


# ---------------------------------------------------------------- 内部
def _log(db, user_id: int, type_: str, delta: int, balance: int,
         plan_code: str | None = None, expires_at: datetime | None = None,
         remark: str | None = None) -> None:
    db.add(PointLog(
        user_id=user_id,
        type=type_,
        delta=delta,
        balance_after=balance,
        plan_code=plan_code,
        expires_at=expires_at,
        remark=remark,
    ))
