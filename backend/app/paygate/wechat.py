"""微信支付 Native（扫码支付），API v3。

下单：POST https://api.mch.weixin.qq.com/v3/pay/transactions/native
回调：验签（平台公钥）后用 APIv3 密钥 AES-256-GCM 解密 resource

三个容易踩错的点，都写死在这里了：
  1. **签名串必须以换行结尾**：`method\nurl\ntimestamp\nnonce\nbody\n`
     —— 最后那个 `\n` 漏掉，签名永远不对，微信只回「签名验证失败」，
     不会告诉你差在哪。
  2. 金额是 **int 分**，不是元，也不是字符串。
  3. 回调的 `resource` 是加密的，验签通过**不代表**可以直接信里面的金额，
     必须解密后再核对 out_trade_no 与 amount，否则等于没验。
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings
from app.paygate.base import (
    PayError,
    PayGateway,
    PayUnconfigured,
    NotifyResult,
    load_key,
)

log = logging.getLogger("stockdata.paygate.wechat")

BASE = "https://api.mch.weixin.qq.com"


class WechatPayGateway(PayGateway):
    name = "wechat"
    label = "微信支付"

    def __init__(self) -> None:
        self.appid = settings.WXPAY_APPID
        self.mchid = settings.WXPAY_MCHID
        self.serial_no = settings.WXPAY_SERIAL_NO
        self.api_v3_key = settings.WXPAY_API_V3_KEY
        self._private_key: rsa.RSAPrivateKey | None = None
        self._platform_pubkey = None

    # -- 配置检查 ---------------------------------------------------
    @classmethod
    def configured(cls) -> bool:
        return all([
            settings.WXPAY_APPID,
            settings.WXPAY_MCHID,
            settings.WXPAY_SERIAL_NO,
            settings.WXPAY_API_V3_KEY,
            settings.WXPAY_PRIVATE_KEY,
            settings.WXPAY_PLATFORM_PUBKEY,
        ])

    def _privkey(self) -> rsa.RSAPrivateKey:
        if self._private_key is None:
            pem = load_key(settings.WXPAY_PRIVATE_KEY)
            if not pem:
                raise PayUnconfigured("微信商户私钥未配置或文件不存在")
            self._private_key = serialization.load_pem_private_key(
                pem.encode(), password=None
            )
        return self._private_key

    def _pubkey(self):
        if self._platform_pubkey is None:
            pem = load_key(settings.WXPAY_PLATFORM_PUBKEY)
            if not pem:
                raise PayUnconfigured("微信支付平台公钥未配置或文件不存在")
            self._platform_pubkey = serialization.load_pem_public_key(pem.encode())
        return self._platform_pubkey

    # -- 签名 -------------------------------------------------------
    def _sign(self, method: str, url_path: str, timestamp: str,
              nonce: str, body: str) -> str:
        #: 注意结尾的 \n —— 少这个换行签名必然失败，且微信不会提示原因
        message = f"{method}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n"
        sig = self._privkey().sign(
            message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
        )
        return base64.b64encode(sig).decode()

    def _auth_header(self, method: str, url_path: str, body: str) -> str:
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        sig = self._sign(method, url_path, ts, nonce, body)
        return (
            f'WECHATPAY2-SHA256-RSA2048 '
            f'mchid="{self.mchid}",nonce_str="{nonce}",'
            f'signature="{sig}",timestamp="{ts}",serial_no="{self.serial_no}"'
        )

    # -- 下单 -------------------------------------------------------
    def create_payment(self, *, order_no: str, subject: str, amount_cents: int,
                       notify_url: str, return_url: str = "") -> dict:
        if not self.configured():
            raise PayUnconfigured("微信支付尚未配置商户参数")

        url_path = "/v3/pay/transactions/native"
        body = json.dumps({
            "appid": self.appid,
            "mchid": self.mchid,
            "description": subject[:127],
            "out_trade_no": order_no,
            "notify_url": notify_url,
            "amount": {"total": int(amount_cents), "currency": "CNY"},
        }, ensure_ascii=False)

        headers = {
            "Authorization": self._auth_header("POST", url_path, body),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "StockData/1.0",
        }
        try:
            r = httpx.post(BASE + url_path, content=body.encode("utf-8"),
                           headers=headers, timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            raise PayError(f"微信下单网络失败：{exc}") from exc

        if r.status_code != 200:
            raise PayError(f"微信下单失败：HTTP {r.status_code} {r.text[:200]}")

        data = r.json()
        code_url = data.get("code_url")
        if not code_url:
            raise PayError(f"微信下单未返回 code_url：{data}")
        return {"qr_content": code_url, "raw": data}

    # -- 回调 -------------------------------------------------------
    def _verify_signature(self, headers: dict, body: bytes) -> None:
        ts = headers.get("wechatpay-timestamp", "")
        nonce = headers.get("wechatpay-nonce", "")
        signature = headers.get("wechatpay-signature", "")
        serial = headers.get("wechatpay-serial", "")
        if not (ts and nonce and signature):
            raise PayError("回调缺少验签头")

        message = f"{ts}\n{nonce}\n{body.decode('utf-8')}\n".encode()
        try:
            self._pubkey().verify(
                base64.b64decode(signature), message,
                padding.PKCS1v15(), hashes.SHA256(),
            )
        except Exception as exc:  # noqa: BLE001
            raise PayError(f"回调验签失败（serial={serial}）：{exc}") from exc

    def _decrypt_resource(self, payload: dict) -> dict:
        resource = payload.get("resource") or {}
        try:
            #: associated_data 可为空，但 GCM 要求传 bytes 而非 None
            aad = (resource.get("associated_data") or "").encode()
            cipher = AESGCM(self.api_v3_key.encode())
            plain = cipher.decrypt(
                base64.b64decode(resource["nonce"]),
                base64.b64decode(resource["ciphertext"]),
                aad,
            )
            return json.loads(plain.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise PayError(f"回调报文解密失败：{exc}") from exc

    def parse_notify(self, headers: dict, body: bytes) -> NotifyResult:
        if not self.configured():
            raise PayUnconfigured("微信支付尚未配置商户参数")

        #: 顺序很重要：**先验签再解密**。验签是对整段 body 做的，
        #: 拿到密文就急着解，等于任何人都构造报文让你解。
        self._verify_signature(headers, body)

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise PayError(f"回调 JSON 解析失败：{exc}") from exc

        detail = self._decrypt_resource(payload)
        state = detail.get("trade_state")
        return NotifyResult(
            order_no=detail.get("out_trade_no", ""),
            trade_no=detail.get("transaction_id", ""),
            amount=int((detail.get("amount") or {}).get("total") or 0),
            success=(state == "SUCCESS"),
            raw=detail,
        )

    def reply_success(self) -> tuple[str, str]:
        return "application/json", '{"code":"SUCCESS","message":"成功"}'

    def reply_fail(self, msg: str) -> tuple[str, str]:
        return "application/json", json.dumps({"code": "FAIL", "message": msg[:100]},
                                              ensure_ascii=False)
