"""签到积分与档位兑换链路测试。

覆盖：签到幂等 / 兑换 / 积分不足 / 降级拒绝 / 同档续期 / 到期回落 / 流水留痕。

用法：python -u test_points.py
"""

from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import PointLog, User  # noqa: E402

BASE = "/api/v1"
init_db()
client = TestClient(app)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}  {detail}")


def uniq(prefix: str) -> str:
    return f"{prefix}{int(time.time())}{random.randint(1000, 9999)}@stockdata.dev"


def main() -> int:
    email = uniq("pts")
    pwd = "test12345"

    r = client.post(f"{BASE}/auth/register", json={"email": email, "password": pwd})
    check("注册测试账号", r.status_code == 200, str(r.status_code))
    if r.status_code != 200:
        return 1

    r = client.post(f"{BASE}/auth/login", json={"email": email, "password": pwd})
    tok = r.json()["data"]["access_token"]
    H = {"Authorization": f"Bearer {tok}"}

    # ---- 1. 签到
    r = client.post(f"{BASE}/points/checkin", headers=H)
    d = r.json()["data"]
    check("首次签到得 1 分", r.status_code == 200 and d["points"] == 1 and d["gained"] == 1,
          str(d))

    r = client.post(f"{BASE}/points/checkin", headers=H)
    d = r.json()["data"]
    check("同日重复签到不加分", d.get("already") is True and d["points"] == 1, str(d))

    # ---- 2. 档位列表带兑换价
    r = client.get(f"{BASE}/quota/plans", headers=H)
    plans = r.json()["data"]["items"]
    by_code = {p["code"]: p for p in plans}
    check("专业版兑换价 15 分", (by_code.get("pro") or {}).get("redeem_points") == 15,
          str((by_code.get("pro") or {}).get("redeem_points")))
    check("旗舰版兑换价 30 分", (by_code.get("vip") or {}).get("redeem_points") == 30,
          str((by_code.get("vip") or {}).get("redeem_points")))
    check("免费版不可兑换", not (by_code.get("free") or {}).get("redeem_points"),
          str((by_code.get("free") or {}).get("redeem_points")))

    # ---- 3. 积分不足
    r = client.post(f"{BASE}/points/redeem", json={"plan_code": "pro"}, headers=H)
    check("积分不足被拒(400)", r.status_code == 400, r.json().get("detail"))

    # ---- 4. 补足积分后兑换
    s = SessionLocal()
    u = s.scalar(select(User).where(User.email == email))
    uid = u.id
    u.points = 15
    s.commit()
    s.close()

    r = client.post(f"{BASE}/points/redeem", json={"plan_code": "pro"}, headers=H)
    d = r.json()["data"]
    ok = r.status_code == 200 and d["tier"] == "pro" and d["points_left"] == 0
    check("兑换专业版", ok, r.json().get("msg"))
    if ok:
        exp = datetime.fromisoformat(str(d["expires_at"]).replace("Z", ""))
        days = (exp - datetime.utcnow()).days
        check("有效期 30 天", 29 <= days <= 30, f"{days} 天")

    # ---- 5. 降级兑换
    s = SessionLocal()
    u = s.get(User, uid)
    u.tier = "vip"
    u.points = 30
    s.commit()
    s.close()
    r = client.post(f"{BASE}/points/redeem", json={"plan_code": "pro"}, headers=H)
    check("降级兑换被拒(400)", r.status_code == 400, r.json().get("detail"))

    # ---- 6. 同档续期
    r = client.post(f"{BASE}/points/redeem", json={"plan_code": "vip"}, headers=H)
    d = r.json()["data"] if r.status_code == 200 else {}
    check("同档兑换=续期", r.status_code == 200 and d.get("action") == "续期",
          r.json().get("msg"))

    # ---- 7. 到期回落（关键：只降档，不断服）
    s = SessionLocal()
    u = s.get(User, uid)
    u.plan_expires_at = datetime.utcnow() - timedelta(days=1)
    s.commit()
    s.close()
    r = client.get(f"{BASE}/quota/me", headers=H)
    got = r.json()["data"]
    check("到期回落免费版", got["tier"] == "free", got["tier"])
    check("回落后服务不中断（接口仍 200）", r.status_code == 200, str(r.status_code))

    # ---- 8. 流水留痕
    r = client.get(f"{BASE}/points/logs?limit=50", headers=H)
    items = r.json()["data"]["items"]
    kinds = {i["type"] for i in items}
    #: 签到 1 条 + 换 pro 1 条 + 续期 vip 1 条 = 3 条。
    #: 第二次签到因为幂等被挡下，**不应该**产生流水 —— 否则就是刷分漏洞。
    check("积分流水有记录", len(items) == 3, f"{len(items)} 条")
    check("流水含签到与兑换", {"checkin", "redeem"} <= kinds, str(kinds))

    # ---- 9. 清理
    s = SessionLocal()
    s.query(PointLog).filter(PointLog.user_id == uid).delete(synchronize_session=False)
    u = s.get(User, uid)
    if u:
        s.delete(u)
    s.commit()
    s.close()

    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n结果：通过 {len(RESULTS) - len(bad)} 项，失败 {len(bad)} 项")
    for n in bad:
        print("   ·", n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
