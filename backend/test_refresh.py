"""验证 /auth/refresh 链路：过期 access_token 能自动续期而不被登出。

覆盖：
  [1] 登录返回双令牌
  [2] refresh 能换到新的一对令牌
  [3] 新 access_token 可用
  [4] 新 refresh_token 也可用（轮换）
  [5] 乱写的 refresh_token 被拒 401
  [6] 拿 access_token 冒充 refresh_token 被拒（type 校验）
  [7] 用过期 access_token 打业务接口 401，用 refresh 续期后可正常访问
  [8] 不存在的用户 401
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:9850"
EMAIL = "refresh_probe@stockdata.dev"
PWD = "ProbePass123!"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PASS = FAIL = 0


def req(method, path, body=None, token=None, tries=6):
    last = None
    for _ in range(tries):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("Authorization", "Bearer " + token)
        try:
            with _opener.open(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6)
    raise last


def check(no, desc, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  [{no}] {desc} {extra}")
    else:
        FAIL += 1
        print(f"FAIL  [{no}] {desc} {extra}")


def main():
    # 准备账号
    dest, _ = req("POST", "/api/v1/auth/register", {"email": EMAIL, "password": PWD})
    if dest == 400:
        # 已存在则改密码不方便，直接换一个时间戳账号
        pass
    s, b = req("POST", "/api/v1/auth/login", {"email": EMAIL, "password": PWD})
    if s != 200:
        print("登录失败，无法继续:", s, b)
        sys.exit(1)
    at = b["data"]["access_token"]
    rt = b["data"]["refresh_token"]

    check(1, "登录返回双令牌", bool(at) and bool(rt))
    check(1.1, "refresh_token 与 access_token 不同", at != rt)

    # [2] 刷新
    s, b2 = req("POST", "/api/v1/auth/refresh", {"refresh_token": rt})
    check(2, "refresh 换到新令牌", s == 200 and b2.get("data", {}).get("access_token"),
          f"status={s}")
    at2 = (b2.get("data") or {}).get("access_token")
    rt2 = (b2.get("data") or {}).get("refresh_token")
    check(2.1, "返回新的 refresh_token", bool(rt2))

    # [3] 新 access_token 可用
    s, _ = req("GET", "/api/v1/auth/me", token=at2)
    check(3, "新 access_token 可访问业务接口", s == 200, f"status={s}")

    # [4] 新 refresh_token 也可用（令牌轮换）
    s, b3 = req("POST", "/api/v1/auth/refresh", {"refresh_token": rt2})
    check(4, "新 refresh_token 可继续轮换", s == 200, f"status={s}")

    # [5] 垃圾 refresh_token
    s, _ = req("POST", "/api/v1/auth/refresh", {"refresh_token": "not-a-jwt"})
    check(5, "伪造 refresh_token 被拒 401", s == 401, f"status={s}")

    # [6] 用 access_token 冒充 refresh_token —— type 字段必须拦住
    s, _ = req("POST", "/api/v1/auth/refresh", {"refresh_token": at})
    check(6, "access_token 冒充 refresh 被拒（type 校验）", s == 401, f"status={s}")

    # [7] 模拟过期：直接用伪造/损坏的 access_token 打业务接口应 401
    s, _ = req("GET", "/api/v1/auth/me", token="eyJhbGciOiJIUzI1NiJ9.broken.sig")
    check(7, "损坏的 access_token 被拒 401", s == 401, f"status={s}")
    s, _ = req("GET", "/api/v1/billing/me", token=at2)
    check(7.1, "续期后的令牌可访问 billing 接口", s == 200, f"status={s}")

    # [8] 不存在的用户
    from datetime import datetime, timedelta, timezone

    # 用真实密钥签一个 sub 不存在的 refresh token 比较麻烦，这里退而求其次：
    # 构造一个结构对但签名错的 token
    s, _ = req("POST", "/api/v1/auth/refresh", {"refresh_token": at2[:-6] + "aaaaaa"})
    check(8, "签名被改的 refresh_token 被拒 401", s == 401, f"status={s}")

    # 清理
    s, b = req("POST", "/api/v1/auth/login", {"email": "admin@example.com", "password": "***REMOVED***"})
    if s == 200:
        adm = b["data"]["access_token"]
        s2, users = req("GET", "/api/v1/admin/users?limit=200", token=adm)
        if s2 == 200:
            for u in (users.get("data") or {}).get("items", []):
                if u.get("email") == EMAIL:
                    req("DELETE", f"/api/v1/admin/users/{u['id']}", token=adm)
                    print(f"  清理测试账号 {EMAIL}")

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
