"""验证「已吊销的 API Key 可物理删除」全链路。

覆盖：
 1. 创建密钥成功
 2. 未吊销直接 purge -> 400
 3. 吊销密钥 -> 200
 4. purge 已吊销密钥 -> 200 且列表里消失
 5. 重复 purge 同一 id -> 404
 6. 别人的密钥 purge -> 404（越权防护）
 7. 未登录 purge -> 401
"""

import json
import random
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:9850"
API = "/api/v1"
def _uniq(prefix):
    """时间戳+随机后缀：同一秒内反复跑测试也不会撞邮箱。"""
    return f"{prefix}{int(time.time())}{random.randint(1000, 9999)}@stockdata.dev"


OWNER = _uniq("purge")
OTHER = _uniq("other")
PWD = "StockData@2024"

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_failed = [0]


def req(path, method="GET", body=None, token=None, timeout=40, retry_conn=True):
    """发请求，返回 (status, json)。

    retry_conn=False 用于**非幂等的写操作**。为什么必须能关掉：
      本地环境偶发 WinError 10054（连接被对端重置）。如果请求其实已经被
      服务端执行了、只是响应在回程中丢了，这里的重试会把它再发一遍 ——
      第二遍拿到的是"已经不存在"的 404，于是 [5] 明明删成功了却报 404。
      实测抓到过：DB 里行确实没了，接口却回 {'detail': 'API Key 不存在'}。
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
                return e.code, {"raw": raw[:400]}
        except (ConnectionResetError, TimeoutError, OSError) as e:
            last = e
            if retry_conn and attempt < 5:
                time.sleep(0.6 * (attempt + 1))
    #: 关掉重试时**不抛异常**，回一个 status=0 让调用方走"回查状态"兜底。
    #: 以前这里 raise，一次断连就把整个脚本炸掉，后面的清理和汇总全没了。
    return 0, {"raw": f"网络错误: {last}"}


def key_gone(key_id, token):
    """回查：这个 id 的密钥还在不在我的列表里。

    purge 这类"删了就没"的操作，判定成功与否要**看状态**而不是看那一次
    的响应码 —— 响应码可能在回程中丢掉。
    """
    st, js = req(f"{API}/apikey", "GET", token=token)
    items = js.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("list") or []
    return key_id not in [k.get("id") for k in items if isinstance(k, dict)]


def ok(cond, label, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""), flush=True)
    if not cond:
        _failed[0] += 1


def login(email):
    st, js = req(f"{API}/auth/login", "POST", {"email": email, "password": PWD})
    if st != 200:
        st2, _ = req(f"{API}/auth/register", "POST", {"email": email, "password": PWD})
        print(f"        (注册 {email} -> {st2})")
        st, js = req(f"{API}/auth/login", "POST", {"email": email, "password": PWD})
    if st != 200 or not (js.get("data") or {}).get("access_token"):
        raise SystemExit(f"登录失败，无法继续 {email}: {st} {js}")
    return js["data"]["access_token"]


def preclean():
    """清掉上次残留的 purge*/other* 测试账号。

    本脚本原本没有清理逻辑，跑完就把账号留在库里；次数多了不仅列表接口变慢，
    还会因为 SQLite 复用 id 让订单/流水串到新账号上。

    只清自己这两个前缀：e2e* 归 e2e_test 管，adm* 归 test_admin 管，
    互相越界会在连续跑全套测试时把别人的账号误杀。
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ApiKey, ApiLog, BalanceLog, Order, User

    s = SessionLocal()
    try:
        n = 0
        prefixes = ("purge", "other")
        for u in s.scalars(select(User)).all():
            em = u.email or ""
            if not em.endswith("@stockdata.dev"):
                continue
            if not em.split("@")[0].startswith(prefixes):
                continue
            s.query(Order).filter(Order.user_id == u.id).delete(synchronize_session=False)
            s.query(BalanceLog).filter(BalanceLog.user_id == u.id).delete(synchronize_session=False)
            s.query(ApiLog).filter(ApiLog.user_id == u.id).delete(synchronize_session=False)
            s.query(ApiKey).filter(ApiKey.user_id == u.id).delete(synchronize_session=False)
            s.delete(u)
            n += 1
        if n:
            s.commit()
            print(f"  开跑前清理旧测试账号 {n} 个", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  开跑前清理失败（忽略）: {exc}", flush=True)
    finally:
        s.close()


print("=" * 60, flush=True)
print("API Key 物理删除（purge）验证", flush=True)
print("=" * 60, flush=True)

# 等健康检查
for i in range(60):
    try:
        st, _ = req("/health")
        if st == 200:
            print(f"[0] 健康检查 200（第 {i+1} 次）", flush=True)
            break
    except Exception:
        pass
    time.sleep(1)
else:
    print("服务未就绪，退出", flush=True)
    sys.exit(1)

preclean()
tok_a = login(OWNER)
tok_b = login(OTHER)
ok(bool(tok_a) and bool(tok_b), "[1] 两个测试账号登录", f"A={bool(tok_a)} B={bool(tok_b)}")

# ---- 2. 创建密钥
st, js = req(f"{API}/apikey", "POST", {"name": "purge-test"}, token=tok_a)
ok(st == 200, "[2] 创建密钥", f"{st}")
key_id = js.get("data", {}).get("id")
raw_key = js.get("data", {}).get("secret")
ok(bool(raw_key), "[2b] 返回明文密钥（仅此一次）", f"{str(raw_key)[:16]}...")
# 后面每一步都要用 key_id，创建失败时必须立刻停 —— 否则会拿着 None
# 去请求 /apikey/None，得到一串看不懂的 404 假失败
if not key_id:
    print(f"\n创建密钥失败，无法继续：{st} {js}", flush=True)
    sys.exit(1)

# ---- 3. 未吊销直接删除 -> 400
st, js = req(f"{API}/apikey/{key_id}/purge", "DELETE", token=tok_a)
msg = js.get("detail") or js.get("msg") or str(js)
ok(st == 400, "[3] 未吊销直接删除应 400", f"{st} {msg}")

# ---- 3b. 吊销前的密钥仍可用（数据接口能调通）
st, js = req(f"{API}/quote?codes=600519.SH", "GET", token=tok_a)
ok(st == 200, "[3b] 吊销前密钥可正常调数据", f"{st}")

# ---- 4. 吊销（DELETE /{id}）
st, js = req(f"{API}/apikey/{key_id}", "DELETE", token=tok_a)
ok(st == 200, "[4] 吊销密钥", f"{st}")

# ---- 5. purge 已吊销密钥
#: purge 不重试（重试会撞上"已删除"的 404），改以"回查列表"判定成败。
st, js = req(f"{API}/apikey/{key_id}/purge", "DELETE", token=tok_a, retry_conn=False)
gone5 = st == 200 or key_gone(key_id, tok_a)
ok(gone5, "[5] 删除已吊销密钥", f"{st} {js.get('msg','')}")

# ---- 6. 列表里确认消失
st, js = req(f"{API}/apikey", "GET", token=tok_a)
items = js.get("data") or []
if isinstance(items, dict):
    items = items.get("items") or items.get("list") or []
ids = [k.get("id") for k in items if isinstance(k, dict)]
ok(key_id not in ids, "[6] 列表已不含该密钥", f"剩余 {len(ids)} 个")

# ---- 7. 重复 purge -> 404
st, js = req(f"{API}/apikey/{key_id}/purge", "DELETE", token=tok_a)
ok(st == 404, "[7] 重复删除应 404", f"{st}")

# ---- 8. 别人的密钥越权 -> 404
st, js = req(f"{API}/apikey", "POST", {"name": "b-key"}, token=tok_b)
b_id = (js.get("data") or {}).get("id")
# 创建失败时 b_id 会是 None，后面 /apikey/None 全变 404 造成误判。
# 必须先确认创建成功，否则直接中止，避免"假失败"。
if not b_id:
    ok(False, "[8a] B 创建密钥（前置条件）", f"{st} {js}")
    b_id = 0
else:
    ok(True, "[8a] B 创建密钥", f"id={b_id}")
st, _ = req(f"{API}/apikey/{b_id}", "DELETE", token=tok_b)
ok(st == 200, "[8b] B 吊销自己的密钥", f"{st}")
st, js = req(f"{API}/apikey/{b_id}/purge", "DELETE", token=tok_a)
ok(st == 404, "[8] 越权删他人密钥应 404", f"{st}")

# ---- 9. 未登录 -> 401
st, js = req(f"{API}/apikey/{b_id}/purge", "DELETE")
ok(st in (401, 403), "[9] 未登录删除应 401/403", f"{st}")

# ---- 10. 清理：B 自己的密钥可以正常删掉
st, _ = req(f"{API}/apikey/{b_id}/purge", "DELETE", token=tok_b, retry_conn=False)
ok(st == 200 or key_gone(b_id, tok_b), "[10] 本人删自己的已吊销密钥", f"{st}")

print("=" * 60, flush=True)
print("ALL PASS" if _failed[0] == 0 else f"{_failed[0]} 项失败", flush=True)
sys.exit(1 if _failed[0] else 0)
