"""同花顺（fuyao）适配器 —— 主力源。

BaseURL : https://fuyao.aicubes.cn
鉴权    : Header `X-api-key`（注意不是 Authorization: Bearer）
响应信封: {"code":0,"message":"success","data":{"item":[...]}}

关键实测结论：
  * `/api/a-share/prices/snapshot` 一次返回全市场 5578 只，成本极低 -> 全市场快照首选
  * `/api/a-share/prices/historical` 强约束单只标的（不接受逗号）、仅 interval=1d
    -> 全市场 K 线走 easy-tdx，不用它
  * capital-flow / high-frequency / news 返回 code=2004（需客户端版），本 Key 无权
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from app.adaptors.base import (
    AdaptorError,
    BaseAdaptor,
    KlineBar,
    Quote,
    normalize_code,
    safe_float,
    safe_int,
)
from app.config import settings


class HithinkAdaptor(BaseAdaptor):
    name = "hithink"
    support_batch = True
    # 一次就能拿全市场，但仍分批避免单次响应过大
    batch_size = 500
    concurrency = 2

    def __init__(self) -> None:
        super().__init__()
        self.base = settings.HITHINK_BASE.rstrip("/")
        self.api_key = settings.HITHINK_API_KEY

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-api-key": self.api_key}

    # ---------------------------------------------------------------- 通用请求
    async def _api(self, path: str, params: dict | None = None) -> Any:
        if not self.api_key:
            raise AdaptorError("[hithink] 未配置 HITHINK_API_KEY")

        data = await self._get_json(
            f"{self.base}{path}", params=params, headers=self._headers
        )
        if not isinstance(data, dict):
            raise AdaptorError("[hithink] 响应格式异常")

        code = data.get("code")
        if code == 2004:
            raise AdaptorError("[hithink] 该端点需同花顺 AI 客户端权限 (code=2004)")
        if code != 0:
            raise AdaptorError(
                f"[hithink] {data.get('message')} (code={code})"
            )
        return data.get("data") or {}

    # ---------------------------------------------------------------- 实时快照
    async def fetch_quotes(self, codes: Sequence[str]) -> list[Quote]:
        """全市场快照。

        实测该端点忽略 ticker 过滤、直接返回全市场，因此这里按需过滤。
        """
        data = await self._api("/api/a-share/prices/snapshot")
        items = data.get("item") or []
        if not items:
            return []

        wanted = {normalize_code(c) for c in codes} if codes else None
        now = data.get("timestamp")

        out: list[Quote] = []
        for it in items:
            code = normalize_code(it.get("thscode") or it.get("ticker") or "")
            if wanted and code not in wanted:
                continue
            price = safe_float(it.get("last_price"))
            prev = safe_float(it.get("prev_price"))
            out.append(
                Quote(
                    source=self.name,
                    code=code,
                    name=it.get("name") or "",
                    price=price,
                    pre_close=prev,
                    open=safe_float(it.get("open_price")),
                    high=safe_float(it.get("high_price")),
                    low=safe_float(it.get("low_price")),
                    volume=safe_int(it.get("volume")),
                    amount=safe_float(it.get("turnover")),
                    change=safe_float(it.get("price_change")),
                    change_pct=safe_float(it.get("price_change_ratio_pct")),
                    turnover_rate=(
                        safe_float(it["turnover_rate"])
                        if it.get("turnover_rate") is not None
                        else None
                    ),
                    vol_ratio=(
                        safe_float(it["volume_ratio"])
                        if it.get("volume_ratio") is not None
                        else None
                    ),
                    ts=self._ts(now),
                )
            )
        return out

    async def fetch_all_quotes(self) -> list[Quote]:
        """拿全市场快照（不过滤）。全市场定时任务用这个。"""
        return await self.fetch_quotes([])

    # ---------------------------------------------------------------- K 线
    async def fetch_kline(
        self, code: str, period: str = "day", limit: int = 100, adjust: str = "qfq"
    ) -> list[KlineBar]:
        """单只标的日线。注意：接口不接受逗号，一次只能一只。"""
        thscode = normalize_code(code)
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - max(limit, 1) * 24 * 3600 * 1000 * 2  # 多取些以覆盖非交易日

        adjust_map = {"none": "none", "qfq": "forward", "hfq": "backward"}
        data = await self._api(
            "/api/a-share/prices/historical",
            {
                "thscode": thscode,
                "interval": "1d",
                "start": start_ms,
                "end": end_ms,
                "adjust": adjust_map.get(adjust, "forward"),
            },
        )
        items = data.get("item") or []
        bars = [
            KlineBar(
                source=self.name,
                code=thscode,
                date=self._fmt_date(it.get("date_ms")),
                open=safe_float(it.get("open_price")),
                high=safe_float(it.get("high_price")),
                low=safe_float(it.get("low_price")),
                close=safe_float(it.get("close_price")),
                volume=safe_int(it.get("volume")),
                amount=safe_float(it.get("turnover")),
            )
            for it in items
        ]
        return bars[-limit:] if limit > 0 else bars

    # ---------------------------------------------------------------- 特色数据
    async def special(self, kind: str, **params: Any) -> Any:
        """特色数据统一入口。

        kind: limit_up / limit_down / limit_break / limit_up_ladder /
              dragon_tiger / skyrocket / hot_stock / anomaly
        """
        mapping = {
            "limit_up": "/api/a-share/special-data/limit-up-pool",
            "limit_down": "/api/a-share/special-data/limit-down-pool",
            "limit_break": "/api/a-share/special-data/limit-break-pool",
            "limit_up_ladder": "/api/a-share/special-data/limit-up-ladder",
            "dragon_tiger": "/api/a-share/special-data/dragon-tiger-list",
            "skyrocket": "/api/a-share/special-data/skyrocket-list",
            "hot_stock": "/api/a-share/special-data/hot-stock-list",
            "anomaly": "/api/a-share/special-data/anomaly-analysis-list",
        }
        path = mapping.get(kind)
        if not path:
            raise AdaptorError(f"[hithink] 未知特色数据类型: {kind}")
        return await self._api(path, params or None)

    async def valuation(self, codes: Sequence[str]) -> list[dict]:
        data = await self._api(
            "/api/a-share/valuations/snapshot",
            {"thscodes": ",".join(normalize_code(c) for c in codes)},
        )
        return data.get("item") or []

    async def ticker_list(self, limit: int = 6000) -> list[dict]:
        data = await self._api("/api/meta/tickers/list", {"limit": limit})
        return data.get("item") or []

    async def trading_days(self, start: str, end: str) -> list[str]:
        """start/end 形如 20260901"""
        data = await self._api(
            "/api/a-share/calendar/trading-days",
            {"start_date": start, "end_date": end},
        )
        return [it.get("date") for it in (data.get("item") or [])]

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _ts(ms: Any):
        from datetime import datetime

        from app.adaptors.base import CN_TZ

        try:
            return datetime.fromtimestamp(int(ms) / 1000, tz=CN_TZ)
        except Exception:  # noqa: BLE001
            return datetime.now(CN_TZ)

    @staticmethod
    def _fmt_date(ms: Any) -> str:
        from datetime import datetime

        from app.adaptors.base import CN_TZ

        try:
            return datetime.fromtimestamp(int(ms) / 1000, tz=CN_TZ).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return ""
