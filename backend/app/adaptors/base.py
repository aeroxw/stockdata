"""数据源适配层：统一模型 + 抽象基类。

6 家数据源的返回格式天差地别（GBK 的 ~ 分隔串、JSON 的 f43/f44 编码字段、
REST 信封……），全部收敛到同一个 Quote / KlineBar 模型后，上层接口与采集器
不再关心数据来自哪家，从而实现：
  1. 主备自动切换
  2. 多源交叉校验
  3. 新增数据源只需新增一个适配器文件
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import httpx
from pydantic import BaseModel, Field

CN_TZ = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 统一模型


class Quote(BaseModel):
    """实时行情快照（统一输出）。"""

    source: str
    code: str                       # 统一代码 600000.SH / 000001.SZ
    name: str = ""
    price: float = 0.0
    pre_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: int = 0                 # 成交量（股）
    amount: float = 0.0             # 成交额（元）
    change: float = 0.0
    change_pct: float = 0.0
    turnover_rate: float | None = None   # 换手率 %
    vol_ratio: float | None = None       # 量比
    circ_mv: float | None = None         # 流通市值（亿元）
    total_mv: float | None = None        # 总市值（亿元）
    pe: float | None = None
    pb: float | None = None
    bid: list[list[float]] = Field(default_factory=list)  # [[价,量],...] 买五档
    ask: list[list[float]] = Field(default_factory=list)  # 卖五档
    ts: datetime = Field(default_factory=lambda: datetime.now(CN_TZ))


class KlineBar(BaseModel):
    """K 线（统一输出）。"""

    source: str
    code: str
    date: str                       # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    amount: float = 0.0


# ---------------------------------------------------------------- 代码规范化


def normalize_code(raw: str) -> str:
    """把各家代码统一成 `600000.SH` / `000001.SZ` 形式。

    支持输入：600000 / sh600000 / 600000.SH / 1.600000
    """
    s = (raw or "").strip()
    if not s:
        return ""

    # 东财 secid 形式：1.600000 / 0.000001
    if "." in s and s.split(".")[0] in ("0", "1"):
        market, num = s.split(".", 1)
        return f"{num}.{'SH' if market == '1' else 'SZ'}"

    low = s.lower()
    for prefix, suffix in (("sh", "SH"), ("sz", "SZ"), ("bj", "BJ")):
        if low.startswith(prefix):
            return f"{s[len(prefix):]}.{suffix}"
        if low.endswith("." + prefix):
            return f"{s[:-3]}.{suffix}"

    # 纯 6 位数字：按号段推断市场
    num = s[:6]
    if num[0] in "56" or num.startswith("11") or num.startswith("9"):
        return f"{num}.SH"
    if num[0] in "04" or num.startswith("12") or num.startswith("8") or num.startswith("43"):
        return f"{num}.SZ"
    return f"{num}.SH"


def split_market(code: str) -> tuple[str, str]:
    """600000.SH -> ('SH', '600000')"""
    if "." in code:
        num, mkt = code.split(".", 1)
        return mkt.upper(), num
    return "SH", code


def to_tencent_code(code: str) -> str:
    """600000.SH -> sh600000"""
    mkt, num = split_market(code)
    return f"{mkt.lower()}{num}"


def to_eastmoney_secid(code: str) -> str:
    """600000.SH -> 1.600000"""
    mkt, num = split_market(code)
    return f"{1 if mkt == 'SH' else 0}.{num}"


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "" or v == "-":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "" or v == "-":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 抽象基类


class AdaptorError(Exception):
    """适配器取数失败。"""


class BaseAdaptor(ABC):
    """所有数据源适配器的基类。"""

    name: str = "base"
    #: 是否支持批量取快照
    support_batch: bool = True
    #: 单批最大代码数（实测上限，超过会被上游限流）
    batch_size: int = 50
    #: 并发上限
    concurrency: int = 6
    #: 网络层重试次数（不含首次）。仅重试「连接被对端掐断」这类可恢复错误，
    #: 4xx 不重试 —— 那是请求本身有问题，重试只会白等。
    net_retries: int = 2
    #: 重试退避基数（秒）：第 n 次重试 sleep base * n
    retry_backoff: float = 0.35
    #: 是否复用 keep-alive 连接。
    #:
    #: 排查东财时一度怀疑是「响应后立刻断链、连接池却复用死连接」，但把每次
    #: 请求都换成全新连接后故障依旧，实测才确认真正原因是**上游限流**（见下）。
    #: 所以这里保持 True —— 频繁新建连接反而会产生大量握手，更容易被限流。
    keep_alive: bool = True
    #: 同一数据源两次请求之间的最小间隔（秒）。0 表示不限。
    #:
    #: 东财的限流策略是「配额用完就在 TCP 层直接掐断连接」
    #: （RemoteProtocolError: Server disconnected without sending a response），
    #: 且不返回 429 —— 靠状态码根本判断不出来。实测同一出口 IP：
    #:   间隔 0.2s -> 0/8   间隔 1s -> 0/8   间隔 3s -> 1/6
    #:   **静默 20s 后 -> 4/5**
    #: 即一旦触发，短时间内怎么重试都没用，必须等一段静默期。
    #: 节流不是为了绕过限流，而是别把有限的配额一次性打光。
    min_interval: float = 0.0
    #: 连续失败达到该次数后进入冷却：冷却期内直接快速失败，不再发网络请求。
    #: 目的不是"治好"上游，而是**别让用户干等** —— 否则每次调用都要
    #: 吃满 15s 超时 × 重试次数，聚合器也没法及时切到备用源。
    circuit_fail_threshold: int = 0      # 0 = 不启用熔断
    circuit_cooldown: float = 30.0       # 首次冷却时长（秒）
    #: 冷却时长上限。连续失败越多冷却越久（指数退避），
    #: 但不能无限涨，否则上游早就恢复了我们还傻等着。
    circuit_cooldown_max: float = 600.0

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # 简单健康统计，供后台「数据源监控」展示
        self.stats = {"ok": 0, "fail": 0, "last_error": "", "last_latency_ms": 0}
        # 节流 / 熔断的运行态
        self._throttle_lock = asyncio.Lock()
        self._last_req_at = 0.0
        self._consec_fail = 0
        self._cooldown_until = 0.0

    @property
    def _in_cooldown(self) -> bool:
        return self._cooldown_until > time.perf_counter()

    async def _throttle(self) -> None:
        """保证距上次请求至少 min_interval 秒（串行化，避免并发一起冲）。"""
        if self.min_interval <= 0:
            return
        async with self._throttle_lock:
            now = time.perf_counter()
            wait = self._last_req_at + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_req_at = time.perf_counter()

    # -- HTTP 客户端（懒加载，复用连接） --
    def _new_client(self) -> httpx.AsyncClient:
        # 部分上游（东财、新浪）会拒绝 python-httpx 默认 UA，这里伪装成浏览器
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if not self.keep_alive:
            headers["Connection"] = "close"
        return httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers=headers,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._new_client()
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _record(self, ok: bool, latency_ms: int, err: str = "") -> None:
        if ok:
            self.stats["ok"] += 1
        else:
            self.stats["fail"] += 1
            self.stats["last_error"] = err[:200]
        self.stats["last_latency_ms"] = latency_ms

    #: 可恢复的网络层异常。上游（尤其是东财）会毫无征兆地掐断 keep-alive 连接，
    #: 表现为 "Server disconnected without sending a response"（RemoteProtocolError）。
    #: 这类错误重试一次基本就能成功，值得救回来。
    _RETRYABLE = (
        httpx.RemoteProtocolError,   # 对端未发响应就断开 / 协议违规
        httpx.ReadError,             # 读响应体时连接断
        httpx.WriteError,            # 写请求时连接断
        httpx.ConnectError,          # 建连失败（DNS 抖动、对端拒绝）
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,           # 连接池排队超时
    )

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        """判断异常是否值得重试。

        5xx 也算可恢复（上游网关抽风），但 4xx 一律不重试 ——
        请求本身有问题，重试只会浪费时间并放大限流风险。
        """
        if isinstance(exc, httpx.HTTPStatusError):
            return 500 <= exc.response.status_code < 600
        return isinstance(exc, BaseAdaptor._RETRYABLE)

    async def _request(
        self,
        url: str,
        params: dict | None,
        headers: dict | None,
        parse: str,                  # "text" | "json"
        encoding: str | None = None,
    ) -> Any:
        """带节流 / 熔断 / 网络重试的 GET。text/json 两种解析共用同一套逻辑。"""
        #: 冷却期内直接快速失败：上游已经在限流了，再打也是白等超时。
        if self._in_cooldown:
            left = int(self._cooldown_until - time.perf_counter())
            raise AdaptorError(f"[{self.name}] 熔断冷却中（剩 {left}s），已快速失败")

        t0 = time.perf_counter()
        last: BaseException | None = None

        for attempt in range(self.net_retries + 1):
            try:
                await self._throttle()
                resp = await self.client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                if parse == "json":
                    out: Any = resp.json()
                else:
                    if encoding:
                        resp.encoding = encoding
                    out = resp.text
                self._consec_fail = 0
                self._cooldown_until = 0.0
                self._record(True, int((time.perf_counter() - t0) * 1000))
                return out
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt >= self.net_retries or not self._is_retryable(exc):
                    break
                # 关键：坏掉的是**这条 keep-alive 连接**。不关掉 client 就直接重试，
                # httpx 很可能复用同一个死连接，重试等于白做。这里强制重建。
                await self.aclose()
                await asyncio.sleep(self.retry_backoff * (attempt + 1))

        assert last is not None
        self._record(False, int((time.perf_counter() - t0) * 1000), str(last))

        #: 连续失败累计到阈值就进冷却。注意只在**重试全部耗尽**后才累加，
        #: 中途自愈的那次不算（成功分支已经清零了）。
        if self.circuit_fail_threshold > 0:
            self._consec_fail += 1
            if self._consec_fail >= self.circuit_fail_threshold:
                #: **指数退避**，这一点很关键：固定 30s 冷却会让我们每隔 30s
                #: 就去敲一次上游的门，而对东财这种"配额用完就掐连接"的限流来说，
                #: 持续请求恰恰会阻止它恢复。失败越多等越久，给上游喘息时间。
                over = self._consec_fail - self.circuit_fail_threshold
                cd = min(self.circuit_cooldown * (2 ** max(0, over)),
                         self.circuit_cooldown_max)
                self._cooldown_until = time.perf_counter() + cd
                self.stats["last_error"] = (
                    f"连续失败 {self._consec_fail} 次，冷却 {cd:.0f}s"
                )
        raise AdaptorError(f"[{self.name}] {last}") from last

    async def _get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        encoding: str | None = None,
    ) -> str:
        return await self._request(url, params, headers, parse="text", encoding=encoding)

    async def _get_json(self, url: str, params: dict | None = None,
                        headers: dict | None = None) -> Any:
        return await self._request(url, params, headers, parse="json")

    # -- 子类必须实现 --
    @abstractmethod
    async def fetch_quotes(self, codes: Sequence[str]) -> list[Quote]:
        """批量取实时快照。"""

    async def fetch_kline(
        self,
        code: str,
        period: str = "day",
        limit: int = 100,
        adjust: str = "qfq",
    ) -> list[KlineBar]:
        """取历史 K 线（可选实现）。"""
        raise NotImplementedError(f"{self.name} 不支持 K 线")

    # -- 批量分批工具 --
    def _chunks(self, codes: Sequence[str]) -> Iterable[list[str]]:
        for i in range(0, len(codes), self.batch_size):
            yield list(codes[i: i + self.batch_size])


# ---------------------------------------------------------------- 聚合器


class QuoteAggregator:
    """多源聚合：主备切换 + 交叉校验。

    usage:
        agg = QuoteAggregator([hithink, tencent, sina])
        quotes = await agg.get_quotes(codes)
    """

    def __init__(self, adaptors: list[BaseAdaptor], threshold_pct: float = 1.0) -> None:
        self.adaptors = adaptors
        #: 多源价格偏差超过该百分比时记录告警
        self.threshold_pct = threshold_pct

    async def get_quotes(
        self,
        codes: Sequence[str],
        sources: Sequence[str] | None = None,
    ) -> tuple[list[Quote], str]:
        """按优先级依次尝试，返回 (quotes, 实际命中的源名)。"""
        ordered = self.adaptors
        if sources:
            ordered = [a for a in self.adaptors if a.name in sources] or self.adaptors

        last_err: Exception | None = None
        for ad in ordered:
            try:
                quotes = await ad.fetch_quotes(codes)
                if quotes:
                    return quotes, ad.name
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        if last_err:
            raise AdaptorError(str(last_err))
        return [], ""

    async def cross_check(self, codes: Sequence[str]) -> dict[str, Any]:
        """多源交叉校验：同一代码在不同源之间的价格偏差。"""
        results: dict[str, list[Quote]] = {}
        tasks = [a.fetch_quotes(codes) for a in self.adaptors]
        outs = await asyncio.gather(*tasks, return_exceptions=True)

        for ad, out in zip(self.adaptors, outs):
            if isinstance(out, Exception) or not out:
                continue
            results[ad.name] = out

        report: dict[str, Any] = {"sources": list(results), "mismatches": []}
        names = list(results)
        if len(names) < 2:
            return report

        base = {q.code: q for q in results[names[0]]}
        for other in names[1:]:
            for q in results[other]:
                b = base.get(q.code)
                if not b or not b.price or not q.price:
                    continue
                diff = abs(q.price - b.price) / b.price * 100
                if diff > self.threshold_pct:
                    report["mismatches"].append({
                        "code": q.code,
                        "a": {"source": names[0], "price": b.price},
                        "b": {"source": other, "price": q.price},
                        "diff_pct": round(diff, 4),
                    })
        return report
