"""抓取真实接口响应，生成前端冒烟测试用的 _smoke_data.json。

用法：先起服务，然后 python -u gen_smoke_data.py
这样冒烟测试喂给渲染函数的是**真实结构**，而不是手写猜测的假数据。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SD_BASE", "http://127.0.0.1:9850/api/v1")
ADMIN_EMAIL = os.environ.get("SD_ADMIN", "admin@example.com")
ADMIN_PWD = os.environ.get("SD_PWD", "***REMOVED***")

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_smoke_data.json")


def req(method: str, path: str, token: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(4):
        try:
            with opener.open(r, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001  沙箱偶发 reset，重试即可
            continue
    raise SystemExit(f"请求失败: {path}")


def main() -> int:
    ok, p = True, req("POST", "/auth/login", body={"email": ADMIN_EMAIL, "password": ADMIN_PWD})
    token = (p.get("data") or {}).get("access_token")
    if not token:
        print(f"管理员登录失败: {p}")
        return 1

    data = {
        "stats": req("GET", "/admin/stats", token)["data"],
        "sources": {"data": req("GET", "/sources", token).get("data") or []},
        "users": req("GET", "/admin/users?limit=20", token)["data"],
        "keys": req("GET", "/admin/keys?limit=20", token)["data"],
        "logs": req("GET", "/admin/logs?limit=30", token)["data"],
        "plans": req("GET", "/admin/plans", token)["data"],
        "orders": req("GET", "/admin/orders?limit=20", token)["data"],
        "bills": req("GET", "/admin/bills?limit=20", token)["data"],
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    print(f"已写入 {OUT}")
    for k, v in data.items():
        n = len(v.get("items", [])) if isinstance(v, dict) and "items" in v else "-"
        print(f"  {k}: items={n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
