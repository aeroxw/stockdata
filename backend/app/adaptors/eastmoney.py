"""东方财富适配器 —— 快照（批量）+ K 线。

快照 : https://push2.eastmoney.com/api/qt/ulist.np/get?secids=1.600000,0.000001&...
K线  : https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=...

======================================================================
2026-09-29 重大修正：单只快照端点已被东财下线
======================================================================
以前快照用的是 `/api/qt/stock/get?secid=...`（一只一个请求）。
实测（同一台机器、同一秒对照，排除了 IP 限流这个变量）：

    /api/qt/stock/get        -> 连接被掐断，永远拿不到响应 ❌
    /api/qt/ulist.np/get     -> 200，一次可带一批代码 ✅
    /api/qt/stock/trends2/get-> 200 ✅
    push2his .../kline/get   -> 连接被掐断 ❌（用官网 JS 里的原样参数也一样）

所以 `stock/get` 不是"暂时抽风"，是已经不能用了 —— 继续用它等于
这个源永远拿不到数据。现在改用批量端点 `ulist.np/get`，顺带把
「50 只股票 = 50 个请求」压成「50 只 = 1 个请求」，请求量降两个数量级，
这本身也是防止再把出口 IP 打进限流名单的关键。

另外东财的限流是**按出口 IP 的**：网页正常（www/quota 页都 200，
`push2.eastmoney.com/` 也返回 404），但 `/api/qt/*` 的真实数据请求
会被直接掐断，且不返回 429。实测一台干净 IP 只要密集打十几条就会
进入这个状态，静默 120s 不足以恢复 —— 所以一旦被判限流，**不要重试**，
越敲越难恢复。下面的 net_retries=0 / 熔断参数就是按这条实测结论设的。

======================================================================
字段含义（ulist.np/get + fltt=2，已用真实响应逐项核对过）
======================================================================
  f2  最新价      f3  涨跌幅%    f4  涨跌额     f5  成交量(手)
  f6  成交额(元)  f7  振幅%      f8  换手率%    f9  市盈率(动)
  f10 量比        f12 代码       f13 市场(1沪0深) f14 名称
  f15 最高        f16 最低       f17 今开       f18 昨收
  f20 总市值(元)  f21 流通市值(元) f23 市净率

**fltt=2 时必须带**：不带的话价格类字段会变成放大整数（f43=916 表示 9.16），
这正是老端点 stock/get 的做法。带上 fltt=2 后全部是原始浮点数。

校验实例（浦发银行 600000，2026-09-29 盘中）：
  f2=9.18 f3=0.22 f18=9.16 -> 9.16×(1+0.22%) = 9.1801 ✓
  f7=1.75 vs (f15-f16)/f18=(9.25-9.09)/9.16=1.746% ✓
  f21/f2=305747595594/9.18=3.33e10 股 -> f5 手数×100/流通股=0.24% = f8 ✓
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
    #: ulist.np/get 支持一次带一批 secids，改成批量后请求量大降。
    support_batch = True
    batch_size = 50
    #: 批量之后本来就没有并发需要了，留 1 避免自己跟自己抢配额。
    concurrency = 1
    #: 两次请求至少隔 1s。东财的判据是"短时间内的请求条数"，
    #: 实测密集打十几条就会进限流状态，节流是唯一的预防手段。
    min_interval = 1.0
    #: **不重试**。实测被掐断后重试从来没有救回来过（静默 2 分钟都不行），
    #: 而每多打一条都在延长限流窗口。失败就快速下沉给下一个源。
    net_retries = 0
    #: 连续 1 次（也就是本次）失败就进冷却，冷却期内直接快速失败。
    #: 东财排在快照链最后一位，本来就是兜底源，快速失败比死等划算。
    circuit_fail_threshold = 1
    #: 首次冷却 5 分钟起步、上限 1 小时：给出口 IP 足够长的静默期。
    #: 固定 30s 会让我们每秒都在敲门，反而阻止它恢复。
    circuit_cooldown = 300.0
    circuit_cooldown_max = 3600.0
    #: 单次请求 6s 就放弃。东财一旦限流，等 15s 也等不到东西，
    #: 白白拖长用户这一跳的响应时间。
    request_timeout = 6.0

    BASE = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    #: 东财会校验来源，缺 Referer 会被直接断开连接
    HEADERS = {"Referer": "https://quote.eastmoney.com/"}

    FIELDS = (
        "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23"
    )

    async def fetch_quotes(self, codes: Sequence[str]) -> list[Quote]:
        out: list[Quote] = []
        errors: list[str] = []

        for chunk in self._chunks(codes):
            params = {
                #: fltt=2 -> 价格类字段返回原始浮点数（见文件头说明）
                "fltt": 2,
                "invt": 2,
                "secids": ",".join(to_eastmoney_secid(c) for c in chunk),
                "fields": self.FIELDS,
            }
            try:
                data = await self._get_json(self.BASE, params, headers=self.HEADERS)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                continue

            diff = ((data or {}).get("data") or {}).get("diff")
            if isinstance(diff, dict):          # 个别情况下会回成 map
                diff = list(diff.values())
            for d in diff or []:
                q = self._parse(d)
                if q is not None:
                    out.append(q)

        #: 一只都没取到、且每次都是**报错**（不是"这批代码东财没有"）时，
        #: 必须抛出去。返回空列表会让上层分不清"东财没有这只"还是
        #: "东财整体不可用"，聚合器就没法正确下沉到其它源。
        if codes and not out and errors:
            raise AdaptorError(f"[eastmoney] 全部取数失败：{errors[0]}")

        return out

    def _parse(self, d: dict) -> Quote | None:
        raw = str(d.get("f12") or "").strip()
        if not raw:
            return None

        #: f13: 1=沪 0=深。拼成 `1.600000` 再交给 normalize_code，
        #: 比按号段猜市场可靠（北交所/新代码段不会猜错）。
        market = d.get("f13")
        code = normalize_code(
            f"{int(market)}.{raw}" if market in (0, 1, "0", "1") else raw
        )

        price = safe_float(d.get("f2"))
        pre_close = safe_float(d.get("f18"))
        change_pct = safe_float(d.get("f3"))

        #: 自洽校验：涨跌幅就是由昨收和现价算出来的，三者必然对得上。
        #: 对不上只可能是字段理解错了（例如 fltt 没生效、字段编号变了）。
        #: 这种时候宁可报错让聚合器换源，也绝不吐一份看起来像样、
        #: 其实价格是放大 100 倍的脏数据出去。
        if price > 0 and pre_close > 0:
            expect = pre_close * (1 + change_pct / 100)
            if abs(price - expect) / price > 0.01:
                raise AdaptorError(
                    f"[eastmoney] 字段自洽校验不通过 {code}："
                    f"现价 {price} 昨收 {pre_close} 涨跌幅 {change_pct}% "
                    f"(期望 {expect:.4f})，疑似字段编号或 fltt 语义有变"
                )

        return Quote(
            source=self.name,
            code=code,
            name=str(d.get("f14") or ""),
            price=price,
            pre_close=pre_close,
            open=safe_float(d.get("f17")),
            high=safe_float(d.get("f15")),
            low=safe_float(d.get("f16")),
            volume=safe_int(d.get("f5")) * 100,      # 手 -> 股
            amount=safe_float(d.get("f6")),          # 元
            change=safe_float(d.get("f4")),
            change_pct=change_pct,
            turnover_rate=safe_float(d.get("f8")) or None,
            vol_ratio=safe_float(d.get("f10")) or None,
            circ_mv=safe_float(d.get("f21")) / 1e8 if d.get("f21") else None,
            total_mv=safe_float(d.get("f20")) / 1e8 if d.get("f20") else None,
            pe=safe_float(d.get("f9")) or None,
            pb=safe_float(d.get("f23")) or None,
        )

    async def fetch_kline(
        self, code: str, period: str = "day", limit: int = 100, adjust: str = "qfq"
    ) -> list[KlineBar]:
        """日/周/月 K 线。

        ⚠️ 2026-09-29 实测：这个端点在**干净出口 IP** 上也拿不到响应
        （连官网 JS 里用的原样参数、带 cb/ut/beg/smplmt 那套都试过），
        大概率已被东财下线。目前东财在 K 线链里排在 tdx、hithink 之后，
        正常流量根本走不到这里，所以先原样保留 —— 万一哪天恢复，
        挂回去就能用；真恢复不了，也只是体检面板上一直显示"异常"。
        """
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
