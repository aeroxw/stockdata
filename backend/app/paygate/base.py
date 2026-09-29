"""支付网关抽象层。

六家数据源已经证明了「统一适配层」的价值：上游格式天差地别，收敛成同一个
模型后，上层完全不用关心背后是哪家。支付同理 —— 微信是 JSON + RSA 签名，
支付宝是表单 + RSA2 签名，协议完全不同，但对业务层暴露的接口应该一模一样：

    gateway.create_payment(...)   -> 拿到二维码/收银台地址
    gateway.parse_notify(...)     -> 验签并返回标准化的回调结果

业务侧（billing 路由）只认这两个方法。以后要加第三家（比如 PayPal），
只需新增一个文件，不用动任何已有代码。

**关于没有商户资质时怎么办**：
每个网关都有 `configured()`，未配置时 `create_payment` 直接抛
`PayUnconfigured`。上层据此给前端返回 503 并从支付渠道列表里剔除该选项，
站点其余功能完全不受影响。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

log = logging.getLogger("stockdata.paygate")


class PayError(Exception):
    """支付网关通用错误（网络失败、上游拒绝等）。"""


class PayUnconfigured(PayError):
    """该渠道尚未配置商户参数。"""


def load_key(value: str) -> str:
    """取私钥/公钥：既支持 PEM 全文，也支持文件路径。

    之所以支持路径：PEM 是多行文本，硬塞进 docker-compose 的 environment 或
    .env 里极易被换行符搞坏（而且会把密钥明文留在 compose 文件里）。
    挂载成文件、配置项只写路径要干净得多。

    判定规则：以 `-----BEGIN` 开头就当 PEM 全文，否则当路径读。
    """
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("-----BEGIN"):
        return v
    p = Path(v)
    if p.is_file():
        return p.read_text(encoding="utf-8")
    log.warning("密钥路径不存在：%s", v)
    return ""


class NotifyResult(dict):
    """标准化的回调结果。

    order_no    本平台订单号
    trade_no    渠道流水号（微信 transaction_id / 支付宝 trade_no）
    amount      金额（分）
    success     是否支付成功
    raw         渠道原始报文，留档排查用
    """


class PayGateway(ABC):
    """支付渠道基类。"""

    name: str = ""           # wechat / alipay
    label: str = ""          # 微信支付 / 支付宝

    @classmethod
    @abstractmethod
    def configured(cls) -> bool:
        """商户参数是否齐备。"""

    @abstractmethod
    def create_payment(
        self,
        *,
        order_no: str,
        subject: str,
        amount_cents: int,
        notify_url: str,
        return_url: str = "",
    ) -> dict:
        """下单，返回给前端用于唤起支付的信息。

        返回至少包含：
            qr_content  二维码内容（前端据此生成二维码）
            raw         渠道原始响应
        微信返回 code_url（weixin://pay/...），支付宝返回 qr_code，
        两者都放进 qr_content，前端不用分支处理。
        """

    @abstractmethod
    def parse_notify(self, headers: dict, body: bytes) -> NotifyResult:
        """校验回调签名并解析出标准结果。

        **验签必须在这里做完** —— 回调地址是公网可访问的，任何人都能 POST，
        不验签就入账等于白送。抛 PayError 表示验签失败或报文非法。
        """

    @abstractmethod
    def reply_success(self) -> tuple[str, str]:
        """给渠道的应答（content-type, body）。回错了渠道会持续重发。"""

    @abstractmethod
    def reply_fail(self, msg: str) -> tuple[str, str]:
        """应答失败。注意：业务逻辑处理完但应答失败，渠道会重发，
        所以上层必须保证入账是**幂等**的。"""

    def query_order(self, order_no: str) -> dict | None:
        """主动查单（可选实现）。用于对账和"回调丢了"时的兜底补单。"""
        return None
