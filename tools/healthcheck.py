"""StockData 全功能 + 六家数据源体检。

用法：python -u tools/healthcheck.py
默认打 NAS（NAS_INTERNAL_IP:9850），加 --local 打本机。

设计要点：
  * 六家数据源**分工不同**，不能用同一个接口测：
      快照链  tencent -> hithink -> sina -> eastmoney   （/quote?source=）
      K线链   tdx     -> hithink -> eastmoney           （/kline?source=）
      特色    hithink + kaipanla                        （/special/）
    拿 /quote 去测 tdx 或 kaipanla 必然失败 —— 它们压根不在快照链里。
  * 行情有 3 秒缓存，同一组 codes 连着测会命中缓存，
    所以每家换一组不同的股票，保证打到真实网络。
  * 数据源"ok=0 且 fail=0"表示**从未被调用**，不等于故障，别误判。
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:9850/api/v1" if "--local" in sys.argv else "http://NAS_INTERNAL_IP:9850/api/v1"
EMAIL = "admin@example.com"
PWD = "***REMOVED***"

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PASS = FAIL = 0
#: 上游限流等外部因素导致的"不算失败但要知道"的项
WARNS: list[str] = []
TOK = None


def req(p, m="GET", b=None, tok=None, timeout=45):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(BASE + p, data=d, method=m)
    r.add_header("Content-Type", "application/json")
    if tok:
        r.add_header("Authorization", "Bearer " + tok)
    last = None
    for i in range(3):
        try:
            with op.open(r, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, {"html": len(raw)}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw[:200]}
        except OSError as e:
            last = e
            if i < 2:
                time.sleep(1.0)
                continue
            return 0, {"raw": f"网络错误 {last}"}


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}  {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def sect(t):
    print(f"\n--- {t} ---")


print("=" * 66)
print(f"StockData 全功能体检   {BASE}")
print("=" * 66)

# ---------------------------------------------------------------- 1. 登录
sect("1. 账号")
st, js = req("/auth/login", "POST", {"email": EMAIL, "password": PWD})
TOK = (js.get("data") or {}).get("access_token")
ck("管理员登录", st == 200 and bool(TOK), f"{st}")
if not TOK:
    print("登录失败，终止")
    sys.exit(1)
st, js = req("/auth/me", tok=TOK)
d = js.get("data") or {}
ck("当前用户是管理员", d.get("is_admin") is True,
   f"{d.get('email')} tier={d.get('tier')}")

# ---------------------------------------------------------------- 2. 六家数据源
#: 每家用一组不同代码，避开 3 秒缓存，确保打到真实网络
SNAP = [
    ("tencent",   "腾讯",     "600519.SH,000001.SZ"),
    ("hithink",   "同花顺",   "600000.SH,000002.SZ"),
    ("sina",      "新浪",     "601318.SH,000858.SZ"),
    ("eastmoney", "东方财富", "600036.SH,002594.SZ"),
]
#: 东财的限流特征串。命中这些说明是**上游在限流**（外部环境），
#: 不是我们的代码坏了 —— 记 WARN 而不是 FAIL，否则每次体检都被它带偏。
#: 它排在快照链最后一位，限流时聚合器会自动下沉，取数不受影响。
EM_LIMIT_HINTS = ("熔断冷却", "Server disconnected", "限流", "RemoteProtocol")


def is_upstream_limited(js: dict) -> bool:
    blob = str(js)
    return any(h in blob for h in EM_LIMIT_HINTS)


def ck_src(name, cond, extra, js, src):
    """数据源断言：东财被上游限流时降级为 WARN，不计入失败。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name} {extra}")
    elif src == "eastmoney" and is_upstream_limited(js):
        WARNS.append(name)
        print(f"  [WARN] {name} {extra}  —— 上游限流（已知，非本站故障）")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


sect("2. 六家数据源 —— 实时快照链")
for src, cn, codes in SNAP:
    st, js = req(f"/quote?codes={codes}&source={src}", tok=TOK)
    items = js.get("data") or []
    got = st == 200 and len(items) > 0 and items[0].get("price") is not None
    px = items[0].get("price") if items else None
    ck_src(f"{cn}({src}) 实时行情", got, f"{st} 共{len(items)}只 首条价={px}", js, src)

sect("3. 六家数据源 —— K 线链")
for src, cn in [("tdx", "通达信TDX"), ("hithink", "同花顺"), ("eastmoney", "东方财富")]:
    st, js = req(f"/kline?code=600519.SH&period=day&limit=5&source={src}", tok=TOK)
    rows = js.get("data") or []
    got = st == 200 and len(rows) > 0
    last_close = rows[-1].get("close") if rows else None
    ck_src(f"{cn}({src}) 日K线", got, f"{st} {len(rows)}根 末收={last_close}", js, src)

sect("4. 六家数据源 —— 特色数据（开盘了 / 同花顺）")
st, js = req("/special", tok=TOK)
kinds = [x["kind"] for x in (js.get("data") or [])]
ck("特色数据清单", st == 200 and bool(kinds), str(kinds))
for k in (kinds or [])[:4]:
    st, js = req(f"/special/{k}", tok=TOK)
    rows = js.get("data") or []
    ck(f"特色数据 {k}", st == 200 and len(rows) > 0, f"{st} {len(rows)}条")

#: 开盘了（kaipanla）**只在龙虎榜**这条路径上被用到，默认走同花顺，
#: 所以不显式指定 source=kaipanla 的话它永远是 ok=0，会被误判成"没接入"。
st, js = req("/special/dragon_tiger?source=kaipanla", tok=TOK)
rows = js.get("data") or []
ck("开盘了(kaipanla) 龙虎榜", st == 200 and len(rows) > 0,
   f"{st} {len(rows)}条 src={(js.get('meta') or {}).get('source')}")

# ---------------------------------------------------------------- 5. 数据接口
sect("5. 核心数据接口")
st, js = req("/quote?codes=600519.SH,000001.SZ,601318.SH", tok=TOK)
items = js.get("data") or []
ck("自动选源行情（不指定 source）", st == 200 and len(items) == 3,
   f"{st} {len(items)}只 src={(js.get('meta') or {}).get('source')}")
st, js = req("/quote?codes=600519.SH,000651.SZ,601899.SH,000725.SZ", tok=TOK)
ck("批量行情 4 只", st == 200 and len((js.get("data") or [])) == 4, f"{st}")

st, js = req("/kline?code=600519.SH&period=day&limit=30&adjust=qfq", tok=TOK)
rows = js.get("data") or []
ck("K线 30 根前复权", st == 200 and len(rows) >= 25, f"{st} {len(rows)}根")
for per in ("week", "month", "60min"):
    st, js = req(f"/kline?code=600519.SH&period={per}&limit=5", tok=TOK)
    ck(f"K线周期 {per}", st == 200 and bool(js.get("data")), f"{st}")

st, js = req("/stock/list?limit=20", tok=TOK)
#: 这个接口返回的是**裸数组**（没有 items 包装），别当 dict 处理
d = js.get("data") or []
lst = d if isinstance(d, list) else (d.get("items") or [])
sample = f" 例：{lst[0].get('code')} {lst[0].get('name')}" if lst else ""
ck("股票列表", st == 200 and len(lst) > 0, f"{st} {len(lst)}条{sample}")

sect("6. 公开接口（免鉴权，主页用）")
st, js = req("/public/quote")
ck("公开行情 /public/quote", st == 200 and bool(js.get("data")), f"{st}")
st, js = req("/public/market")
ck("市场概览 /public/market", st == 200, f"{st}")

# ---------------------------------------------------------------- 7. API Key
sect("7. API Key 管理")
st, js = req("/apikey", "POST", {"name": "hc-smoke", "scopes": ["quote", "kline"]}, tok=TOK)
kd = js.get("data") or {}
secret, kid = kd.get("secret"), kd.get("id")
ck("创建 Key", st == 200 and bool(secret), f"{st} {str(secret)[:20]}...")
if secret:
    st, js = req("/quote?codes=600519.SH", tok=secret)
    ck("用 Key 调行情", st == 200 and bool(js.get("data")), f"{st}")
    st, js = req("/kline?code=600519.SH&limit=3", tok=secret)
    ck("用 Key 调 K 线", st == 200 and bool(js.get("data")), f"{st}")
    #: 注意测的是**数据接口** /special/{kind}，不是清单接口 /special ——
    #: 清单只是元数据列表，设计上不校验 scope，用它会得到 200（不是 bug）。
    st, js = req("/special/limit_up", tok=secret)
    ck("无权限 scope 被拒(403)", st == 403, f"{st}")

st, js = req("/apikey", tok=TOK)
keys = js.get("data") or []
ck("Key 列表", st == 200 and len(keys) >= 1, f"{st} {len(keys)}个")
if kid:
    st, _ = req(f"/apikey/{kid}", "DELETE", tok=TOK)
    ck("吊销 Key", st == 200, f"{st}")
    st, _ = req(f"/apikey/{kid}/purge", "DELETE", tok=TOK)
    ck("物理删除已吊销 Key", st == 200, f"{st}")

# ---------------------------------------------------------------- 8. 计费
sect("8. 套餐 / 充值 / 账单")
st, js = req("/billing/plans", tok=TOK)
plans = (js.get("data") or {}).get("items") or []
ck("套餐列表", st == 200 and len(plans) >= 3, f"{st} {[p['code'] for p in plans]}")
st, js = req("/billing/me", tok=TOK)
b = js.get("data") or {}
ck("我的套餐与余额", st == 200,
   f"tier={b.get('plan', {}).get('code')} 余额={b.get('balance')}分")
#: 到期信息必须完整返回 —— 这是控制台展示的唯一数据源，
#: 少了 entitlement 前端就显示不出到期时间
ck("返回权益/到期信息", "entitlement" in b and "is_trial" in b,
   f"trial={b.get('is_trial')} 剩{b.get('days_left')}天 过期={b.get('expired')}")

st, js = req("/billing/channels", tok=TOK)
ch = (js.get("data") or {}).get("items") or []
ck("支付渠道列表", st == 200 and len(ch) >= 1,
   f"{st} {[c['channel'] for c in ch]} 在线已配={ (js.get('data') or {}).get('online_ready') }")
st, js = req("/billing/usage?days=7", tok=TOK)
u = js.get("data") or {}
ck("用量统计（控制台图表数据源）", st == 200 and bool(u.get("daily")),
   f"{st} {len(u.get('daily') or [])}天")
st, js = req("/billing/orders", tok=TOK)
ck("我的订单", st == 200, f"{st} total={(js.get('data') or {}).get('total')}")
st, js = req("/billing/bills", tok=TOK)
ck("我的账单", st == 200, f"{st}")

#: 后台用户列表要带试用/剩余天数标记，管理员才能判断该不该催续费
st, js = req("/admin/users?limit=5", tok=TOK)
rows = (js.get("data") or {}).get("items") or []
has_exp = any(("days_left" in r or r.get("is_trial") is not None) for r in rows)
ck("后台用户列表含有效期字段", st == 200 and bool(rows) and has_exp,
   f"{st} {len(rows)}个")

#: 后台改有效期接口。
#: **这条会真改数据**，所以必须"读原值 -> 改 -> 还原"，否则体检跑一次
#: 就把航哥自己的账号从"长期有效"变成"1 天后到期"，纯属自己挖坑。
if rows:
    uid = rows[0].get("id")
    orig_exp = rows[0].get("plan_expires_at")
    st2, js2 = req(f"/admin/users/{uid}/expiry", "POST", {"days": 1}, tok=TOK)
    ck("后台可调整用户有效期", st2 == 200,
       f"{st2} {(js2.get('data') or {}).get('plan_expires_at')}")
    restore = {"clear": True} if not orig_exp else {"expires_at": orig_exp}
    st3, _ = req(f"/admin/users/{uid}/expiry", "POST", restore, tok=TOK)
    ck("有效期已还原（体检不留副作用）", st3 == 200, f"{st3} 原值={orig_exp}")

sect("9. 支付闸门（安全）")
#: 期望值**按环境不同**：本地 .env 里 ALLOW_MOCK_PAY=true（跑测试必须开），
#: NAS 上是 false（防白嫖）。同一个断言不能两边都用 403，否则本地必然假失败。
IS_LOCAL = "--local" in sys.argv
st, js = req("/billing/recharge", "POST", {"amount": 10000, "channel": "mock"}, tok=TOK)
if IS_LOCAL:
    ck("模拟支付在本地可用(true)", st == 200, f"{st}")
else:
    ck("模拟支付已关闭(403)", st == 403, f"{st}")
st, js = req("/billing/recharge", "POST", {"amount": 10000, "channel": "manual"}, tok=TOK)
no = (js.get("data") or {}).get("order_no")
ck("线下转账可下单", st == 200 and bool(no), f"{st} {no}")
if no:
    st, js = req(f"/billing/orders/{no}/pay", "POST", {}, tok=TOK)
    ck("线下转账单不可自助入账(400)", st == 400, f"{st}")

# ---------------------------------------------------------------- 10. 后台
sect("10. 管理后台")
st, js = req("/admin/stats", tok=TOK)
s = js.get("data") or {}
ck("后台概览", st == 200, f"{st} 用户={s.get('users')} 24h调用={s.get('calls_24h')}")
#: 本地**没装 Redis**，这里是 False 属于预期，不算失败 —— 只有 NAS 才要求连通
if "--local" in sys.argv:
    print("     （本地无 Redis，跳过连通性断言）")
else:
    ck("后台 Redis 已连接",
       ((s.get("system") or {}).get("redis") or {}).get("available") is True,
       str(((s.get("system") or {}).get("redis") or {}).get("available")))
st, js = req("/admin/users?limit=5", tok=TOK)
ck("用户列表", st == 200, f"{st} total={(js.get('data') or {}).get('total')}")
st, js = req("/admin/keys?limit=5", tok=TOK)
ck("密钥列表", st == 200, f"{st}")
st, js = req("/admin/orders?limit=5", tok=TOK)
ck("订单列表", st == 200, f"{st} total={(js.get('data') or {}).get('total')}")
st, js = req("/admin/logs?limit=5", tok=TOK)
ck("调用日志", st == 200, f"{st} total={(js.get('data') or {}).get('total')}")
st, js = req("/admin/plans", tok=TOK)
ck("套餐管理", st == 200, f"{st} {len((js.get('data') or {}).get('items') or [])}个")

# ---------------------------------------------------------------- 11. 数据源健康
sect("11. 数据源健康汇总")
st, js = req("/sources", tok=TOK)
rows = js.get("data") or []
for r in rows:
    name = r.get("source")
    ok, fail, err = r.get("ok", 0), r.get("fail", 0), r.get("last_error", "")
    if ok == 0 and fail == 0:
        flag, note = "--", "本轮未被调用"
    elif fail == 0:
        flag, note = "OK", f"延迟 {r.get('last_latency_ms')}ms"
    else:
        flag, note = "!!", f"失败{fail}次 {err[:50]}"
    print(f"    {name:<10} ok={ok:<4} fail={fail:<4} {flag:<3} {note}")

print("\n" + "=" * 66)
print(f"体检结果：通过 {PASS} 项，失败 {FAIL} 项"
      + (f"，警告 {len(WARNS)} 项（上游限流，非本站故障）" if WARNS else ""))
if WARNS:
    for w in WARNS:
        print(f"    · {w}")
print("=" * 66)
sys.exit(1 if FAIL else 0)
