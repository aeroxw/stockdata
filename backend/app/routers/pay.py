"""支付回调（微信 / 支付宝）。

这里**没有鉴权** —— 回调是渠道服务器主动 POST 过来的，带不了我们的 JWT。
安全性完全依赖「验签」，验签在各自网关的 parse_notify 里做。

三条铁律：
  1. **先验签再信数据**。回调地址公网可达，任何人都能 POST，不验签就入账等于白送。
  2. **入账必须幂等**。渠道在应答失败/超时时会重发（微信最多 15 次、支付宝会持续重试），
     所以同一个订单被回调 N 次只能入账一次。settle_order 内部已按 status 挡住，
     但这里仍然显式处理，做到"看代码就知道是幂等的"。
  3. **必须核对金额**。回调里的 amount 要和订单 amount 对得上，
     防止被篡改成"付 1 分钱买 100 元"。

另外：**应答格式错了渠道会一直重发**。微信要 JSON {"code":"SUCCESS"}，
支付宝要纯文本 "success"，混用会导致渠道反复重发，日志里全是重复回调。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Order
from app.pay import settle_order
from app.paygate import get_gateway
from app.paygate.base import PayError, PayUnconfigured

router = APIRouter(prefix="/pay", tags=["支付回调"])

log = logging.getLogger("stockdata.pay.notify")


@router.post("/notify/{channel}", summary="支付渠道异步回调")
async def notify(channel: str, request: Request,
                 db: Session = Depends(get_db)) -> Response:
    #: 未知渠道直接 404 —— 不静默接受，免得别人拿个假渠道名来探接口
    try:
        gw = get_gateway(channel)
    except PayUnconfigured:
        log.warning("收到未知支付渠道的回调：%s", channel)
        return Response(content="unknown channel", status_code=404)

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    # ---------- 1. 验签 + 解析 ----------
    try:
        result = gw.parse_notify(headers, body)
    except PayError as exc:
        #: 验签失败要记 warning（可能是有人在伪造回调），但**不回 fail**。
        #: 回 fail 会让渠道重发，而验签失败重发一万次也还是失败；
        #: 真正需要重发的是"我们这边临时故障"，那种情况走下面的 except。
        log.warning("[%s] 回调验签失败：%s", channel, exc)
        ctype, text = gw.reply_fail(str(exc))
        return Response(content=text, media_type=ctype, status_code=200)

    order_no = result.get("order_no") or ""
    log.info("[%s] 收到回调 order=%s trade=%s success=%s",
             channel, order_no, result.get("trade_no"), result.get("success"))

    # ---------- 2. 找订单 ----------
    order = db.scalar(select(Order).where(Order.order_no == order_no))
    if not order:
        log.error("[%s] 回调中的订单不存在：%s", channel, order_no)
        #: 订单不存在，回 fail 让渠道重发也没用（还是找不到），
        #: 但回 success 更糟 —— 渠道以为我们收到了，钱就丢了。
        #: 这里回 fail 是为了让渠道留下重试记录，同时也进告警日志便于人工对账。
        ctype, text = gw.reply_fail("order not found")
        return Response(content=text, media_type=ctype, status_code=200)

    # ---------- 3. 幂等：已支付直接回成功 ----------
    if order.status == "paid":
        log.info("[%s] 订单 %s 已支付，忽略重复回调", channel, order_no)
        ctype, text = gw.reply_success()
        return Response(content=text, media_type=ctype, status_code=200)

    if order.status in ("cancelled", "refunded"):
        log.warning("[%s] 订单 %s 状态为 %s，无法入账", channel, order.order_no, order.status)
        ctype, text = gw.reply_success()   # 不让它无限重发
        return Response(content=text, media_type=ctype, status_code=200)

    # ---------- 4. 未成功支付 ----------
    if not result.get("success"):
        log.info("[%s] 订单 %s 支付未完成，忽略", channel, order_no)
        ctype, text = gw.reply_success()
        return Response(content=text, media_type=ctype, status_code=200)

    # ---------- 5. 金额核对 ----------
    paid = int(result.get("amount") or 0)
    if paid != int(order.amount or 0):
        #: 金额对不上**绝对不能入账**。记 error 等人工介入，
        #: 同时回 fail —— 这属于异常情况，需要留下痕迹。
        log.error(
            "[%s] 订单 %s 金额不符：渠道 %s 分，订单 %s 分，拒绝入账",
            channel, order_no, paid, order.amount,
        )
        ctype, text = gw.reply_fail("amount mismatch")
        return Response(content=text, media_type=ctype, status_code=200)

    # ---------- 6. 入账 ----------
    try:
        ok, msg = settle_order(db, order, trade_no=result.get("trade_no"))
    except Exception as exc:  # noqa: BLE001
        #: 我们自己的故障（DB 抖动等）—— 回 fail 让渠道稍后重发，
        #: 这才是重试机制真正该兜底的场景。
        log.exception("[%s] 订单 %s 入账异常：%s", channel, order_no, exc)
        ctype, text = gw.reply_fail("internal error")
        return Response(content=text, media_type=ctype, status_code=200)

    if not ok:
        log.warning("[%s] 订单 %s 入账被拒：%s", channel, order_no, msg)
        ctype, text = gw.reply_fail(msg)
        return Response(content=text, media_type=ctype, status_code=200)

    log.info("[%s] 订单 %s 入账成功，渠道流水 %s",
             channel, order_no, result.get("trade_no"))
    ctype, text = gw.reply_success()
    return Response(content=text, media_type=ctype, status_code=200)
