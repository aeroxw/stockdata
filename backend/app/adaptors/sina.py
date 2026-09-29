"""新浪行情适配器 —— 备用快照源。

接口 : http://hq.sinajs.cn/list=sh600000,...
编码 : GBK
坑   : 必须带 Referer: https://finance.sina.com.cn，否则 403

字段索引（0-based）：
  0=名称 1=今开 2=昨收 3=现价 4=最高 5=最低 6=买一价 7=卖一价
  8=成交量(股) 9=成交额(元)
  10~19=买五档（**量在前、价在后**，与腾讯相反）
  20~29=卖五档
  30=日期 31=时间
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Sequence

from app.adaptors.base import (
    BaseAdaptor,
    CN_TZ,
    Quote,
    normalize_code,
    safe_float,
    safe_int,
    to_tencent_code,
)

_PATTERN = re.compile(r'hq_str_(\w+)="([^"]*)"')
_REFERER = "https://finance.sina.com.cn"


class SinaAdaptor(BaseAdaptor):
    name = "sina"
    support_batch = True
    batch_size = 50
    concurrency = 6

    BASE = "http://hq.sinajs.cn/list="

    async def fetch_quotes(self, codes: Sequence[str]) -> list[Quote]:
        if not codes:
            return []

        symbols = ",".join(to_tencent_code(c) for c in codes)
        text = await self._get(
            self.BASE + symbols,
            headers={"Referer": _REFERER},
            encoding="gbk",
        )

        out: list[Quote] = []
        for m in _PATTERN.finditer(text):
            q = self._parse(m.group(1), m.group(2))
            if q:
                out.append(q)
        return out

    def _parse(self, symbol: str, payload: str) -> Quote | None:
        f = payload.split(",")
        if len(f) < 32:
            return None

        code = normalize_code(symbol)
        price = safe_float(f[3])
        pre_close = safe_float(f[2])

        # 新浪买/卖档是「量在前、价在后」
        bid = [
            [safe_float(f[i + 1]), safe_int(f[i])]
            for i in range(10, 20, 2)
        ]
        ask = [
            [safe_float(f[i + 1]), safe_int(f[i])]
            for i in range(20, 30, 2)
        ]

        return Quote(
            source=self.name,
            code=code,
            name=f[0],
            price=price,
            pre_close=pre_close,
            open=safe_float(f[1]),
            high=safe_float(f[4]),
            low=safe_float(f[5]),
            volume=safe_int(f[8]),       # 新浪直接是股
            amount=safe_float(f[9]),     # 元
            change=round(price - pre_close, 4) if pre_close else 0.0,
            change_pct=(
                round((price - pre_close) / pre_close * 100, 4) if pre_close else 0.0
            ),
            bid=bid,
            ask=ask,
            ts=self._parse_ts(f[30], f[31]),
        )

    @staticmethod
    def _parse_ts(date_s: str, time_s: str) -> datetime:
        try:
            return datetime.strptime(
                f"{date_s.strip()} {time_s.strip()}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=CN_TZ)
        except Exception:  # noqa: BLE001
            return datetime.now(CN_TZ)
