"""验证管理后台接口：stats / users / keys / logs 的筛选分页与各类写操作。"""

import json
import pathlib
import random
import sys
import time
import urllib.error
import urllib.request

# 管理员账号走本机 .env，不写进源码 —— 见 tools/local_cfg.py 的说明
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
from local_cfg import cfg

BASE = cfg("STOCKDATA_BASE", "http://127.0.0.1:9850").rstrip("/")
API = "/api/v1"
ADMIN_MAIL = cfg("STOCKDATA_ADMIN_EMAIL")
ADMIN_PWD = cfg("STOCKDATA_ADMIN_PASSWORD")
TS = f"{int(time.time())}{random.randint(1000, 9999)}"  # 加随机后缀，同秒重跑不撞车

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_fail = [0]


def req(path, method="GET", body=None, token=None, timeout=60, retry_conn=True):
    """发请求，返回 (status, json)。

    retry_conn=False 用于**非幂等的写操作**（删用户、purge 密钥）。
    本地偶发 WinError 10054：请求其实已被服务端执行，只是响应在回程丢了。
    盲目重试会把"删成功"重放成第二次请求，于是拿到"已不存在"的 404 ——
    [24]/[33]/[34] 都这样假失败过。这类操作要关掉重试，改以回查状态判定。
    """
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    last = None
    for attempt in range(1 if not retry_conn else 6):
        try:
            with _OPENER.open(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw[:300]}
        except (ConnectionResetError, TimeoutError, OSError) as e:
            last = e
            if retry_conn and attempt < 5:
                time.sleep(0.6 * (attempt + 1))
    #: 关掉重试时不抛异常，回 status=0 交给调用方用"回查状态"兜底。
    return 0, {"raw": f"网络错误: {last}"}


def user_gone(email, token):
    """回查：这个邮箱还能不能在用户列表里搜到。删用户的成败判定用它，
    比看那一次 DELETE 的响应码可靠。"""
    st, js = req(f"{API}/admin/users?limit=200&q={email}", token=token)
    if st != 200:
        return False
    return not [r for r in (js.get("data", {}).get("items") or []) if r.get("email") == email]


def ok(cond, label, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""), flush=True)
    if not cond:
        _fail[0] += 1


def get():
    st, js = req(f"{API}/auth/login", "POST", {"email": ADMIN_MAIL, "password": ADMIN_PWD})
    if st != 200:
        print("管理员登录失败", st, js)
        sys.exit(1)
    return js["data"]["access_token"]


def preclean():
    """清掉上次可能残留的测试账号，保证脚本可反复运行。

    本脚本用 adm<TS>a/b 造数据；如果上次被中断没清掉，会和新账号混在一起，
    让 "[7] 用户列表含新用户"、"按邮箱筛选只剩 1 条" 这类断言出现歧义。
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ApiKey, ApiLog, User

    s = SessionLocal()
    try:
        n = 0
        for u in s.scalars(select(User)).all():
            if (u.email or "").startswith("adm") and (u.email or "").endswith("@stockdata.dev"):
                #: 两张关联表都要删。以前只删了 User，
                #: 漏掉 ApiKey / ApiLog —— SQLite 的 INTEGER PRIMARY KEY 会复用
                #: 已删用户的 id，下一轮新建的 adm 用户"继承"了上一轮的密钥：
                #:   [8]  active_keys 变成 2（期望 1）
                #:   [20] 按邮箱搜密钥返回 2 条（期望 1）
                #: 只删用户不删关联表，是所有"计数莫名多 1"类假失败的共同根因。
                for M in (ApiKey, ApiLog):
                    s.query(M).filter(M.user_id == u.id).delete(synchronize_session=False)
                s.delete(u)
                n += 1
        if n:
            s.commit()
            print(f"  开跑前清理旧 adm 测试账号 {n} 个")
    except Exception as exc:  # noqa: BLE001
        print(f"  开跑前清理失败（忽略）: {exc}")
    finally:
        s.close()


print("=" * 62, flush=True)
print("管理后台接口验证", flush=True)
print("=" * 62, flush=True)

for i in range(40):
    try:
        st, _ = req("/health")
        if st == 200:
            break
    except Exception:
        pass
    time.sleep(1)

tok = get()
ok(True, "[0] 管理员登录", ADMIN_MAIL)

# ---------------- 1. stats 结构与取值
st, js = req(f"{API}/admin/stats", token=tok)
d = js.get("data", {}) if st == 200 else {}
need = ["users", "users_new_7d", "users_active_7d", "tier_counts", "api_keys",
        "active_keys", "calls_24h", "calls_prev_24h", "error_rate", "avg_latency_ms",
        "p95_latency_ms", "hourly", "top_paths", "status_dist", "system"]
missing = [k for k in need if k not in d]
ok(st == 200 and not missing, "[1] /admin/stats 字段完整", f"缺失 {missing}" if missing else "")
ok(len(d.get("hourly", [])) == 24, "[2] 24 小时分桶 = 24 段", str(len(d.get("hourly", []))))
ok(isinstance(d.get("p95_latency_ms"), int), "[3] P95 延迟为整数", str(d.get("p95_latency_ms")))
ok(d.get("calls_24h", 0) >= sum(h["total"] for h in d.get("hourly", [])),
   "[4] 24h 调用数 >= 分桶合计（分桶只取最近 23h+当前小时）",
   f"{d.get('calls_24h')} vs {sum(h['total'] for h in d.get('hourly', []))}")
sysinfo = d.get("system", {})
ok("database" in sysinfo and "redis" in sysinfo and "uptime_s" in sysinfo,
   "[5] system 信息含数据库/缓存/运行时长", f"{sysinfo.get('database')} redis={sysinfo.get('redis')}")
print(f"        users={d.get('users')} keys={d.get('active_keys')}/{d.get('api_keys')} "
      f"calls24h={d.get('calls_24h')} err={d.get('error_rate')}% "
      f"avg={d.get('avg_latency_ms')}ms p95={d.get('p95_latency_ms')}ms")

# ---------------- 2. 造测试数据
preclean()
U1 = f"adm{TS}a@stockdata.dev"
U2 = f"adm{TS}b@stockdata.dev"
PWD = "StockData@2024"
for m in (U1, U2):
    st, js = req(f"{API}/auth/register", "POST", {"email": m, "password": PWD})
    #: 注册是"创建型"接口：第一次可能已经成功、只是响应在回程丢了，req() 重试
    #: 后拿到 400「该邮箱已注册」。这恰恰说明账号已经在库里 —— 正是我们要的。
    #: 只有**其它**失败码才真的要中止（后面所有断言都依赖这两个账号）。
    if st == 400 and "已注册" in str((js or {}).get("detail") or ""):
        continue
    if st != 200:
        raise SystemExit(f"注册测试账号失败 {m}: {st} {js}")
st, js = req(f"{API}/auth/login", "POST", {"email": U1, "password": PWD})
if st != 200 or "access_token" not in (js.get("data") or {}):
    raise SystemExit(f"登录测试账号失败 {U1}: {st} {js}")
u1_tok = js["data"]["access_token"]

st, js = req(f"{API}/apikey", "POST", {"name": "adm-key-a"}, token=u1_tok)
if st != 200 or "id" not in (js.get("data") or {}):
    raise SystemExit(f"创建测试密钥失败: {st} {js}")
u1_key_id = js["data"]["id"]
u1_secret = js["data"]["secret"]
ok(True, "[6] 创建测试用户与密钥", f"{U1} key={u1_key_id}")

# 用一次产生日志
req(f"{API}/quote?codes=600519.SH", token=u1_tok)

# ---------------- 3. 用户列表筛选
st, js = req(f"{API}/admin/users?limit=200", token=tok)
items = js["data"]["items"]
total_all = js["data"]["total"]
u1_row = next((x for x in items if x["email"] == U1), None)
ok(u1_row is not None, "[7] 用户列表含新用户", f"total={total_all}")
ok(u1_row and u1_row.get("key_quota") == 3 and u1_row.get("active_keys") == 1,
   "[8] 返回 key_quota / active_keys", f"{u1_row and (u1_row.get('active_keys'), u1_row.get('key_quota'))}")
ok(u1_row and u1_row.get("calls_24h", 0) >= 1, "[9] 返回该用户 24h 调用数",
   str(u1_row and u1_row.get("calls_24h")))

st, js = req(f"{API}/admin/users?q={U1}", token=tok)
ok(js["data"]["total"] == 1 and js["data"]["items"][0]["email"] == U1,
   "[10] 按邮箱搜索命中 1 条", str(js["data"]["total"]))

st, js = req(f"{API}/admin/users?q=adm{TS}", token=tok)
ok(js["data"]["total"] == 2, "[11] 模糊搜索命中 2 条", str(js["data"]["total"]))

st, js = req(f"{API}/admin/users?limit=1", token=tok)
ok(js["data"]["total"] == total_all and len(js["data"]["items"]) == 1,
   "[12] 分页：total 是全量，items 只返回 1 条", f"total={js['data']['total']}")

st, js = req(f"{API}/admin/users?limit=1&offset=1", token=tok)
second = js["data"]["items"][0]["email"] if js["data"]["items"] else None
ok(second != items[0]["email"], "[13] offset=1 拿到第二页不同数据", f"{second}")

# ---------------- 4. 改套餐（body 与 query 两种）
st, js = req(f"{API}/admin/users/{u1_row['id']}/tier", "POST", {"tier": "vip"}, token=tok)
ok(st == 200 and js["data"]["tier"] == "vip", "[14] 改套餐（body）", f"{st}")
st, js = req(f"{API}/admin/users/{u1_row['id']}/tier?tier=pro", "POST", token=tok)
ok(st == 200 and js["data"]["tier"] == "pro", "[15] 改套餐（query，兼容旧前端）", f"{st}")
st, js = req(f"{API}/admin/users/{u1_row['id']}/tier?tier=xx", "POST", token=tok)
ok(st == 400, "[16] 非法套餐返回 400", f"{st}")

# ---------------- 5. 用户详情
st, js = req(f"{API}/admin/users/{u1_row['id']}", token=tok)
dd = js.get("data", {})
ok(st == 200 and dd.get("keys") and dd.get("user", {}).get("tier") == "pro",
   "[17] 用户详情含密钥与套餐", f"keys={len(dd.get('keys', []))}")

# ---------------- 6. 重置密码
st, js = req(f"{API}/admin/users/{u1_row['id']}/password", "POST",
             {"password": "NewPass@2026"}, token=tok)
ok(st == 200, "[18] 重置密码", f"{st}")
st_old, _ = req(f"{API}/auth/login", "POST", {"email": U1, "password": PWD})
st_new, _ = req(f"{API}/auth/login", "POST", {"email": U1, "password": "NewPass@2026"})
ok(st_old == 401 and st_new == 200, "[19] 旧密码失效 / 新密码可用", f"old={st_old} new={st_new}")

# ---------------- 7. 密钥管理
st, js = req(f"{API}/admin/keys?q={U1}", token=tok)
ok(js["data"]["total"] == 1, "[20] 密钥按归属邮箱搜索", str(js["data"]["total"]))
st, js = req(f"{API}/admin/keys?status=revoked", token=tok)
ok(all(not k["is_active"] for k in js["data"]["items"]), "[21] 按状态筛选（已吊销）",
   str(js["data"]["total"]))

st, js = req(f"{API}/admin/keys/{u1_key_id}", "DELETE", token=tok)
ok(st == 400, "[22] 未吊销直接删除 -> 400", f"{st}")
st, js = req(f"{API}/admin/keys/{u1_key_id}/revoke", "POST", token=tok)
ok(st == 200, "[23] 管理员吊销密钥", f"{st}")
#: 删密钥同样是"删了就没"的操作，关掉重试；404 也可能是"其实删掉了，
#: 重试时撞上的"。用"密钥是否已不在列表"来兜底判定。
st, js = req(f"{API}/admin/keys/{u1_key_id}", "DELETE", token=tok, retry_conn=False)
_, kjs = req(f"{API}/admin/keys?q={U1}", token=tok)
left24 = (kjs.get("data") or {}).get("total")
gone24 = st == 200 or left24 == 0
ok(gone24, "[24] 管理员删除已吊销密钥", f"{st}（剩余 {left24}）")
st, js = req(f"{API}/admin/keys/{u1_key_id}", "DELETE", token=tok)
ok(st == 404, "[25] 重复删除 -> 404", f"{st}")

# ---------------- 8. 日志筛选
st, js = req(f"{API}/admin/logs?q=quote", token=tok)
ok(js["data"]["total"] >= 1, "[26] 日志按路径搜索", str(js["data"]["total"]))
st, js = req(f"{API}/admin/logs?status=err", token=tok)
ok(all(l["status"] >= 400 for l in js["data"]["items"]), "[27] 只看失败的筛选正确",
   str(js["data"]["total"]))
st, js = req(f"{API}/admin/logs?limit=5", token=tok)
ok(len(js["data"]["items"]) <= 5 and js["data"]["total"] >= len(js["data"]["items"]),
   "[28] 日志分页", f"total={js['data']['total']}")

# ---------------- 9. 权限与自我保护
st, _ = req(f"{API}/admin/stats", token=u1_tok)
ok(st == 403, "[29] 普通用户访问后台 -> 403", f"{st}")
st, _ = req(f"{API}/admin/stats")
ok(st in (401, 403), "[30] 未登录访问后台 -> 401/403", f"{st}")

st, js = req(f"{API}/admin/users/1/toggle", "POST", token=tok)
ok(st == 400 and "自己" in str(js.get("detail", "")), "[31] 不能禁用/启用自己之外的保护",
   f"{st} {js.get('detail')}")

st, js = req(f"{API}/admin/users/1", "DELETE", token=tok)
ok(st == 400, "[32] 不能删除自己", f"{st} {js.get('detail')}")

# ---------------- 10. 删除测试用户
st, js = req(f"{API}/admin/users?limit=200&q={U2}", token=tok)
rows = js["data"]["items"]
if rows:
    st, js = req(f"{API}/admin/users/{rows[0]['id']}", "DELETE", token=tok, retry_conn=False)
    ok(st == 200 or user_gone(U2, tok), "[33] 删除用户", f"{st} {js.get('data')}")
st, js = req(f"{API}/admin/users?limit=200&q={U1}", token=tok)
rows = js["data"]["items"]
if rows:
    st, _ = req(f"{API}/admin/users/{rows[0]['id']}", "DELETE", token=tok, retry_conn=False)
    ok(st == 200 or user_gone(U1, tok), "[34] 删除另一个测试用户", f"{st}")

print("=" * 62, flush=True)
print("ALL PASS" if _fail[0] == 0 else f"{_fail[0]} 项失败", flush=True)
sys.exit(1 if _fail[0] else 0)
