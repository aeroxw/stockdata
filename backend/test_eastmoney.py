"""东方财富适配器测试（离线，不依赖东财可达）。

背景：2026-09-29 发现老的单只快照端点 `/api/qt/stock/get` 已被东财下线，
换成批量端点 `/api/qt/ulist.np/get`。这个测试用**当时抓到的真实响应做夹具**，
把字段映射钉死 —— 以后谁再改解析逻辑，跑一遍就知道有没有理解错。

用法：python -u test_eastmoney.py
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from app.adaptors.base import AdaptorError  # noqa: E402
from app.adaptors.eastmoney import EastmoneyAdaptor  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}  {detail}")


#: 真实响应夹具（2026-09-29 盘中，fltt=2，已人工逐项核对）
REAL = {
    "rc": 0, "rt": 11, "svr": 177617589, "lt": 1, "full": 1,
    "data": {
        "total": 3,
        "diff": [
            {"f2": 9.18, "f3": 0.22, "f4": 0.02, "f5": 805097, "f6": 739740988.0,
             "f7": 1.75, "f8": 0.24, "f9": 4.94, "f10": 1.18, "f12": "600000",
             "f13": 1, "f14": "浦发银行", "f15": 9.25, "f16": 9.09, "f17": 9.15,
             "f18": 9.16, "f20": 305747595594, "f21": 305747595594, "f23": 0.41},
            {"f2": 11.35, "f3": 0.44, "f4": 0.05, "f5": 690979, "f6": 784632823.03,
             "f7": 1.15, "f8": 0.36, "f9": 4.29, "f10": 0.81, "f12": "000001",
             "f13": 0, "f14": "平安银行", "f15": 11.41, "f16": 11.28, "f17": 11.3,
             "f18": 11.3, "f20": 220257171547, "f21": 220254524648, "f23": 0.48},
            {"f2": 1235.58, "f3": -0.67, "f4": -8.3, "f5": 26366,
             "f6": 3260057865.0, "f7": 1.21, "f8": 0.21, "f9": 17.35, "f10": 0.94,
             "f12": "600519", "f13": 1, "f14": "贵州茅台", "f15": 1245.87,
             "f16": 1230.88, "f17": 1244.6, "f18": 1243.88, "f20": 1544575824564,
             "f21": 1544575824564, "f23": 6.15},
        ],
    },
}


class StubEM(EastmoneyAdaptor):
    """把网络层换成夹具，专注验证解析与分批。"""

    def __init__(self, payload, error: Exception | None = None) -> None:
        super().__init__()
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []

    async def _get_json(self, url, params=None, headers=None):  # type: ignore[override]
        self.calls.append({"url": url, "params": dict(params or {})})
        if self.error:
            raise self.error
        return self.payload


def main() -> None:
    em = EastmoneyAdaptor()

    # ---------------- 1. 主路径：真实响应 -> Quote ----------------
    print("\n[1] 真实响应解析（字段映射）")
    qs = [em._parse(d) for d in REAL["data"]["diff"]]
    check("解析出 3 只", len(qs) == 3, str(len(qs)))
    a = qs[0]
    assert a is not None and a.code == "600000.SH"
    check("代码 + 市场映射 f13=1 -> SH", a.code == "600000.SH" and qs[1].code == "000001.SZ",
          f"{a.code} / {qs[1].code}")
    check("名称 f14", a.name == "浦发银行", a.name)
    check("现价 f2", a.price == 9.18, str(a.price))
    check("昨收 f18", a.pre_close == 9.16, str(a.pre_close))
    check("涨跌幅 f3", a.change_pct == 0.22, str(a.change_pct))
    check("涨跌额 f4", a.change == 0.02, str(a.change))
    check("最高/最低/今开 f15/f16/f17",
          (a.high, a.low, a.open) == (9.25, 9.09, 9.15), f"{a.high}/{a.low}/{a.open}")
    check("成交量 f5 手 -> 股", a.volume == 805097 * 100, str(a.volume))
    check("成交额 f6 元", a.amount == 739740988.0, str(a.amount))
    check("换手率 f8", a.turnover_rate == 0.24, str(a.turnover_rate))
    check("量比 f10", a.vol_ratio == 1.18, str(a.vol_ratio))
    check("市盈率 f9", a.pe == 4.94, str(a.pe))
    check("市净率 f23", a.pb == 0.41, str(a.pb))
    check("流通市值 f21 元 -> 亿", round(a.circ_mv or 0, 2) == round(305747595594 / 1e8, 2),
          str(a.circ_mv))
    check("总市值 f20 元 -> 亿", round(a.total_mv or 0, 2) == round(305747595594 / 1e8, 2),
          str(a.total_mv))
    check("茅台负涨跌幅", qs[2] is not None and qs[2].change_pct == -0.67,
          str(qs[2].change_pct if qs[2] else None))

    # ---------------- 2. 自洽校验：字段理解错了必须报错 ----------------
    print("\n[2] 自洽校验（防止 fltt 失效时吐放大 100 倍的脏数据）")
    bad = {"f2": 918.0, "f3": 0.22, "f12": "600000", "f13": 1, "f14": "浦发银行", "f18": 9.16}
    try:
        em._parse(bad)
        check("价格放大 100 倍被拦截", False, "没有抛错")
    except AdaptorError as exc:
        check("价格放大 100 倍被拦截", "自洽校验不通过" in str(exc), str(exc)[:60])

    no_code = {"f2": 1.0, "f18": 1.0, "f3": 0.0}
    check("缺 f12 时跳过而不是造一条脏数据", em._parse(no_code) is None)

    # ---------------- 3. 批量请求：一次 50 只 ----------------
    print("\n[3] 批量与分批")
    stub = StubEM(REAL)
    codes = [f"{600000 + i}.SH" for i in range(120)]
    out = asyncio.run(stub.fetch_quotes(codes))
    check("120 只 -> 3 批（batch_size=50）", len(stub.calls) == 3, str(len(stub.calls)))
    check("请求打到 ulist.np/get",
          all(c["url"] == EastmoneyAdaptor.BASE for c in stub.calls), stub.calls[0]["url"])
    check("带 fltt=2（否则价格是放大整数）",
          all(c["params"].get("fltt") == 2 for c in stub.calls),
          str(stub.calls[0]["params"].get("fltt")))
    check("secids 用 1./0. 前缀",
          stub.calls[0]["params"]["secids"].startswith("1.600000"),
          stub.calls[0]["params"]["secids"][:40])
    check("三批都回同一份夹具 -> 解析条数 9", len(out) == 9, str(len(out)))

    # ---------------- 4. 限流：全失败必须抛错，不能静默返回空 ----------------
    print("\n[4] 上游限流（全失败要抛错，聚合器才能下沉到其它源）")
    dead = StubEM(None, error=AdaptorError("上游未返回任何响应就掐断了连接"))
    try:
        asyncio.run(dead.fetch_quotes(["600000.SH"]))
        check("全失败抛 AdaptorError", False, "返回了空列表")
    except AdaptorError as exc:
        check("全失败抛 AdaptorError", "eastmoney" in str(exc), str(exc)[:60])

    # ---------------- 5. diff 是 dict 形态 ----------------
    print("\n[5] 上游偶尔把 diff 回成 map")
    as_map = {"data": {"total": 1, "diff": {"0": REAL["data"]["diff"][0]}}}
    stub2 = StubEM(as_map)
    out2 = asyncio.run(stub2.fetch_quotes(["600000.SH"]))
    check("diff 为 dict 也能解析", len(out2) == 1 and out2[0].code == "600000.SH", str(len(out2)))

    # ---------------- 6. 参数护栏 ----------------
    print("\n[6] 限流相关参数")
    check("不重试（重试只会延长限流窗口）", EastmoneyAdaptor.net_retries == 0,
          str(EastmoneyAdaptor.net_retries))
    check("已改成批量端点", EastmoneyAdaptor.support_batch is True)
    check("冷却下限 >= 300s", EastmoneyAdaptor.circuit_cooldown >= 300,
          str(EastmoneyAdaptor.circuit_cooldown))
    check("单次超时收紧到 6s（别拖累调用方）",
          EastmoneyAdaptor.request_timeout <= 8, str(EastmoneyAdaptor.request_timeout))

    ok = sum(1 for _, v, _ in RESULTS if v)
    print(f"\n{'=' * 60}\n结果：{ok}/{len(RESULTS)} 通过\n{'=' * 60}")
    for n, v, d in RESULTS:
        if not v:
            print(f"  失败：{n}  {d}")
    sys.exit(0 if ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
