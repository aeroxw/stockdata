"""清理测试脚本产生的用户 / 密钥 / 日志 / 测试档位。

保留真实账号（you@example.com）与内置的 free / pro / vip 三档额度。

测试脚本异常退出时不会执行自己的清理分支，残留会污染下一次运行——
最典型的是测试脚本造的临时档位（team12345）留在库里，
导致下一次运行断言"档位只有 3 个"失败。所以这里要一并清掉。
"""

import re
import sys

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import ApiKey, ApiLog, Plan, User  # noqa: E402

PREFIXES = ("purge", "other", "e2e", "smoke", "tmp", "adm")
DOMAINS = ("@stockdata.dev", "@test.dev", "@example.dev")

#: 内置档位，绝不能删
BUILTIN_PLANS = {"free", "pro", "vip"}
#: 测试脚本造的临时档位：team<数字>
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

n_key = n_log = 0
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
    db.delete(u)

# 清掉所有指向已不存在 key 的孤儿日志（key_id 非空的）
n_log += db.query(ApiLog).filter(
    ApiLog.key_id.isnot(None),
    ApiLog.key_id.notin_(select(ApiKey.id)),
).delete(synchronize_session=False)

db.commit()

# ---- 清理测试脚本造的临时档位 ----
plans = db.scalars(select(Plan)).all()
del_plans = [
    p for p in plans
    if p.code not in BUILTIN_PLANS and TEST_PLAN_RE.match(p.code or "")
]
for p in del_plans:
    print(f"  - 档位 {p.code} ({p.name})")
    db.delete(p)
db.commit()

# 被删档位的用户回落 free，避免 tier 指向不存在的档位
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
    f"/ 测试档位 {len(del_plans)}"
)
if n_fix:
    print(f"        修正悬空档位归属 {n_fix} 个用户")
print("剩余用户:")
for u in left:
    print(f"  * {u.email} (tier={u.tier}, admin={u.is_admin})")
print("剩余档位:", [p.code for p in db.scalars(select(Plan)).all()])
db.close()
