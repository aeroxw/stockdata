"""支付渠道注册表。

新增渠道只需：写一个继承 PayGateway 的类，然后在这里加一行。
业务代码（billing 路由）永远只认 `get_gateway(name)`。
"""

from __future__ import annotations

from app.config import settings
from app.paygate.alipay import AlipayGateway
from app.paygate.base import PayGateway, PayUnconfigured
from app.paygate.wechat import WechatPayGateway

_GATEWAYS: dict[str, type[PayGateway]] = {
    "wechat": WechatPayGateway,
    "alipay": AlipayGateway,
}


def get_gateway(name: str) -> PayGateway:
    """取渠道实例。未注册的渠道直接报错，避免落到"静默放行"的坑里。"""
    cls = _GATEWAYS.get(name)
    if cls is None:
        raise PayUnconfigured(f"未知的支付渠道：{name}")
    return cls()


def available_channels() -> list[dict]:
    """当前真正可用的在线支付渠道（供前端渲染收银台）。

    只返回**商户参数已配置**的渠道 —— 没配置就说没配置，
    别给用户一个点了必然报错的按钮。
    """
    out = []
    for name, cls in _GATEWAYS.items():
        out.append({
            "channel": name,
            "label": cls.label,
            "configured": bool(cls.configured()),
        })
    return out


def notify_url_for(channel: str) -> str:
    """拼回调地址。

    优先用显式配置（WXPAY_NOTIFY_URL / ALIPAY_NOTIFY_URL），
    没配就用 SITE_BASE_URL 拼默认路径。两者都没有就返回空 ——
    上层据此判定"这渠道没法接回调"，宁可不给出入口。
    """
    base = (settings.SITE_BASE_URL or "").rstrip("/")
    if channel == "wechat":
        if settings.WXPAY_NOTIFY_URL:
            return settings.WXPAY_NOTIFY_URL
        return f"{base}/api/v1/pay/notify/wechat" if base else ""
    if channel == "alipay":
        if settings.ALIPAY_NOTIFY_URL:
            return settings.ALIPAY_NOTIFY_URL
        return f"{base}/api/v1/pay/notify/alipay" if base else ""
    return ""


__all__ = [
    "PayGateway", "PayUnconfigured",
    "WechatPayGateway", "AlipayGateway",
    "get_gateway", "available_channels", "notify_url_for",
]
