"""支付入账核心。

**余额与流水只允许在这里写入** —— 用户自助支付、后台手工确认到账两条路
都调用同一个 settle_order，避免出现"订单已支付但余额没加"这种经典不一致。

接真实支付渠道（微信/支付宝）时只需替换两处：
  1. create_order 之后把 pay_url 返回给前端去唤起收银台；
  2. 渠道回调里校验签名后调用 settle_order(...)，其余逻辑不用动。
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import BalanceLog, Order, User
from app.plans import invalidate_plan_cache, invalidate_usage_cache, yuan

log = logging.getLogger("stockdata.pay")


def new_order_no(prefix: str = "RC") -> str:
    """生成订单号：RC + 日期 + 8 位随机串，便于人工报单时口头核对。"""
    day = datetime.utcnow().strftime("%Y%m%d")
    alphabet = string.ascii_uppercase + string.digits
    rand = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{prefix}{day}{rand}"


def apply_plan(db, user: User, plan: dict, period: str) -> datetime | None:
    """把套餐生效到用户身上，返回新的到期时间（免费版返回 None = 长期有效）。

    续费规则：同一套餐未到期时续费，到期时间在**原到期日基础上顺延**，
    不会把用户已买的剩余天数吞掉。
    """
    now = datetime.utcnow()
    if plan["price_month"] == 0 and plan["price_year"] == 0:
        user.tier = plan["code"]
        user.plan_expires_at = None
        #: 切到免费版要清掉试用标记 —— 否则"试用中"的标签会一直挂着。
        user.is_trial = False
        return None

    base = now
    if user.tier == plan["code"] and user.plan_expires_at and user.plan_expires_at > now:
        base = user.plan_expires_at
    expires = base + timedelta(days=365 if period == "year" else 30)

    user.tier = plan["code"]
    user.plan_expires_at = expires
    #: 只要真正买过一次，就不再是"新用户试用"了，到期策略按付费用户走。
    user.is_trial = False
    invalidate_usage_cache(user.id)
    invalidate_plan_cache()
    return expires


def settle_order(db, order: Order, trade_no: str | None = None) -> tuple[bool, str]:
    """订单入账。返回 (是否成功, 说明)。

    recharge —— 金额进余额，写一条充值流水；
    upgrade / renew —— 下单时已扣款并生效，这里只补状态（幂等）。
    """
    if order.status == "paid":
        return False, "订单已支付，请勿重复入账"
    if order.status in ("cancelled", "refunded"):
        return False, f"订单状态为「{order.status}」，无法入账"

    user = db.scalar(select(User).where(User.id == order.user_id))
    if not user:
        return False, "订单关联的用户不存在"

    order.status = "paid"
    order.paid_at = datetime.utcnow()
    order.trade_no = trade_no or order.trade_no or f"mock-{secrets.token_hex(6)}"
    order.updated_at = datetime.utcnow()

    if order.kind == "recharge":
        new_balance = (user.balance or 0) + (order.amount or 0)
        user.balance = new_balance
        db.add(BalanceLog(
            user_id=user.id,
            amount=order.amount or 0,
            balance_after=new_balance,
            type="recharge",
            ref=order.order_no,
            remark=f"账户充值 ¥{yuan(order.amount or 0)}（{order.pay_channel}）",
            created_at=datetime.utcnow(),
        ))

    db.commit()
    log.info("订单 %s 已入账：user=%s amount=%s", order.order_no, user.email, order.amount)
    return True, "ok"
