"""支付宝「当面付」—— alipay.trade.precreate（商家生成二维码，用户扫）。

与微信是两套完全不同的协议：微信是 JSON + 自定义 Authorization 头，
支付宝是表单参数排序 + RSA2(SHA256withRSA) 签名 + sign=xxx 追加在参数里。

坑位记录：
  1. **待签名串必须是「原始值」**，不能是 URL 编码后的值 —— 先按 key 升序拼好
     原文再签名，最后才做 urlencode。顺序反了签名必然不通过。
  2. 排除 `sign` 和 `sign_type` 本身，空值参数也要排除。
  3. 回调验签时，**验的是 sign**，参数用原始表单（不是 JSON）。
  4. total_amount 是**元**（字符串，两位小数），不是分 —— 和微信相反。
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote_plus, urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import settings
from app.paygate.base import (
    PayError,
    PayGateway,
    PayUnconfigured,
    NotifyResult,
    load_key,
)

log = logging.getLogger("stockdata.paygate.alipay")

GATEWAY = "https://openapi.alipay.com/gateway.do"
#: 沙箱环境（开发联调用，正式上线别开）
SANDBOX = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"


class AlipayGateway(PayGateway):
    name = "alipay"
    label = "支付宝"

    def __init__(self) -> None:
        self.appid = settings.ALIPAY_APPID
        self._private_key: rsa.RSAPrivateKey | None = None
        self._public_key = None

    @classmethod
    def configured(cls) -> bool:
        return all([
            settings.ALIPAY_APPID,
            settings.ALIPAY_PRIVATE_KEY,
            settings.ALIPAY_PUBLIC_KEY,
        ])

    def _privkey(self) -> rsa.RSAPrivateKey:
        if self._private_key is None:
            pem = load_key(settings.ALIPAY_PRIVATE_KEY)
            if not pem:
                raise PayUnconfigured("支付宝应用私钥未配置或文件不存在")
            self._private_key = serialization.load_pem_private_key(
                pem.encode(), password=None
            )
        return self._private_key

    def _pubkey(self):
        if self._public_key is None:
            pem = load_key(settings.ALIPAY_PUBLIC_KEY)
            if not pem:
                raise PayUnconfigured("支付宝公钥未配置或文件不存在")
            self._public_key = serialization.load_pem_public_key(pem.encode())
        return self._public_key

    # -- 签名 -------------------------------------------------------
    @staticmethod
    def _build_sign_content(params: dict) -> str:
        """按 key 升序拼 `k1=v1&k2=v2`，排除 sign / sign_type / 空值。"""
        parts = []
        for k in sorted(params):
            if k in ("sign", "sign_type"):
                continue
            v = params[k]
            if v is None or v == "":
                continue
            parts.append(f"{k}={v}")
        return "&".join(parts)

    def _sign(self, content: str) -> str:
        import base64

        sig = self._privkey().sign(content.encode("utf-8"),
                                   padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def _signed_params(self, biz: dict, notify_url: str = "") -> dict:
        params = {
            "app_id": self.appid,
            "method": "alipay.trade.precreate",
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": _now_str(),
            "version": "1.0",
            "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
        }
        if notify_url:
            params["notify_url"] = notify_url
        params["sign"] = self._sign(self._build_sign_content(params))
        return params

    # -- 下单 -------------------------------------------------------
    def create_payment(self, *, order_no: str, subject: str, amount_cents: int,
                       notify_url: str, return_url: str = "") -> dict:
        if not self.configured():
            raise PayUnconfigured("支付宝尚未配置商户参数")

        biz = {
            "out_trade_no": order_no,
            "total_amount": f"{amount_cents / 100.0:.2f}",   # 支付宝要**元**
            "subject": subject[:128],
        }
        params = self._signed_params(biz, notify_url)

        url = GATEWAY + "?" + urlencode(params, quote_via=quote_plus)
        try:
            r = httpx.get(url, timeout=15.0)
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise PayError(f"支付宝下单失败：{exc}") from exc

        resp = data.get("alipay_trade_precreate_response") or {}
        if resp.get("code") != "10000":
            raise PayError(
                f"支付宝下单失败：{resp.get('sub_code')} {resp.get('sub_msg')}"
            )
        qr = resp.get("qr_code")
        if not qr:
            raise PayError(f"支付宝未返回 qr_code：{resp}")
        return {"qr_content": qr, "raw": resp}

    # -- 回调 -------------------------------------------------------
    def parse_notify(self, headers: dict, body: bytes) -> NotifyResult:
        if not self.configured():
            raise PayUnconfigured("支付宝尚未配置商户参数")

        #: 支付宝回调是 application/x-www-form-urlencoded
        from urllib.parse import parse_qs, unquote

        raw = body.decode("utf-8", "replace")
        try:
            params = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        except Exception as exc:  # noqa: BLE001
            raise PayError(f"回调参数解析失败：{exc}") from exc

        sign = params.get("sign", "")
        if not sign:
            raise PayError("回调缺少 sign")

        content = self._build_sign_content(params)
        import base64

        try:
            self._pubkey().verify(
                base64.b64decode(sign), content.encode("utf-8"),
                padding.PKCS1v15(), hashes.SHA256(),
            )
        except Exception as exc:  # noqa: BLE001
            raise PayError(f"回调验签失败：{exc}") from exc

        state = params.get("trade_status", "")
        amount = 0
        try:
            amount = int(round(float(params.get("total_amount") or 0) * 100))
        except (TypeError, ValueError):
            amount = 0

        return NotifyResult(
            order_no=params.get("out_trade_no", ""),
            trade_no=params.get("trade_no", ""),
            amount=amount,
            #: TRADE_SUCCESS（可退款期）/ TRADE_FINISHED（不可退款）都算支付成功
            success=state in ("TRADE_SUCCESS", "TRADE_FINISHED"),
            raw=params,
        )

    def reply_success(self) -> tuple[str, str]:
        return "text/plain", "success"

    def reply_fail(self, msg: str) -> tuple[str, str]:
        return "text/plain", "fail"


def _now_str() -> str:
    from datetime import datetime, timedelta

    #: 支付宝要求北京时间，服务器若是 UTC 会报 invalid-timestamp
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
