"""清理测试脚本产生的用户 / 密钥 / 日志 / 订单 / 流水 / 测试套餐。

保留真实账号（admin@example.com）与内置的 free / pro / vip 三档套餐。

测试脚本异常退出时不会执行自己的清理分支，残留会污染下一次运行——
最典型的是 test_billing 造的临时套餐（team12345）留在库里，
导致下一次运行断言"公开套餐只有 3 个"失败。所以这里要一并清掉。
"""

import re
import sys

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import ApiKey, ApiLog, BalanceLog, Order, Plan, User  # noqa: E402

PREFIXES = ("purge", "other", "e2e", "smoke", "tmp", "adm", "billing")
DOMAINS = ("@stockdata.dev", "@test.dev", "@example.dev")

#: 内置套餐，绝不能删
BUILTIN_PLANS = {"free", "pro", "vip"}
#: 测试脚本造的临时套餐：team<数字>
TEST_PLAN_RE = re.compile(r"^(team|test|tmp)\d*$")

init_db()
db = SessionLocal()

users = db.scalars(select(User)).all()
targets = [
    u for u in users
    if u.email.split("@")[0].lower().startswith(PREFIXES)
    or u.email.lower().endswith(DOMAINS)
]

print(f"当前用户数: {len(users)}  命中测试账号: {len(targets)}")
if not targets:
    print("无需清理")
    db.close()
    sys.exit(0)

for u in targets:
    print(f"  - {u.email} (id={u.id}, tier={u.tier}, admin={u.is_admin})")

confirm = "--yes" in sys.argv
if not confirm:
    print("\n加 --yes 才会真正删除")
    db.close()
    sys.exit(0)

n_key = n_log = n_order = n_bill = 0
for u in targets:
    keys = db.scalars(select(ApiKey).where(ApiKey.user_id == u.id)).all()
    for k in keys:
        n_log += db.query(ApiLog).filter(ApiLog.key_id == k.id).delete(
            synchronize_session=False
        )
        db.delete(k)
        n_key += 1
    n_log += db.query(ApiLog).filter(ApiLog.user_id == u.id).delete(
        synchronize_session=False
    )
    # 订单与余额流水必须随用户一起删：SQLite 的 INTEGER PRIMARY KEY 会复用 id，
    # 只删 users 行会让下一个新建用户"继承"这些历史订单/流水
    n_order += db.query(Order).filter(Order.user_id == u.id).delete(
        synchronize_session=False
    )
    n_bill += db.query(BalanceLog).filter(BalanceLog.user_id == u.id).delete(
        synchronize_session=False
    )
    db.delete(u)

# 清掉所有指向已不存在 key 的孤儿日志（key_id 非空的）
n_log += db.query(ApiLog).filter(
    ApiLog.key_id.isnot(None),
    ApiLog.key_id.notin_(select(ApiKey.id)),
).delete(synchronize_session=False)

db.commit()

# 清掉指向已不存在用户的孤儿订单 / 余额流水
alive_ids = select(User.id)
n_order += db.query(Order).filter(Order.user_id.notin_(alive_ids)).delete(
    synchronize_session=False
)
n_bill += db.query(BalanceLog).filter(BalanceLog.user_id.notin_(alive_ids)).delete(
    synchronize_session=False
)

db.commit()

# ---- 清理测试脚本造的临时套餐 ----
plans = db.scalars(select(Plan)).all()
del_plans = [
    p for p in plans
    if p.code not in BUILTIN_PLANS and TEST_PLAN_RE.match(p.code or "")
]
for p in del_plans:
    print(f"  - 套餐 {p.code} ({p.name})")
    db.delete(p)
db.commit()

# 被删套餐的用户回落 free，避免 tier 指向不存在的套餐
alive_codes = {p.code for p in db.scalars(select(Plan)).all()}
n_fix = 0
for u in db.scalars(select(User)).all():
    if u.tier not in alive_codes:
        print(f"  ~ 用户 {u.email} 的 tier={u.tier} 已不存在，回落 free")
        u.tier = "free"
        n_fix += 1
if n_fix:
    db.commit()

left = db.scalars(select(User)).all()
print(
    f"\n已删除: 用户 {len(targets)} / 密钥 {n_key} / 日志 {n_log} "
    f"/ 订单 {n_order} / 流水 {n_bill} / 测试套餐 {len(del_plans)}"
)
if n_fix:
    print(f"        修正悬空套餐归属 {n_fix} 个用户")
print("剩余用户:")
for u in left:
    print(f"  * {u.email} (tier={u.tier}, admin={u.is_admin})")
print("剩余套餐:", [p.code for p in db.scalars(select(Plan)).all()])
db.close()
