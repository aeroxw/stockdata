"""限流与缓存（Redis）。

设计原则：**Redis 不可用时降级放行，绝不能因为限流组件挂掉而拖垮整个 API**。
"""

from __future__ import annotations

import json
import logging
import time

from app.config import settings

log = logging.getLogger("stockdata.ratelimit")

_client = None
_available: bool | None = None   # None=未探测，True/False=探测结果
_last_error: str = ""
_last_probe: float = 0.0
_warned_down: bool = False   # 已就「不可用」告警过，避免重探刷屏

#: 探测失败后隔多久再试一次。
#: 原来一旦失败就永久判死 —— 但 docker compose 的 depends_on 只保证启动顺序，
#: 不保证 Redis 已就绪。容器场景下 API 很可能先起来探测失败，之后 Redis 好了也接不上，
#: 限流和黑名单就永久废了。所以这里必须留一个重试窗口。
_RETRY_INTERVAL = 30.0
#: 连接/读写超时必须显式设短，否则 Redis 主机不可达时每次请求都会卡很久
_CONNECT_TIMEOUT = 2.0
_SOCKET_TIMEOUT = 2.0


def _get(force: bool = False):
    """惰性连接 Redis。

    force=True 用于后台「重新检测」按钮：无视重试窗口立即重探。
    """
    global _client, _available, _last_error, _last_probe, _warned_down

    now = time.time()
    # 已知不可用且还在重试窗口内 —— 直接降级，别再卡一次网络
    if not force and _available is False and (now - _last_probe) < _RETRY_INTERVAL:
        return None
    # 已知可用且没要求重探 —— 直接用
    if not force and _client is not None:
        return _client

    try:
        import redis

        client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=_CONNECT_TIMEOUT,
            socket_timeout=_SOCKET_TIMEOUT,
        )
        client.ping()
        _client, _available = client, True
        _last_error, _last_probe = "", now
        if _warned_down:      # 恢复后提示一次，方便确认自动重连生效
            log.info("Redis 已恢复连接")
            _warned_down = False
        return _client
    except Exception as exc:  # noqa: BLE001
        was_up = _available is True
        _available, _client = False, None
        _last_error, _last_probe = str(exc), now
        # 只在「刚变不可用」或「首次探测」时告警：
        # 否则 Redis 压根没部署时，每 30 秒一次重探会把日志刷满
        if was_up or not _warned_down:
            log.warning("Redis 不可用，限流与缓存降级: %s", exc)
            _warned_down = True
        return None


def is_available() -> bool:
    _get()
    return bool(_available)


def redis_status(force: bool = False) -> dict:
    """给后台展示的 Redis 状态详情（URL 会脱敏，避免泄露密码）。"""
    _get(force=force)
    url = settings.REDIS_URL
    if "//" in url and "@" in url.split("//", 1)[1]:
        scheme, rest = url.split("//", 1)
        url = scheme + "//***:***@" + rest.split("@", 1)[1]
    return {
        "available": bool(_available),
        "url": url,
        "error": _last_error,
        "last_probe": _last_probe,
        "retry_interval_s": _RETRY_INTERVAL,
    }


def check_rate_limit(key_id: int, limit: int) -> tuple[bool, int]:
    """按分钟滑动窗口限流。返回 (是否放行, 剩余配额)。"""
    r = _get()
    if r is None:
        return True, limit          # 降级放行

    now = int(time.time())
    bucket = now // 60
    rk = f"rl:{key_id}:{bucket}"
    try:
        pipe = r.pipeline()
        pipe.incr(rk)
        pipe.expire(rk, 120)
        used = pipe.execute()[0]
        remain = max(limit - used, 0)
        return used <= limit, remain
    except Exception as exc:  # noqa: BLE001
        log.warning("限流检查失败，放行: %s", exc)
        return True, limit


#: Redis 不可用时的进程内兜底缓存，避免「全市场快照」这类重查询每次都打到数据源
_MEMO: dict[str, tuple[float, object]] = {}
_MEMO_MAX = 256


def _memo_get(key: str):
    hit = _MEMO.get(key)
    if not hit:
        return None
    expire, val = hit
    if time.time() > expire:
        _MEMO.pop(key, None)
        return None
    return val


def _memo_set(key: str, value, ttl: int) -> None:
    if len(_MEMO) >= _MEMO_MAX:          # 简单的容量保护
        oldest = min(_MEMO, key=lambda k: _MEMO[k][0])
        _MEMO.pop(oldest, None)
    _MEMO[key] = (time.time() + ttl, value)


def cache_get(key: str):
    r = _get()
    if r is None:
        return _memo_get(key)
    try:
        raw = r.get(key)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def cache_set(key: str, value, ttl: int) -> None:
    r = _get()
    if r is None:
        _memo_set(key, value, ttl)
        return
    try:
        r.setex(key, ttl, json.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        pass


def incr_calls(key_id: int) -> None:
    """累计调用次数（用于后台统计）。"""
    r = _get()
    if r is None:
        return
    try:
        r.incr(f"calls:{key_id}")
    except Exception:  # noqa: BLE001
        pass


def blacklist_key(key_hash: str) -> None:
    """吊销密钥后加入黑名单，即时生效。"""
    r = _get()
    if r is None:
        return
    try:
        r.setex(f"revoked:{key_hash}", 86400 * 30, "1")
    except Exception:  # noqa: BLE001
        pass


def is_blacklisted(key_hash: str) -> bool:
    r = _get()
    if r is None:
        return False
    try:
        return bool(r.exists(f"revoked:{key_hash}"))
    except Exception:  # noqa: BLE001
        return False
