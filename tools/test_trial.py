"""验证「新用户 15 天试用期」+ 到期停止 API 调用。

用 FastAPI 的 TestClient 打本地 SQLite，不用起服务。
覆盖：
  1. 注册即得 15 天试用期（tier=pro / is_trial=True / 到期时间≈15 天后）
  2. /billing/me 正确返回到期信息与剩余天数
  3. 试用期内能正常调数据接口
  4. 把到期时间改成过去 -> 数据接口返回 402，且 /billing 仍可用（能续费）
  5. 管理员豁免（不会把自己锁在外面）
  6. 购买套餐后 is_trial 清零
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402

init_db(seed=True)
client = TestClient(app)

PASS = FAIL = 0


def ck(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def reg(email: str):
    return client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123",
    })


def login(email: str):
    r = client.post("/api/v1/auth/login", json={
        "email": email, "password": "testpass123",
    })
    return r.json()["data"]["access_token"]


def set_expiry(email: str, dt: datetime | None, trial: bool | None = None):
    """直接改数据库，模拟"时间过去了"。"""
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        u.plan_expires_at = dt
        if trial is not None:
            u.is_trial = trial
        db.commit()
    finally:
        db.close()


def main() -> int:
    stamp = datetime.utcnow().strftime("%H%M%S")
    email = f"trial{stamp}@example.com"
    admin_email = f"admin{stamp}@example.com"

    print("\n=== 1. 注册即发放 15 天试用期 ===")
    r = reg(email)
    if r.status_code != 200:
        print("  注册返回:", r.text[:300])
    ck("注册成功", r.status_code == 200, f"{r.status_code}")
    tok = login(email)
    me = client.get("/api/v1/billing/me", headers={"Authorization": f"Bearer {tok}"})
    d = me.json()["data"]
    ck("套餐为 pro（试用档）", d["plan"]["code"] == "pro", d["plan"]["code"])
    ck("is_trial=True", d.get("is_trial") is True, str(d.get("is_trial")))
    exp = d.get("plan_expires_at")
    ck("有到期时间", bool(exp), str(exp))
    ck("剩余 14 天（15 天向下取整到天）", d.get("days_left") == 14, str(d.get("days_left")))
    ck("未过期", d.get("expired") is False, str(d.get("expired")))

    print("\n=== 2. 试用期内可以正常调数据接口 ===")
    r = client.get("/api/v1/quote?codes=600519.SH",
                   headers={"Authorization": f"Bearer {tok}"})
    ck("行情接口放行", r.status_code == 200, f"{r.status_code}")

    print("\n=== 3. 到期后停止 API 调用 ===")
    set_expiry(email, datetime.utcnow() - timedelta(days=1))
    r = client.get("/api/v1/quote?codes=600519.SH",
                   headers={"Authorization": f"Bearer {tok}"})
    ck("行情接口返回 402", r.status_code == 402, f"{r.status_code} {r.text[:60]}")
    ck("提示文案提到试用期/购买", "试用" in r.text or "购买" in r.text, r.text[:80])

    print("\n=== 4. 到期后仍要能进计费页续费（不能被锁死） ===")
    me = client.get("/api/v1/billing/me", headers={"Authorization": f"Bearer {tok}"})
    ck("billing/me 仍可用", me.status_code == 200, f"{me.status_code}")
    ck("标记已过期", me.json()["data"].get("expired") is True)
    pl = client.get("/api/v1/billing/plans", headers={"Authorization": f"Bearer {tok}"})
    ck("套餐列表仍可用", pl.status_code == 200, f"{pl.status_code}")

    print("\n=== 5. 管理员豁免（不把自己锁在外面） ===")
    reg(admin_email)   # 库里已有用户，所以这个不是管理员……
    #: 直接把上面这个账号提为管理员来测豁免
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == admin_email).first()
        u.is_admin = True
        u.plan_expires_at = datetime.utcnow() - timedelta(days=1)
        u.is_trial = True
        db.commit()
    finally:
        db.close()
    atok = login(admin_email)
    r = client.get("/api/v1/quote?codes=600519.SH",
                   headers={"Authorization": f"Bearer {atok}"})
    ck("管理员过期也能调用", r.status_code == 200, f"{r.status_code}")

    print("\n=== 6. 试用期结束后购买套餐 -> 脱离试用 ===")
    set_expiry(email, datetime.utcnow() - timedelta(days=1))
    #: 先给余额（走后台式的直接改库，避免依赖支付渠道）
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        u.balance = 999000
        db.commit()
    finally:
        db.close()
    r = client.post("/api/v1/billing/buy",
                    json={"plan_code": "pro", "period": "month"},
                    headers={"Authorization": f"Bearer {tok}"})
    ck("购买成功", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    if r.status_code == 200:
        me = client.get("/api/v1/billing/me", headers={"Authorization": f"Bearer {tok}"})
        d = me.json()["data"]
        ck("is_trial 已清零", d.get("is_trial") is False, str(d.get("is_trial")))
        ck("到期时间顺延到约 30 天后", (d.get("days_left") or 0) >= 29,
           str(d.get("days_left")))
        r2 = client.get("/api/v1/quote?codes=600519.SH",
                        headers={"Authorization": f"Bearer {tok}"})
        ck("购买后接口恢复", r2.status_code == 200, f"{r2.status_code}")

    print(f"\n结果: 通过 {PASS} 失败 {FAIL}")
    return 1 if FAIL else 0


sys.exit(main())
