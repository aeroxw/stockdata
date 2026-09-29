"""通达信适配器 —— 历史 K 线主力源。

走 NAS 上已部署的 easy-tdx：GET /api/v1/bars?market=SH&code=600000&category=DAY&count=90&adjust=QFQ

实测结论（很重要）：
  * 参数是 market(SH/SZ/BJ) + code，**不是 thscode**
  * 服务端死限约 **28 请求/秒**（并发 12/16/24 都一样，服务端硬限）
  * 全市场 5574 只约 230 秒 —— 这是全市场日 K 的最优路径
    比腾讯 web.ifzq.gtimg.cn 稳得多（后者连续拉几千只会被掐死）
"""

from __future__ import annotations

from typing import Sequence

from app.adaptors.base import (
    BaseAdaptor,
    KlineBar,
    Quote,
    safe_float,
    safe_int,
    split_market,
)
from app.config import settings

#: easy-tdx category 映射。
#:
#: **格式是下划线**：分钟级是 `MIN_60` 而不是 `MIN60`/`HOUR`。
#: 正确枚举来自 easy-tdx 自己的 openapi.json：
#:   MIN_1, MIN_5, MIN_15, MIN_30, MIN_60, MIN_120, DAY, WEEK, MONTH, SEASON, YEAR
#: 以前这里写的 HOUR / MIN30 / MIN15…，全是无效值 ——
#: 任何分钟级 K 线请求都会被回 400，TDX 源静默失效、
#: 只能靠聚合器降级到同花顺/东财，表面上"能用"但 TDX 从没真正工作过。
#: 查法：curl http://<NAS内网IP>:8000/openapi.json 看 /api/v1/bars 的 category 描述。
_CATEGORY = {
    "day": "DAY",
    "week": "WEEK",
    "month": "MONTH",
    "60min": "MIN_60",
    "30min": "MIN_30",
    "15min": "MIN_15",
    "5min": "MIN_5",
    "1min": "MIN_1",
}
_ADJUST = {"none": "NONE", "qfq": "QFQ", "hfq": "HFQ"}


class TdxAdaptor(BaseAdaptor):
    name = "tdx"
    support_batch = False
    batch_size = 1
    concurrency = 12      # 实测服务端死限 28 req/s，并发 12 最稳

    def __init__(self) -> None:
        super().__init__()
        self.base = settings.EASYTDX_BASE.rstrip("/")

    async def fetch_quotes(self, codes: Sequence[str]) -> list[Quote]:
        """easy-tdx 的实时报价走 mac/quote-list；这里退化为用最新一根日 K 近似。

        盘中实时快照请用腾讯/新浪，本适配器专注 K 线。
        """
        out: list[Quote] = []
        for c in codes:
            try:
                bars = await self.fetch_kline(c, "day", 1, "none")
            except Exception:  # noqa: BLE001
                continue
            if bars:
                b = bars[0]
                out.append(
                    Quote(
                        source=self.name,
                        code=b.code,
                        price=b.close,
                        open=b.open,
                        high=b.high,
                        low=b.low,
                        volume=b.volume,
                        amount=b.amount,
                    )
                )
        return out

    async def fetch_kline(
        self, code: str, period: str = "day", limit: int = 100, adjust: str = "qfq"
    ) -> list[KlineBar]:
        market, num = split_market(code)
        params = {
            "market": market,
            "code": num,
            "category": _CATEGORY.get(period, "DAY"),
            "count": max(limit, 1),
            "adjust": _ADJUST.get(adjust, "QFQ"),
        }
        data = await self._get_json(f"{self.base}/api/v1/bars", params)
        rows = (data or {}).get("data") or []

        bars: list[KlineBar] = []
        for r in rows:
            # date 形如 2026-09-23T00:00:00
            date_s = str(r.get("date") or "")[:10]
            bars.append(
                KlineBar(
                    source=self.name,
                    code=code,
                    date=date_s,
                    open=safe_float(r.get("open")),
                    high=safe_float(r.get("high")),
                    low=safe_float(r.get("low")),
                    close=safe_float(r.get("close")),
                    volume=safe_int(r.get("vol")),
                    amount=safe_float(r.get("amount")),
                )
            )
        return bars[-limit:] if limit > 0 else bars
