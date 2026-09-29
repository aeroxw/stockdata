"""腾讯行情适配器 —— 盘中高频快照主源。

接口 : http://qt.gtimg.cn/q=sh600000,sz000001,...
编码 : GBK
格式 : v_sh600000="1~浦发银行~600000~9.16~9.00~...";

两个必踩的坑（已实测）：
  1. 解析**必须**用 re.finditer(r'v_(\\w+)="([^"]*)"')，不能用 re.match。
     响应以 `;` + 换行分隔，第二段起开头是 \\n，match 只会命中第一条。
  2. 单次批量 50~60 只最稳，300 只基本全丢。

字段索引（0-based）：
  1=名称 2=代码 3=现价 4=昨收 5=今开 6=成交量(手)
  9~18=买五档(价,量交替)  19~28=卖五档
  30=时间 31=涨跌 32=涨跌% 33=最高 34=最低
  36=成交量(手) 37=成交额(万) 38=换手率 39=市盈率
  43=振幅 44=流通市值(亿) 45=总市值 46=市净率 47=涨停价 48=跌停价 49=量比
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

_PATTERN = re.compile(r'v_(\w+)="([^"]*)"')


class TencentAdaptor(BaseAdaptor):
    name = "tencent"
    support_batch = True
    batch_size = 50      # 实测上限，超过会被掐
    concurrency = 6

    BASE = "http://qt.gtimg.cn/q="

    async def fetch_quotes(self, codes: Sequence[str]) -> list[Quote]:
        if not codes:
            return []

        symbols = ",".join(to_tencent_code(c) for c in codes)
        text = await self._get(self.BASE + symbols, encoding="gbk")

        out: list[Quote] = []
        # 必须用 finditer：响应是多段 `;` 分隔的（finditer 返回 Match，需取 group）
        # 注意：代码市场后缀要取 symbol（如 v_sh000001），不能用字段[2] 的裸代码
        #      否则 000001.SH（上证指数）会被按号段误判成 000001.SZ（平安银行）
        for m in _PATTERN.finditer(text):
            q = self._parse(m.group(1), m.group(2))
            if q:
                out.append(q)
        return out

    def _parse(self, symbol: str, payload: str) -> Quote | None:
        f = payload.split("~")
        if len(f) < 50:
            return None

        code = normalize_code(symbol)
        if not code:
            return None

        price = safe_float(f[3])
        pre_close = safe_float(f[4])
        # 成交量单位是手 -> 转成股；成交额单位是万元 -> 转成元
        volume = safe_int(f[36]) * 100
        amount = safe_float(f[37]) * 10000

        # 五档：买盘在 9~18，卖盘在 19~28，都是「价、量」交替
        bid = [
            [safe_float(f[i]), safe_int(f[i + 1])]
            for i in range(9, 19, 2)
        ]
        ask = [
            [safe_float(f[i]), safe_int(f[i + 1])]
            for i in range(19, 29, 2)
        ]

        return Quote(
            source=self.name,
            code=code,
            name=f[1],
            price=price,
            pre_close=pre_close,
            open=safe_float(f[5]),
            high=safe_float(f[33]),
            low=safe_float(f[34]),
            volume=volume,
            amount=amount,
            change=safe_float(f[31]),
            change_pct=safe_float(f[32]),
            turnover_rate=safe_float(f[38]),
            vol_ratio=safe_float(f[49]),
            circ_mv=safe_float(f[44]),
            total_mv=safe_float(f[45]),
            pe=safe_float(f[39]),
            pb=safe_float(f[46]),
            bid=bid,
            ask=ask,
            ts=self._parse_ts(f[30]),
        )

    @staticmethod
    def _parse_ts(raw: str) -> datetime:
        """20260928161458 -> datetime(含 +08:00)"""
        s = (raw or "").strip()
        try:
            return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
        except Exception:  # noqa: BLE001
            return datetime.now(CN_TZ)
