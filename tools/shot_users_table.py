"""给「用户管理」表格拍离线快照，用来验证折行问题是否真的修好。

为什么要离线快照：后台要登录才能看到真实表格，headless 截图没法输入账号密码。
这里把 admin.html / style.css / app.js 复制一份，把 SD.api 换成返回固定假数据
（数据照着航哥那张截图造），再塞进一个关掉新规则的对照版本，
就能在本地浏览器里同时得到"改前 / 改后"两张图直接比对。

用法：python tools/shot_users_table.py
产出：.tmp/shot/after-*.png（改后）、.tmp/shot/before-*.png（对照）
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "backend" / "static"
# 截图产出放 outputs/（要给人看的），离线快照和 Edge 临时 profile 放 .tmp/
OUT = ROOT / "outputs" / "users-table-shot"
WORK = ROOT / ".tmp" / "shot"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 照着航哥截图里的两行用户造数据，长度、字段都对齐真实接口返回
FAKE_USERS = [
    {
        "id": 2, "email": "someone_long_address@example.com",
        "tier": "pro", "is_admin": False, "is_active": True,
        "active_keys": 0, "key_quota": 10, "calls_24h": 1,
        "created_at": "2026-09-29T03:59:26",
    },
    {
        "id": 1, "email": "you@example.com",
        "tier": "vip", "is_admin": True, "is_active": True,
        "active_keys": 0, "key_quota": 50, "calls_24h": 503,
        "created_at": "2026-09-29T02:17:33",
    },
]

FAKE_PLANS = [
    {"code": "free", "name": "免费版", "level": 0,
     "max_keys": 3, "rate_limit": 60, "daily_quota": 1000, "is_active": True, "users": 0},
    {"code": "pro", "name": "专业版", "level": 10,
     "max_keys": 10, "rate_limit": 300, "daily_quota": 50000, "is_active": True, "users": 1},
    {"code": "vip", "name": "旗舰版", "level": 20,
     "max_keys": 50, "rate_limit": 1200, "daily_quota": -1, "is_active": True, "users": 1},
]

FAKE_STATS = {
    "users": 2, "users_new_7d": 2, "users_active_7d": 2, "users_disabled": 0,
    "keys": 0, "api_keys": 0, "active_keys": 0, "revoked_keys": 0,
    "calls_24h": 504, "calls_prev_24h": 0, "calls_delta_pct": None,
    "error_rate": 1.2, "errors_24h": 6, "avg_latency_ms": 128, "p95_latency_ms": 240,
    "hourly": [], "top_paths": [], "status_dist": {"s2xx": 498, "s4xx": 6, "s5xx": 0},
    "tiers": [], "tier_counts": {},
    # 缺这两个字段概览页会抛 "Cannot read properties of undefined"，
    # 然后顶出一条红色错误横幅，把截图弄脏（跟本次改动无关，纯 stub 不完整）
    "system": {
        "redis": {"available": True, "error": ""},
        "postgres": {"available": True, "error": ""},
    },
}

# 注入到 app.js 之后、主脚本之前：伪造登录态 + 拦截所有接口请求
STUB = """
<script>
/* --- 快照专用：伪造登录态，不碰 localStorage（file:// 下可能被禁） --- */
Object.defineProperty(SD, 'token', { get: () => 'snapshot', set: () => {} });
Object.defineProperty(SD, 'refreshToken', { get: () => '', set: () => {} });

const FAKE = __FAKE__;
SD.api = async function (path) {
  if (path.startsWith('/auth/me'))      return { data: { id: 1, email: 'you@example.com', is_admin: true } };
  if (path.startsWith('/admin/plans'))  return { data: FAKE.plans };
  if (path.startsWith('/admin/users'))  return { data: { total: FAKE.users.length, items: FAKE.users } };
  if (path.startsWith('/admin/stats'))  return { data: FAKE.stats };
  if (path.startsWith('/sources'))      return { data: [] };
  return { data: { total: 0, items: [] } };
};
/* boot() 结尾会 switchView('overview')，等它跑完再切到用户管理 */
window.addEventListener('load', () => setTimeout(() => switchView('users'), 400));
</script>
"""

# 关掉本轮新增的三条规则，复现"改前"的样子做对照
REVERT = """
<link rel="stylesheet" href="before-revert.css">
"""

REVERT_CSS = """/* 对照用：把本轮新增的禁止折行规则全部退回原始行为 */
th { white-space: normal !important; }
.status { white-space: nowrap; }
.status { white-space: normal !important; }
.table-nowrap th, .table-nowrap td { white-space: normal !important; }
.table-nowrap .cell-main { max-width: none; overflow: visible; text-overflow: clip; }
.table-nowrap th, .table-nowrap td { padding-left: 14px !important; padding-right: 14px !important; }
.table-nowrap .btn-sm { padding: 7px 14px !important; font-size: 13px !important; }
"""


def build_html(variant: str) -> str:
    html = (STATIC / "admin.html").read_text(encoding="utf-8")
    # file:// 下 "/static/xxx" 会解析到磁盘根目录，改成相对路径
    html = html.replace('href="/static/', 'href="./static/')
    html = html.replace('src="/static/', 'src="./static/')
    stub = STUB.replace("__FAKE__", json.dumps({
        "users": FAKE_USERS, "plans": FAKE_PLANS, "stats": FAKE_STATS,
    }, ensure_ascii=False))
    # 插到 app.js 之后（此时 SD 已存在，且主脚本还没开始跑）
    marker = '<script src="./static/app.js"></script>'
    if marker not in html:
        raise SystemExit("没找到 app.js 的 script 标签，admin.html 结构变了")
    html = html.replace(marker, marker + stub, 1)
    if variant == "before":
        html = html.replace("</head>", REVERT + "</head>", 1)
    return html


def shoot(edge: str, html: Path, png: Path, width: int, height: int) -> bool:
    cmd = [
        edge, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--hide-crash-restore-bubble",
        f"--user-data-dir={WORK / 'profile'}",
        f"--window-size={width},{height}",
        "--virtual-time-budget=4000",
        f"--screenshot={png}",
        html.as_uri(),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not png.exists():
        print(f"  [失败] {png.name}  Edge rc={r.returncode} {r.stderr[:300]}")
        return False
    print(f"  [OK]   {png.name}  ({png.stat().st_size // 1024} KB)")
    return True


def main() -> int:
    edge = next((p for p in EDGE_CANDIDATES if Path(p).exists()), None)
    if not edge:
        print("找不到 Edge，无法截图")
        return 2

    for d in (OUT, WORK):
        if d.exists():
            shutil.rmtree(d)
    OUT.mkdir(parents=True)
    (WORK / "static").mkdir(parents=True)
    for name in ("style.css", "app.js"):
        shutil.copy2(STATIC / name, WORK / "static" / name)
    (WORK / "before-revert.css").write_text(REVERT_CSS, encoding="utf-8")

    (WORK / "after.html").write_text(build_html("after"), encoding="utf-8")
    (WORK / "before.html").write_text(build_html("before"), encoding="utf-8")

    print("渲染快照：")
    ok = True
    # 1680 宽 ≈ 1640px 容器满宽（版面改成 1640 后的目标场景）：
    # 期望整表不折行、也不出横向滚动条
    ok &= shoot(edge, WORK / "after.html", OUT / "after-1680.png", 1680, 1000)
    # 1100 宽 ≈ 那张截图的窗口宽度（≤1100 时侧栏会变成顶部横条）：
    # 期望表头/徽章仍不折行，改由横向滚动条兜底
    ok &= shoot(edge, WORK / "after.html", OUT / "after-1100.png", 1100, 1000)
    # 对照：把本轮新增的规则全部退回，复现"KEY 用量"裂成两行的原状
    ok &= shoot(edge, WORK / "before.html", OUT / "before-1100.png", 1100, 1000)
    print(f"\n输出目录：{OUT}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
