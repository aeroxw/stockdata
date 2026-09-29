"""临时验证：base.py 的网络重试是否按预期工作。

场景 A：第 1 次 RemoteProtocolError、第 2 次成功 -> 应返回数据，ok=1 fail=0
场景 B：连续 3 次 RemoteProtocolError         -> 应抛 AdaptorError，fail=1
场景 C：400 Bad Request                       -> 不重试，直接失败（只调 1 次）
场景 D：503                                    -> 应重试
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import httpx  # noqa: E402

from app.adaptors.base import AdaptorError, BaseAdaptor  # noqa: E402


class Dummy(BaseAdaptor):
    name = "dummy"

    async def fetch_quotes(self, codes):
        return []


def make_client(script):
    """script: 每次调用返回 (result | Exception)

    注意：aclose() **不能**把 is_closed 置 True —— 那样 BaseAdaptor.client
    这个 property 会在下次访问时重建一个真实的 httpx.AsyncClient，测试就
    真的发出网络请求了（沙箱里会拿到代理的 502）。这里只计数，模拟
    "关掉旧连接、拿到一条新连接"的效果。
    """
    calls = {"n": 0, "closed": 0}

    class Resp:
        status_code = 200
        text = "hello"
        encoding = "utf-8"

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class C:
        is_closed = False

        async def get(self, url, params=None, headers=None):
            i = calls["n"]
            calls["n"] += 1
            item = script[min(i, len(script) - 1)]
            if isinstance(item, Exception):
                raise item
            return Resp()

        async def aclose(self):
            calls["closed"] += 1

    return C(), calls


async def main():
    err = httpx.RemoteProtocolError("Server disconnected without sending a response")
    ok = 0
    fail = 0

    # A: 第1次断连，第2次成功
    d = Dummy()
    d.retry_backoff = 0.01
    c, calls = make_client([err, "ok"])
    d._client = c
    try:
        r = await d._get("http://x")
        print(f"A 重试后成功: 返回={r!r} 调用次数={calls['n']} stats={d.stats}")
        ok += (r == "hello" and calls["n"] == 2 and d.stats["ok"] == 1 and d.stats["fail"] == 0)
    except Exception as e:
        print("A 失败", e)
        fail += 1

    # B: 一直断连 -> 抛错，且只调 net_retries+1 = 3 次
    d = Dummy()
    d.retry_backoff = 0.01
    c, calls = make_client([err])
    d._client = c
    try:
        await d._get("http://x")
        print("B 未抛错 —— 不符合预期")
        fail += 1
    except AdaptorError as e:
        good = calls["n"] == 3 and d.stats["fail"] == 1 and d.stats["ok"] == 0
        print(f"B 重试耗尽后抛错: 调用次数={calls['n']} stats={d.stats} 判定={'OK' if good else 'BAD'}")
        ok += good

    # C: 400 -> 不重试，只调 1 次
    d = Dummy()
    d.retry_backoff = 0.01
    r400 = httpx.Response(400, request=httpx.Request("GET", "http://x"))
    c, calls = make_client([httpx.HTTPStatusError("bad", request=r400.request, response=r400)])
    d._client = c
    try:
        await d._get("http://x")
        fail += 1
        print("C 未抛错")
    except AdaptorError:
        good = calls["n"] == 1
        print(f"C 400 不重试: 调用次数={calls['n']} 判定={'OK' if good else 'BAD'}")
        ok += good

    # D: 503 -> 应重试
    d = Dummy()
    d.retry_backoff = 0.01
    r503 = httpx.Response(503, request=httpx.Request("GET", "http://x"))
    c, calls = make_client([httpx.HTTPStatusError("busy", request=r503.request, response=r503), "ok"])
    d._client = c
    try:
        await d._get_json("http://x")
        good = calls["n"] == 2
        print(f"D 503 重试后成功: 调用次数={calls['n']} 判定={'OK' if good else 'BAD'}")
        ok += good
    except AdaptorError as e:
        print("D 失败", e)
        fail += 1

    print(f"\n结果: 通过={ok} 失败={fail}")
    return 1 if fail else 0


sys.exit(asyncio.run(main()))
