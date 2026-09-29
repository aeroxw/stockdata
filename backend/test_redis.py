"""验证 Redis 状态展示与自动重连。

重点：旧实现一旦探测失败就永久判死，Redis 后起来也接不上（容器场景致命）。
这里用「假 Redis」起停来模拟，确认现在能自动接上。
"""

import json
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

# 管理员账号走本机 .env，不写进源码 —— 见 tools/local_cfg.py 的说明
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
from local_cfg import cfg

BASE = "http://127.0.0.1:9850"
API = "/api/v1"
PORT = 6379

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_fail = [0]


def req(path, method="GET", body=None, token=None, timeout=30):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    for attempt in range(4):
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
            if attempt == 3:
                raise
            time.sleep(0.6 * (attempt + 1))


def ok(cond, label, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra else ""), flush=True)
    if not cond:
        _fail[0] += 1


# ---------------------------------------------------------------- 假 Redis
class FakeRedis(threading.Thread):
    """只实现 PING / EXISTS / SETEX / GET / INCR / EXPIRE，够跑通探测与基本调用。"""

    daemon = True

    def __init__(self):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", PORT))
        self.sock.listen(8)
        self.store: dict[str, str] = {}
        self.running = True
        self.pings = 0

    def run(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _read_cmd(self, f):
        line = f.readline()
        if not line or not line.startswith(b"*"):
            return None
        n = int(line[1:].strip())
        args = []
        for _ in range(n):
            hdr = f.readline()
            if not hdr or not hdr.startswith(b"$"):
                return None
            ln = int(hdr[1:].strip())
            args.append(f.read(ln))
            f.read(2)
        return args

    def _serve(self, conn):
        with conn:
            f = conn.makefile("rb")
            while True:
                try:
                    args = self._read_cmd(f)
                except Exception:
                    break
                if not args:
                    break
                cmd = args[0].upper().decode()
                if cmd == "PING":
                    self.pings += 1
                    conn.sendall(b"+PONG\r\n")
                elif cmd == "EXISTS":
                    conn.sendall(b":%d\r\n" % (1 if args[1].decode() in self.store else 0))
                elif cmd in ("SETEX", "SET"):
                    self.store[args[1].decode()] = args[3].decode() if cmd == "SETEX" else args[2].decode()
                    conn.sendall(b"+OK\r\n")
                elif cmd == "GET":
                    v = self.store.get(args[1].decode())
                    conn.sendall(b"$%d\r\n%s\r\n" % (len(v), v.encode()) if v is not None else b"$-1\r\n")
                elif cmd == "INCR":
                    k = args[1].decode()
                    v = int(self.store.get(k, "0")) + 1
                    self.store[k] = str(v)
                    conn.sendall(b":%d\r\n" % v)
                elif cmd == "EXPIRE":
                    conn.sendall(b":1\r\n")
                elif cmd == "COMMAND":
                    conn.sendall(b"*0\r\n")
                else:
                    conn.sendall(b"+OK\r\n")

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


def port_open() -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


print("=" * 62, flush=True)
print("Redis 状态与自动重连验证", flush=True)
print("=" * 62, flush=True)

for i in range(40):
    try:
        st, _ = req("/health")
        if st == 200:
            break
    except Exception:
        pass
    time.sleep(1)

st, js = req(f"{API}/auth/login", "POST",
             {"email": cfg("STOCKDATA_ADMIN_EMAIL"),
              "password": cfg("STOCKDATA_ADMIN_PASSWORD")})
if st != 200:
    print("登录失败", st, js)
    sys.exit(1)
tok = js["data"]["access_token"]
ok(True, "[0] 管理员登录")

# ---------------- 1. 未连接时给出结构化信息
st, js = req(f"{API}/admin/stats", token=tok)
r = (js.get("data", {}).get("system", {}) or {}).get("redis", {}) if st == 200 else {}
ok(isinstance(r, dict) and "available" in r and "url" in r,
   "[1] stats.system.redis 是结构化对象", str(list(r.keys())))
ok(r.get("available") is False and r.get("error"),
   "[2] 无 Redis 时给出具体原因", str(r.get("error"))[:70])
ok(r.get("url", "").startswith("redis://"), "[3] 返回 Redis 地址", r.get("url"))
ok(r.get("retry_interval_s") == 30, "[4] 返回自动重试间隔", str(r.get("retry_interval_s")))

# ---------------- 2. 起假 Redis → 应能自动接上（旧实现这里会永远失败）
srv = FakeRedis()
srv.start()
time.sleep(0.5)
ok(port_open(), "[5] 假 Redis 已监听 6379")

st, js = req(f"{API}/admin/redis/recheck", "POST", token=tok)
r2 = js.get("data", {}) if st == 200 else {}
ok(st == 200 and r2.get("available") is True,
   "[6] Redis 起来后重测 -> 已连接（旧实现会永久失败）", f"{st} {js.get('msg')}")
ok(not r2.get("error"), "[7] 连接成功后清空错误信息", repr(r2.get("error")))

# ---------------- 3. 连接后限流/黑名单真的走 Redis
st, js = req(f"{API}/admin/stats", token=tok)
r3 = js["data"]["system"]["redis"]
ok(r3.get("available") is True, "[8] stats 里也显示已连接")
ok(srv.pings >= 1, "[9] 确实向 Redis 发过 PING", f"pings={srv.pings}")

# ---------------- 4. 关掉 Redis → 自动降回未连接
srv.stop()
time.sleep(0.6)
st, js = req(f"{API}/admin/redis/recheck", "POST", token=tok)
r4 = js.get("data", {}) if st == 200 else {}
ok(st == 200 and r4.get("available") is False,
   "[10] Redis 掉线后重测 -> 回到未连接（服务未崩溃）", f"{st} {js.get('msg')}")
ok(bool(r4.get("error")), "[11] 记录了掉线原因", str(r4.get("error"))[:70])

# ---------------- 5. 掉线后服务仍然正常（降级不中断）
st, js = req(f"{API}/admin/stats", token=tok)
ok(st == 200 and js["data"]["users"] >= 1, "[12] Redis 不可用时接口仍正常", f"{st}")
st, js = req(f"{API}/admin/users?limit=3", token=tok)
ok(st == 200, "[13] 用户列表不受影响", f"{st}")

print("=" * 62, flush=True)
print("ALL PASS" if _fail[0] == 0 else f"{_fail[0]} 项失败", flush=True)
sys.exit(1 if _fail[0] else 0)
