"""东方财富适配器 —— 快照 + K 线 + 复权因子。

实时 : https://push2.eastmoney.com/api/qt/stock/get?secid=1.600000&fields=...
K线  : https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=...&klt=101&fqt=1

两个坑：
  1. secid 形式是 `1.600000`(沪) / `0.000001`(深)，不是 sh600000
  2. 价格类字段是「整数放大 100 倍」，例如 f43=916 表示 9.16，必须 /100
     （换手率 f168、涨跌幅 f170 同理）
"""

from __future__ import annotations

from typing import Sequence

from app.adaptors.base import (
    AdaptorError,
    BaseAdaptor,
    KlineBar,
    Quote,
    normalize_code,
    safe_float,
    safe_int,
    to_eastmoney_secid,
)

# klt: 101=日 102=周 103=月 ; fqt: 0=不复权 1=前复权 2=后复权
_PERIOD_MAP = {"day": 101, "week": 102, "month": 103}
_ADJUST_MAP = {"none": 0, "qfq": 1, "hfq": 2}


class EastmoneyAdaptor(BaseAdaptor):
    name = "eastmoney"
    support_batch = False      # 该端点一次一只，批量靠并发
    batch_size = 1
    #: 并发从 8 降到 2：东财限流不看状态码，直接在 TCP 层掐连接，
    #: 高并发只会更快把配额打光（实测并发 8 时第 1 次之后基本全挂）。
    concurrency = 2
    #: 两次请求至少隔 0.6s —— 别把配额一次性打光。
    #: 详见 base.BaseAdaptor.min_interval 里的实测数据。
    min_interval = 0.6
    #: 退避拉长到 2s / 4s：限流后秒级重试没意义（实测要静默 20s 才恢复），
    #: 但也不能让调用方干等，所以配合下面的熔断快速失败。
    retry_backoff = 2.0
    net_retries = 2
    #: 连续 3 次失败即冷却 30s，冷却期内快速失败让聚合器马上下沉到其它源。
    #: 东财排在快照链最后一位，本来就是兜底源，快速失败比死等更划算。
    circuit_fail_threshold = 3
    circuit_cooldown = 30.0

    BASE = "https://push2.eastmoney.com/api/qt/stock/get"
    KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    # 东财会校验来源，缺 Referer 会被直接断开连接
    HEADERS = {"Referer": "https://quote.eastmoney.com/"}

    FIELDS = (
        "f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f169,f170,"
        "f116,f117,f162,f167,f50,f51,f52,f19,f20,f17,f18,f15,f16,f13,f14,f11,f12,f39,f37,f35,f33,f31"
    )

    async def fetch_quotes(self, codes: Sequence[str]) -> list[Quote]:
        import asyncio

        sem = asyncio.Semaphore(self.concurrency)
        errors: list[str] = []

        async def one(code: str) -> Quote | None:
            async with sem:
                try:
                    data = await self._get_json(
                        self.BASE,
                        {"secid": to_eastmoney_secid(code), "fields": self.FIELDS},
                        headers=self.HEADERS,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
                    return None
            d = (data or {}).get("data") or {}
            if not d:
                return None
            return self._parse(d)

        results = await asyncio.gather(*[one(c) for c in codes])
        out = [r for r in results if r]

        #: 一只都没取到、且每次都是**报错**（不是"代码不存在"）时，必须抛出去。
        #: 以前这里直接返回空列表，上层（聚合器）分不清是"这批代码东财没有"
        #: 还是"东财整体不可用"，结果就是静默返回 0 条、体检看起来像成功，
        #: 实际上数据源已经废了。抛出去，聚合器才能正确下沉到其它源。
        if codes and not out and errors and len(errors) >= len(codes):
            raise AdaptorError(f"[eastmoney] 全部 {len(codes)} 只均取数失败：{errors[0]}")
        return out

    def _parse(self, d: dict) -> Quote:
        # 价格类字段统一 /100
        price = safe_float(d.get("f43")) / 100
        pre_close = safe_float(d.get("f60")) / 100
        return Quote(
            source=self.name,
            code=normalize_code(str(d.get("f57") or "")),
            name=str(d.get("f58") or ""),
            price=price,
            pre_close=pre_close,
            open=safe_float(d.get("f46")) / 100,
            high=safe_float(d.get("f44")) / 100,
            low=safe_float(d.get("f45")) / 100,
            volume=safe_int(d.get("f47")) * 100,      # 手 -> 股
            amount=safe_float(d.get("f48")),          # 元
            change=safe_float(d.get("f169")) / 100,
            change_pct=safe_float(d.get("f170")) / 100,
            turnover_rate=safe_float(d.get("f168")) / 100,
            circ_mv=(
                safe_float(d.get("f116")) / 1e8 if d.get("f116") else None
            ),   # 元 -> 亿元
            total_mv=(
                safe_float(d.get("f117")) / 1e8 if d.get("f117") else None
            ),
            pe=safe_float(d.get("f162")) / 100,
            pb=safe_float(d.get("f167")) / 100,
        )

    async def fetch_kline(
        self, code: str, period: str = "day", limit: int = 100, adjust: str = "qfq"
    ) -> list[KlineBar]:
        params = {
            "secid": to_eastmoney_secid(code),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": _PERIOD_MAP.get(period, 101),
            "fqt": _ADJUST_MAP.get(adjust, 1),
            "end": "20500101",
            "lmt": max(limit, 1),
        }
        data = await self._get_json(self.KLINE, params, headers=self.HEADERS)
        klines = ((data or {}).get("data") or {}).get("klines") or []

        norm = normalize_code(code)
        bars: list[KlineBar] = []
        for line in klines:
            # 2026-09-23,9.02,8.98,9.05,8.95,511244,458952160,...
            parts = line.split(",")
            if len(parts) < 6:
                continue
            bars.append(
                KlineBar(
                    source=self.name,
                    code=norm,
                    date=parts[0],
                    open=safe_float(parts[1]),
                    close=safe_float(parts[2]),
                    high=safe_float(parts[3]),
                    low=safe_float(parts[4]),
                    volume=safe_int(parts[5]),
                    amount=safe_float(parts[6]) if len(parts) > 6 else 0.0,
                )
            )
        return bars[-limit:] if limit > 0 else bars
