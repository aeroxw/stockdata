"""StockData 全功能 + 六家数据源体检。

用法：python -u tools/healthcheck.py
默认打 NAS，加 --local 打本机 127.0.0.1:9850。

NAS 地址和管理员账号**不再硬编码**（2026-09-29 之前写死在文件里，
推公开仓库时泄露了管理员邮箱与口令）。现在从环境变量或项目根 .env 读：

    STOCKDATA_BASE=http://192.168.x.x:9850
    STOCKDATA_ADMIN_EMAIL=you@example.com
    STOCKDATA_ADMIN_PASSWORD=你的密码

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

from local_cfg import cfg

if "--local" in sys.argv:
    BASE = "http://127.0.0.1:9850/api/v1"
else:
    BASE = cfg("STOCKDATA_BASE", "http://127.0.0.1:9850").rstrip("/") + "/api/v1"
EMAIL = cfg("STOCKDATA_ADMIN_EMAIL")
PWD = cfg("STOCKDATA_ADMIN_PASSWORD")

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
#:
#: 注意：错误文案在 base.explain_error() 里被翻译成中文了，
#: 所以这里几种写法都要留着 —— 英文原串在旧日志里还有。
EM_LIMIT_HINTS = (
    "熔断冷却", "限流",
    "上游未返回任何响应", "掐断了连接", "已被上游下线",
    "Server disconnected", "RemoteProtocol",
)


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

# ---------------------------------------------------------------- 8. 配额与积分
sect("8. 配额、积分与兑换")
st, js = req("/quota/plans", tok=TOK)
plans = (js.get("data") or {}).get("items") or []
ck("档位列表", st == 200 and len(plans) >= 3, f"{st} {[p['code'] for p in plans]}")
#: 档位里**不能再出现价格字段**，出现就说明开源化改造没做干净
ck("档位不含价格字段",
   all(("price_month" not in p and "price_year" not in p) for p in plans),
   str([k for p in plans for k in p if k.startswith("price_")]))

st, js = req("/quota/me", tok=TOK)
b = js.get("data") or {}
ck("我的配额", st == 200,
   f"tier={b.get('tier')} max_keys={(b.get('quota') or {}).get('max_keys')} "
   f"日配额={(b.get('quota') or {}).get('daily_quota')}")
#: 三项额度是限流链路的唯一真源，控制台与实际拦截必须读同一份
ck("返回三项额度", {"max_keys", "rate_limit", "daily_quota"} <= set(b.get("quota") or {}),
   str(sorted((b.get("quota") or {}).keys())))
ck("返回已用情况", "calls_24h" in (b.get("usage") or {}),
   str(sorted((b.get("usage") or {}).keys())))
#: 积分与到期时间是积分机制的核心返回，控制台靠它们渲染
ck("返回积分与到期信息", "points" in b and "plan_expires_at" in b,
   f"积分={b.get('points')} 到期={b.get('plan_expires_at')}")

st, js = req("/quota/usage?days=7", tok=TOK)
u = js.get("data") or {}
ck("用量统计（控制台图表数据源）", st == 200 and bool(u.get("daily")),
   f"{st} {len(u.get('daily') or [])}天")

#: 后台用户列表要带档位与 Key 用量，管理员才能判断该不该调额度
st, js = req("/admin/users?limit=5", tok=TOK)
rows = (js.get("data") or {}).get("items") or []
ck("后台用户列表可用", st == 200 and bool(rows), f"{st} {len(rows)}个")
#: balance / is_trial 是已删除的计费列，绝不能回来；
#: 但 plan_expires_at 在积分机制里是合法的（兑换来的档位到期日），必须存在。
ck("后台用户行不含余额/试用期字段",
   bool(rows) and all(("balance" not in r and "is_trial" not in r) for r in rows),
   str([k for r in rows for k in r if k in ("balance", "is_trial")]))
ck("后台用户行带积分与到期时间",
   bool(rows) and all("points" in r for r in rows),
   str([r.get("points") for r in rows]))

# ---- 签到积分 ----
st, js = req("/points/checkin", "POST", tok=TOK)
ck("签到", st == 200, f"{st} {js.get('msg')}")
st, js = req("/points/checkin", "POST", tok=TOK)
ck("同日重复签到不加分",
   (js.get("data") or {}).get("already") is True, f"{st} {js.get('msg')}")

st, js = req("/points/me", tok=TOK)
p = js.get("data") or {}
ck("积分概览可读", st == 200 and "points" in p, f"{st} 积分={p.get('points')}")
opts = p.get("options") or []
ck("可兑换档位 2 个", len(opts) == 2, str([o["code"] for o in opts]))
ck("兑换价 专业版15 / 旗舰版30",
   {o["code"]: o["redeem_points"] for o in opts} == {"pro": 15, "vip": 30},
   str({o["code"]: o["redeem_points"] for o in opts}))

st, js = req("/points/logs?limit=5", tok=TOK)
ck("积分流水可读", st == 200 and "items" in (js.get("data") or {}), f"{st}")

#: 积分不足时兑换必须被拒，这是配额机制不会被人绕过的保证。
#: 这一项**只在管理员本人积分不够时才会真正执行**（够的话打过去会兑换成功，
#: 那就把人家档位改了）。所以跳过时要显式打一行 SKIP，
#: 否则每次体检的总项数会飘，容易被误读成"少了一项，是不是挂了"。
cost_min = min((o["redeem_points"] for o in opts), default=0)
if p.get("points", 0) < cost_min:
    st, js = req("/points/redeem", "POST", {"plan_code": "pro"}, tok=TOK)
    ck("积分不足时兑换被拒(400)", st == 400, f"{st} {js.get('detail')}")
else:
    print(f"  [SKIP] 积分不足时兑换被拒(400)  —— 当前 {p.get('points')} 分"
          f"已够兑换（最低 {cost_min} 分），跳过以免误改档位；"
          f"该分支由 backend/test_points.py 覆盖")

sect("9. 支付已彻底移除（安全）")
#: 开源版不该再有任何计费端点。这里逐个确认它们已经 404，
#: 否则说明有残留路由被挂上去了。
for path in ("/billing/me", "/billing/plans", "/billing/orders", "/billing/bills",
             "/billing/channels"):
    st, _ = req(path, tok=TOK)
    ck(f"{path} 已移除(404)", st == 404, f"{st}")

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
st, js = req("/admin/logs?limit=5", tok=TOK)
ck("调用日志", st == 200, f"{st} total={(js.get('data') or {}).get('total')}")
st, js = req("/admin/plans", tok=TOK)
ck("档位管理", st == 200, f"{st} {len((js.get('data') or {}).get('items') or [])}个")

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
