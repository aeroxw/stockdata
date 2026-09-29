"""端到端闭环冒烟测试：注册 -> 登录 -> 创建 API Key -> 用 Key 调数据接口。"""

import json
import random
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:9850"
def _uniq(prefix):
    """时间戳+随机后缀：同一秒内反复跑测试也不会撞邮箱。"""
    return f"{prefix}{int(time.time())}{random.randint(1000, 9999)}@stockdata.dev"


EMAIL = _uniq("e2e")
PWD = "StockData@2024"

_step = [0]


def step(msg):
    _step[0] += 1
    print(f"\n[{_step[0]}] {msg}", flush=True)


#: 直连本机时必须绕过环境里的 HTTP_PROXY，否则会被代理拦下返回 502
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(path, method="GET", body=None, token=None, apikey=None, timeout=40):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if apikey:
        r.add_header("X-API-Key", apikey)
    # 沙箱里偶发 ConnectionReset，重试几次避免误报
    last: Exception | None = None
    for attempt in range(4):
        try:
            with _OPENER.open(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw[:600]}
        except (ConnectionResetError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.6 * (attempt + 1))
    raise last  # type: ignore[misc]


def ok(cond, label, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (" " + str(extra) if extra else ""), flush=True)
    if not cond:
        sys.exit(1)


# ---------------------------------------------------------------- 0. 健康检查
step("健康检查 /health")
for i in range(40):
    try:
        st, d = req("/health", timeout=3)
        if st == 200:
            break
    except Exception:
        pass
    time.sleep(0.5)
else:
    print("服务未起来")
    sys.exit(1)
print("   ", d)

# ---------------------------------------------------------------- 1. 注册
step(f"注册 {EMAIL}")
st, d = req("/api/v1/auth/register", "POST", {"email": EMAIL, "password": PWD})
#: 注册是"创建型"接口：第一次可能已经成功了、只是响应在回程丢了，req() 重试
#: 后拿到 400「该邮箱已注册」。这恰恰说明账号已在库里 —— 正是我们要的结果，
#: 不能算失败。真正的判定交给后面能不能登录。
ok(st in (200, 400), "注册", d)
if st == 200:
    user = d["data"]
    ok(user.get("is_admin") is True or user.get("is_admin") is False, "返回用户信息", user)
else:
    ok("已注册" in str(d.get("detail") or ""), "返回用户信息（账号已存在）", d)

# ---------------------------------------------------------------- 2. 重复注册应被拒
step("重复注册应返回 400")
st, d = req("/api/v1/auth/register", "POST", {"email": EMAIL, "password": PWD})
ok(st == 400, "拒绝重复邮箱", d.get("detail"))

# ---------------------------------------------------------------- 3. 弱密码应被拒
step("弱密码应返回 422")
st, _ = req("/api/v1/auth/register", "POST", {"email": "x@y.com", "password": "123"})
ok(st == 422, "密码长度校验", "")

# ---------------------------------------------------------------- 4. 登录
step("登录")
st, d = req("/api/v1/auth/login", "POST", {"email": EMAIL, "password": PWD})
ok(st == 200, "登录成功", "")
token = d["data"]["access_token"]
ok(bool(token), "拿到 access_token", token[:24] + "...")

# ---------------------------------------------------------------- 5. 错误密码
step("错误密码应返回 401")
st, _ = req("/api/v1/auth/login", "POST", {"email": EMAIL, "password": "wrongpass123"})
ok(st == 401, "拒绝错误密码", "")

# ---------------------------------------------------------------- 6. /me
step("GET /auth/me")
st, d = req("/api/v1/auth/me", token=token)
ok(st == 200 and d["data"]["email"] == EMAIL, "当前用户", d.get("data"))

# ---------------------------------------------------------------- 7. 创建 API Key
step("创建 API Key")
st, d = req("/api/v1/apikey", "POST",
            {"name": "e2e-test", "scopes": ["quote", "kline"], "rate_limit": 60},
            token=token)
ok(st == 200, "创建成功", "")
key = d["data"]
secret = key.get("secret")
ok(bool(secret) and secret.startswith("sk_live_"), "返回明文密钥", secret)
ok(key["masked"].endswith("****"), "列表字段已脱敏", key["masked"])

# ---------------------------------------------------------------- 8. Key 列表不含明文
step("Key 列表不应包含明文")
st, d = req("/api/v1/apikey", token=token)
ok(st == 200, "列表成功", "")
ok(all("secret" not in k for k in d["data"]), "无 secret 泄露", "")
ok(any(k["id"] == key["id"] for k in d["data"]), "新 key 在列表中", "")

# ---------------------------------------------------------------- 9. 用 Key 调行情
step("用 API Key 调 /quote")
st, d = req("/api/v1/quote?codes=600519.SH,000001.SH,300750.SZ", apikey=secret)
ok(st == 200, "鉴权通过(X-API-Key)", d.get("meta", {}).get("source"))
rows = d["data"]
ok(len(rows) >= 1, f"返回 {len(rows)} 条行情", "")
for r in rows:
    print(f"     {r['code']:<12} {r['name']:<8} {r['price']:>10.2f} "
          f"{r['change_pct']:>+7.2f}%")

# ---------------------------------------------------------------- 10. 无 Key 应被拒
step("无 API Key 调 /quote 应 401/403")
st, d = req("/api/v1/quote?codes=600519.SH")
ok(st in (401, 403), "拒绝匿名访问", st)

# ---------------------------------------------------------------- 11. 假 Key 应被拒
step("伪造 API Key 应被拒")
st, d = req("/api/v1/quote?codes=600519.SH", apikey="sk_live_fake_" + "x" * 28)
ok(st in (401, 403), "拒绝伪造密钥", st)

# ---------------------------------------------------------------- 12. 公开行情（首页用）
step("公开行情 /public/quote（免登录）")
st, d = req("/api/v1/public/quote")
ok(st == 200, "公开接口可用", d.get("meta", {}).get("source"))
for r in d["data"][:5]:
    print(f"     {r['code']:<12} {r['name']:<8} {r['price']:>10.2f} {r['change_pct']:>+7.2f}%")

# ---------------------------------------------------------------- 13. 数据源状态
step("GET /sources")
st, d = req("/api/v1/sources", token=token)
ok(st == 200, "数据源列表", "")
for s in d["data"]:
    print(f"     {s}")

# ---------------------------------------------------------------- 14. K 线
step("用 API Key 调 /kline")
st, d = req("/api/v1/kline?code=600519.SH&period=day&limit=5", apikey=secret, timeout=60)
ok(st == 200, "K线返回", d.get("meta", {}).get("source"))
bars = d["data"] or []
ok(len(bars) > 0, f"K线 {len(bars)} 根", "")
for b in bars[:5]:
    print(f"     {b['date']}  O{b['open']:.2f} H{b['high']:.2f} "
          f"L{b['low']:.2f} C{b['close']:.2f}")

# ---------------------------------------------------------------- 14b. query 传 key
step("用 ?apikey= 传密钥（浏览器调试）")
st, d = req(f"/api/v1/quote?codes=600519.SH&apikey={secret}")
ok(st == 200, "query 方式鉴权通过", d.get("meta", {}).get("source"))

# ---------------------------------------------------------------- 15. 吊销 Key
step("吊销 API Key")
st, d = req(f"/api/v1/apikey/{key['id']}", "DELETE", token=token)
ok(st == 200, "吊销成功", d.get("msg"))

step("吊销后 Key 立即失效")
st, d = req("/api/v1/quote?codes=600519.SH", apikey=secret)
ok(st in (401, 403), "已失效", st)

print("\n" + "=" * 60)
print("全部通过：注册 -> 登录 -> 建 Key -> 取数 -> 吊销 闭环 OK")
print("=" * 60)
