"""NAS 部署验收：从本机访问 NAS_INTERNAL_IP:9850 跑一遍关键链路。

验收项对齐 DEPLOY-NAS.md 的验收清单：
  首页 / 注册管理员 / 用量图表 / 建 Key / 调行情 / 充值只剩线下转账 /
  后台概览（缓存应已连接）
"""
import json
import time
import urllib.error
import urllib.request

BASE = "http://NAS_INTERNAL_IP:9850"
API = "/api/v1"
ADMIN = ("admin@example.com", "***REMOVED***")

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PASS = FAIL = 0


def req(p, m="GET", b=None, tok=None, timeout=40):
    d = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(BASE + p, data=d, method=m)
    r.add_header("Content-Type", "application/json")
    if tok:
        r.add_header("Authorization", "Bearer " + tok)
    for i in range(3):
        try:
            with op.open(r, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                #: 静态页面返回的是 HTML，不能按 JSON 解析 —— 只回状态码
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
        except OSError:
            if i < 2:
                time.sleep(1.0)
                continue
            return 0, {"raw": "网络错误"}


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}  {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


print("=" * 62)
print("NAS 部署验收  http://NAS_INTERNAL_IP:9850")
print("=" * 62)

# 1. 静态页面
for p, label in [("/", "首页"), ("/login.html", "登录页"), ("/console.html", "控制台"),
                 ("/admin.html", "后台"), ("/docs.html", "文档")]:
    st, _ = req(p)
    ck(f"页面可访问 {label}", st == 200, f"{st}")

# 2. 健康检查
st, js = req("/health")
ck("健康检查 + Redis", st == 200 and js.get("redis") is True, str(js))

# 3. 注册管理员（全新库，第一个注册的成为管理员）
em, pw = ADMIN
st, js = req(f"{API}/auth/register", "POST", {"email": em, "password": pw})
reg_ok = st in (200, 400)
ck("注册管理员账号", reg_ok, f"{st} {js.get('detail') or (js.get('data') or {}).get('email')}")
st, js = req(f"{API}/auth/login", "POST", {"email": em, "password": pw})
tok = (js.get("data") or {}).get("access_token")
ck("管理员登录", st == 200 and bool(tok), f"{st}")

if not tok:
    print("\n登录失败，后续验收跳过")
    raise SystemExit(1)

# 4. 套餐 / 配额
st, js = req(f"{API}/billing/plans", tok=tok)
items = (js.get("data") or {}).get("items", [])
ck("套餐列表 3 档", st == 200 and len(items) == 3, str([x["code"] for x in items]))

# 5. 建 API Key
st, js = req(f"{API}/apikey", "POST", {"name": "nas-smoke", "scopes": ["quote"]}, tok=tok)
key = (js.get("data") or {}).get("secret")
kid = (js.get("data") or {}).get("id")
ck("创建 API Key", st == 200 and bool(key), f"{st} {str(key)[:18]}...")

# 6. 用 Key 调数据接口
st, js = req(f"{API}/quote?codes=600519.SH", tok=key)
ok_data = st == 200 and bool((js.get("data") or []))
ck("用 Key 调行情 /quote", ok_data, f"{st}")
if ok_data:
    d0 = (js.get("data") or [{}])[0]
    print(f"         贵州茅台 现价 {d0.get('price')}")

# 7. 安全闸门：充值只能线下转账
st, js = req(f"{API}/billing/recharge", "POST",
             {"amount": 10000, "channel": "mock"}, tok=tok)
ck("模拟支付已关闭(403)", st == 403, f"{st} {js.get('detail')}")

st, js = req(f"{API}/billing/recharge", "POST",
             {"amount": 10000, "channel": "manual"}, tok=tok)
no = (js.get("data") or {}).get("order_no")
ck("线下转账建单可用", st == 200 and bool(no), f"{st} {no}")

if no:
    st, js = req(f"{API}/billing/orders/{no}/pay", "POST", {}, tok=tok)
    ck("线下转账单不能自助入账(400)", st == 400, f"{st} {js.get('detail')}")

# 8. 后台概览（缓存状态）
st, js = req(f"{API}/admin/stats", tok=tok)
sysinfo = (js.get("data") or {}).get("system") or {}
ck("后台概览可访问", st == 200, f"{st}")
ck("后台缓存状态已连接", (sysinfo.get("redis") or {}).get("available") is True,
   str(sysinfo.get("redis", {}).get("available")))
ck("后台识别到 PostgreSQL", "postgresql" in str(sysinfo.get("database", "")).lower(),
   str(sysinfo.get("database"))[:60])

# 9. 清理冒烟数据
if kid:
    req(f"{API}/apikey/{kid}", "DELETE", tok=tok)
    req(f"{API}/apikey/{kid}/purge", "DELETE", tok=tok)

print("\n" + "=" * 62)
print(f"验收结果：通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 62)
raise SystemExit(1 if FAIL else 0)
