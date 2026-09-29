"""用户自助计费：套餐浏览 / 充值 / 升级 / 账单 / 用量。

设计原则：
  * 金额**对外一律用「分」的整数**，前端展示时再 /100，杜绝浮点误差；
  * 支付通道可插拔：一期是 mock（点一下即到账）+ manual（线下转账由后台确认），
    接微信/支付宝时只需在 pay.settle_order 外面加一层签名校验；
  * 充值与升级**分开**：充值只加余额，升级从余额扣，账单能对得上。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.models import ApiKey, ApiLog, BalanceLog, Order, User
from app.pay import apply_plan, new_order_no, settle_order
from app.paygate import available_channels, get_gateway, notify_url_for
from app.paygate.base import PayError, PayUnconfigured
from app.plans import (
    daily_usage,
    entitlement,
    get_plan,
    get_plan_row,
    list_plans,
    plan_to_dict,
    yuan,
)
from app.schemas import (
    ApiResponse,
    BuyPlanIn,
    PayIn,
    RechargeIn,
)

router = APIRouter(prefix="/billing", tags=["套餐与充值"])

log = logging.getLogger("stockdata.billing")

#: 充值档位（单位分），前端快捷按钮直接渲染这个列表
RECHARGE_PRESETS = [5000, 10000, 30000, 50000, 100000, 300000]


def _qr_svg(content: str | None) -> str | None:
    """把二维码内容转成 SVG data URI，前端直接 <img src> 就能显示。

    segno 是**可选依赖**：装不上就返回 None，前端退化成显示文本，
    绝不能因为一个二维码库让整个下单接口 500。
    """
    if not content:
        return None
    try:
        import segno

        return segno.make(content).svg_data_uri(scale=6, border=2)
    except Exception as exc:  # noqa: BLE001
        log.warning("二维码生成失败（不影响下单）：%s", exc)
        return None


def _order_out(o: Order) -> dict:
    return {
        "id": o.id,
        "order_no": o.order_no,
        "kind": o.kind,
        "title": o.title,
        "amount": o.amount,
        "amount_yuan": yuan(o.amount or 0),
        "plan_code": o.plan_code,
        "period": o.period,
        "pay_channel": o.pay_channel,
        "status": o.status,
        "trade_no": o.trade_no,
        "remark": o.remark,
        "created_at": o.created_at,
        "updated_at": o.updated_at,
        "paid_at": o.paid_at,
    }


def _bill_out(b: BalanceLog) -> dict:
    labels = {
        "recharge": "充值入账", "consume": "套餐消费", "refund": "退款",
        "grant": "平台赠送", "deduct": "平台扣减",
    }
    return {
        "id": b.id,
        "amount": b.amount,
        "amount_yuan": yuan(b.amount or 0),
        "balance_after": b.balance_after,
        "balance_after_yuan": yuan(b.balance_after or 0),
        "type": b.type,
        "type_label": labels.get(b.type, b.type),
        "ref": b.ref,
        "remark": b.remark,
        "created_at": b.created_at,
    }


# ---------------------------------------------------------------- 套餐
@router.get("/plans", summary="可购买套餐列表")
def public_plans(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """只返回 is_public && is_active 的套餐；隐藏套餐由后台手动指派。"""
    plans = list_plans(db, only_public=True, only_active=True)
    current = get_plan(db, user.tier)
    for p in plans:
        p["is_current"] = p["code"] == user.tier
        #: 免费/低价套餐没有"降级"按钮，只标出是否可升级
        p["can_upgrade"] = p["level"] > current["level"]
        p["price_month_yuan"] = yuan(p["price_month"])
        p["price_year_yuan"] = yuan(p["price_year"])
    return ApiResponse(data={
        "items": plans,
        "current": current,
        "presets": [{"amount": a, "yuan": yuan(a)} for a in RECHARGE_PRESETS],
    })


@router.get("/channels", summary="可用的支付渠道")
def pay_channels(_user: User = Depends(get_current_user)):
    """告诉前端该渲染哪些支付按钮。

    在线渠道只有在**商户参数配齐**时才会出现在列表里 —— 没配就别显示，
    否则用户点下去必然报错，体验比"没有这个选项"差得多。
    线下转账（manual）永远可用，保证站点在任何情况下都能收钱。
    """
    channels = available_channels()
    online = [c for c in channels if c["configured"] and notify_url_for(c["channel"])]
    return ApiResponse(data={
        "items": [
            {"channel": "manual", "label": "线下转账", "configured": True,
             "note": "转账后联系管理员确认到账"},
            *[{**c, "note": ""} for c in online],
        ],
        #: 前端据此提示"在线支付尚未开通"
        "online_ready": bool(online),
    })


@router.get("/me", summary="我的套餐与余额")
def my_plan(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    plan = get_plan(db, user.tier)
    used = daily_usage(db, user.id)
    quota = plan["daily_quota"]
    active_keys = (
        db.query(func.count(ApiKey.id))
        .filter(ApiKey.user_id == user.id, ApiKey.is_active.is_(True))
        .scalar() or 0
    )
    #: 有效期统一从 plans.entitlement 取，前后台和鉴权链路共用同一份判断，
    #: 不会出现"控制台显示还有 3 天、API 却已经 402"这种不一致。
    ent = entitlement(db, user)

    return ApiResponse(data={
        "user_id": user.id,
        "email": user.email,
        "balance": user.balance or 0,
        "balance_yuan": yuan(user.balance or 0),
        "plan": plan,
        "plan_expires_at": ent["expires_at"],
        "days_left": ent["days_left"],
        "expired": ent["expired"],
        "is_trial": ent["is_trial"],
        "entitlement": ent,
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


# ---------------------------------------------------------------- 充值
@router.post("/recharge", summary="创建充值订单")
def create_recharge(payload: RechargeIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    if payload.amount < 100:
        raise HTTPException(400, "最低充值 ¥1.00")

    #: 建单阶段就拦，前端拿不到 pay_url，就不会出现"点一下就到账"的按钮。
    #: 只在建单时拦是不够的（旧订单仍在库里），所以 /pay 那边还有一道闸。
    if payload.channel == "mock" and not settings.ALLOW_MOCK_PAY:
        raise HTTPException(403, "模拟支付通道已关闭，请选择线下转账")

    #: 在线渠道：先探一下网关配置，没配就直接拒，别生成一笔永远付不了的订单
    qr_content = None
    if payload.channel in ("wechat", "alipay"):
        try:
            gw = get_gateway(payload.channel)
        except PayUnconfigured:
            raise HTTPException(503, f"{payload.channel} 渠道未开通")
        if not gw.configured():
            raise HTTPException(503, f"{gw.label}尚未配置商户参数，请改用线下转账")
        notify = notify_url_for(payload.channel)
        if not notify:
            raise HTTPException(
                503,
                f"{gw.label}回调地址未配置（需设置 SITE_BASE_URL 或 "
                f"{'WXPAY' if payload.channel == 'wechat' else 'ALIPAY'}_NOTIFY_URL）",
            )

    for _ in range(5):
        no = new_order_no()
        if not db.scalar(select(Order).where(Order.order_no == no)):
            break
    else:
        raise HTTPException(500, "订单号生成失败，请重试")

    o = Order(
        order_no=no,
        user_id=user.id,
        kind="recharge",
        title=f"账户充值 ¥{yuan(payload.amount)}",
        amount=payload.amount,
        pay_channel=payload.channel,
        status="pending",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(o)
    db.commit()
    db.refresh(o)

    #: 在线渠道：真正去渠道下单拿二维码。这一步放在**订单已落库之后**，
    #: 万一渠道超时，库里仍留着一笔 pending 单，用户可以重试或改线下转账，
    #: 不会出现"钱付了但平台没订单"这种最糟糕的情况。
    pay_url = None
    if payload.channel in ("wechat", "alipay"):
        try:
            result = gw.create_payment(
                order_no=no,
                subject=f"{settings.APP_NAME} 账户充值",
                amount_cents=payload.amount,
                notify_url=notify_url_for(payload.channel),
            )
            qr_content = result.get("qr_content")
        except PayError as exc:
            log.warning("渠道 %s 下单失败：%s", payload.channel, exc)
            raise HTTPException(502, f"{gw.label}下单失败：{exc}")
    elif payload.channel == "mock":
        pay_url = f"/api/v1/billing/orders/{no}/pay"

    return ApiResponse(
        msg=("请使用微信或支付宝扫码支付" if qr_content else "订单已创建，请在 30 分钟内完成支付"),
        data={
            **_order_out(o),
            #: mock 通道给前端一个"立即支付"入口；manual 通道提示等待后台确认
            "pay_url": pay_url,
            #: 在线渠道返回二维码：qr_svg 直接可 <img src>，
            #: qr_content 留着给"复制到剪贴板 / 唤起 App"用
            "qr_content": qr_content,
            "qr_svg": _qr_svg(qr_content),
            "expires_in": 1800,
        },
    )


@router.post("/orders/{order_no}/pay", summary="支付订单（模拟支付）")
def pay_order(order_no: str, payload: PayIn | None = None,
              db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """一期内置模拟支付：点一下立即到账。

    真实渠道接入时，这里换成"唤起收银台"，由渠道回调调 settle_order 入账，
    余额与流水的写入逻辑完全复用，不用改。
    """
    o = db.scalar(select(Order).where(Order.order_no == order_no, Order.user_id == user.id))
    if not o:
        raise HTTPException(404, "订单不存在")
    if o.kind != "recharge":
        raise HTTPException(400, "该订单不是充值订单")
    if o.status in ("cancelled", "refunded"):
        raise HTTPException(400, "订单已关闭，无法支付")
    #: 自助支付闸门（这里踩过两次坑，两条路都能白嫖，必须一起堵）：
    #:
    #: 1) mock 通道：公网部署时 ALLOW_MOCK_PAY=false。余额能直接买套餐，
    #:    开着它等于任何人一点就领到 VIP 配额。
    #: 2) manual 通道：本意是"线下转账，等后台在 /admin/orders/{no}/confirm
    #:    确认到账"，但原先 /pay 对**任何渠道**的订单都放行 —— 用户拿到
    #:    自己的 manual 单号直接 POST /pay 就能给自己入账 100 元，
    #:    后台确认形同虚设。
    #:
    #: 所以这里改成白名单：只有 mock 允许自助支付，其余一律走各自渠道的
    #: 入账路径（manual 等后台确认，未来微信/支付宝等回调）。
    if o.pay_channel != "mock":
        raise HTTPException(400, "该订单需线下转账，请等待后台确认到账")
    if not settings.ALLOW_MOCK_PAY:
        raise HTTPException(403, "模拟支付通道已关闭，请使用线下转账或在线支付")

    if o.status == "paid":
        #: 幂等：重复支付不报错也不重复入账。返回体带上余额，
        #: 保证"首次支付"和"重复支付"两种响应的字段一致，客户端不用分支处理。
        db.refresh(user)
        return ApiResponse(
            msg="订单已支付，未重复扣款",
            data={**_order_out(o), "balance": user.balance,
                  "balance_yuan": yuan(user.balance or 0)},
        )

    ok, msg = settle_order(db, o, trade_no=(payload.trade_no if payload else None))
    if not ok:
        raise HTTPException(400, msg)
    db.refresh(o)
    db.refresh(user)
    return ApiResponse(
        msg=f"支付成功，余额 ¥{yuan(user.balance or 0)}",
        data={**_order_out(o), "balance": user.balance, "balance_yuan": yuan(user.balance or 0)},
    )


@router.post("/orders/{order_no}/cancel", summary="取消待支付订单")
def cancel_my_order(order_no: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    o = db.scalar(select(Order).where(Order.order_no == order_no, Order.user_id == user.id))
    if not o:
        raise HTTPException(404, "订单不存在")
    if o.status != "pending":
        raise HTTPException(400, f"订单当前状态为 {o.status}，无法取消")
    o.status = "cancelled"
    o.updated_at = datetime.utcnow()
    db.commit()
    return ApiResponse(msg="订单已取消", data=_order_out(o))


@router.get("/orders", summary="我的订单")
def my_orders(
    kind: str = Query("all", pattern="^(all|recharge|upgrade|renew|downgrade)$"),
    status: str = Query("all", pattern="^(all|pending|paid|cancelled|failed|refunded)$"),
    offset: int = 0,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Order).filter(Order.user_id == user.id)
    if kind != "all":
        query = query.filter(Order.kind == kind)
    if status != "all":
        query = query.filter(Order.status == status)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = query.order_by(Order.created_at.desc()).offset(offset).limit(limit).all()
    return ApiResponse(data={
        "total": total, "offset": offset, "limit": limit,
        "items": [_order_out(r) for r in rows],
    })


# ---------------------------------------------------------------- 升级
@router.post("/buy", summary="用余额购买/续费套餐")
def buy_plan(payload: BuyPlanIn, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    row = get_plan_row(db, payload.plan_code)
    if not row:
        raise HTTPException(404, "套餐不存在")
    if not row.is_active:
        raise HTTPException(400, "该套餐已下架，请选择其他套餐")
    plan = plan_to_dict(row)
    period = payload.period
    price = plan["price_year"] if period == "year" else plan["price_month"]

    current = get_plan(db, user.tier)
    if current["code"] == plan["code"]:
        kind = "renew"
    elif plan["level"] > current["level"]:
        kind = "upgrade"
    else:
        kind = "downgrade"

    #: 免费套餐价格是 0，直接生效，不走扣款
    if price <= 0:
        apply_plan(db, user, plan, period)
        o = Order(
            order_no=new_order_no("UP"),
            user_id=user.id,
            kind=kind,
            title=f"切换到「{plan['name']}」",
            amount=0,
            plan_code=plan["code"],
            period=period,
            pay_channel="balance",
            status="paid",
            paid_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(o)
        db.commit()
        return ApiResponse(
            msg=f"已切换到「{plan['name']}」",
            data={"order": _order_out(o), "balance": user.balance,
                  "balance_yuan": yuan(user.balance or 0)},
        )

    balance = user.balance or 0
    if balance < price:
        raise HTTPException(
            400,
            f"余额不足：套餐「{plan['name']}」{_period_label(period)}需 ¥{yuan(price)}，"
            f"当前余额 ¥{yuan(balance)}，还需充值 ¥{yuan(price - balance)}",
        )

    new_balance = balance - price
    user.balance = new_balance
    expires = apply_plan(db, user, plan, period)

    o = Order(
        order_no=new_order_no("UP"),
        user_id=user.id,
        kind=kind,
        title=f"「{plan['name']}」{_period_label(period)}",
        amount=price,
        plan_code=plan["code"],
        period=period,
        pay_channel="balance",
        status="paid",
        paid_at=datetime.utcnow(),
        remark=f"到期时间 {expires.strftime('%Y-%m-%d')}" if expires else None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(o)
    db.add(BalanceLog(
        user_id=user.id,
        amount=-price,
        balance_after=new_balance,
        type="consume",
        ref=o.order_no,
        remark=f"购买「{plan['name']}」{_period_label(period)}",
        created_at=datetime.utcnow(),
    ))
    db.commit()
    db.refresh(o)

    return ApiResponse(
        msg=f"已{'续费' if kind == 'renew' else '升级到'}「{plan['name']}」，"
            f"扣款 ¥{yuan(price)}，到期 {expires.strftime('%Y-%m-%d') if expires else '长期有效'}",
        data={"order": _order_out(o), "balance": new_balance,
              "balance_yuan": yuan(new_balance)},
    )


def _period_label(period: str) -> str:
    return "年度" if period == "year" else "月度"


# ---------------------------------------------------------------- 账单
@router.get("/bills", summary="我的余额流水")
def my_bills(
    type: str = Query("all", pattern="^(all|recharge|consume|refund|grant|deduct)$"),
    offset: int = 0,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(BalanceLog).filter(BalanceLog.user_id == user.id)
    if type != "all":
        query = query.filter(BalanceLog.type == type)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = query.order_by(BalanceLog.created_at.desc()).offset(offset).limit(limit).all()

    income = (
        db.query(func.sum(BalanceLog.amount))
        .filter(BalanceLog.user_id == user.id, BalanceLog.amount > 0)
        .scalar() or 0
    )
    outcome = (
        db.query(func.sum(BalanceLog.amount))
        .filter(BalanceLog.user_id == user.id, BalanceLog.amount < 0)
        .scalar() or 0
    )
    return ApiResponse(data={
        "total": total, "offset": offset, "limit": limit,
        "income": income, "income_yuan": yuan(income),
        "outcome": abs(outcome), "outcome_yuan": yuan(abs(outcome)),
        "items": [_bill_out(r) for r in rows],
    })


# ---------------------------------------------------------------- 用量
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
