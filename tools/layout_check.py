"""前台三页（首页 / 接口文档 / 用户控制台）的宽度与溢出自检。

用途：改 `.container` / `.nav-inner` 宽度、改卡片网格、改侧栏宽度之后，
一键确认"版面没被挤坏"。**不要靠读 CSS 推断** —— 折行和溢出是 layout
算出来的，只有真浏览器 + 真窗口宽度才有答案。

用法：
    python -u tools/layout_check.py                 # 默认测 1680 / 1440 两档
    python -u tools/layout_check.py 1680            # 只测一档
    python -u tools/layout_check.py --no-live       # 线上不可达时用占位夹具

产出：
  * 每个页面一张 PNG（.tmp/layout/ 下），人眼复核用
  * 一份纯文本断言结果（靠 --dump-dom 抓，可进 CI）

判定三件事：
  1. `html.scrollWidth` 不超过视口 —— 没有整页横向溢出
  2. 除"自身就是横向滚动容器"（overflow-x: auto/scroll，那是设计如此）外，
     没有元素 `scrollWidth > clientWidth`
  3. `.nav-inner` 与 `.container` 同宽 —— 不同宽导航条和正文就对不齐

注意（踩过的坑，见技能 headless-authed-page-snapshot）：
  * 截图/抓 DOM 都要带 `--virtual-time-budget`，否则异步渲染没跑完，
    截出来全是"加载中…"，会误判成"桩没生效"
  * 桩必须插在 app.js 的 script 标签**之后**、主内联脚本**之前**：
    插到 </head> 前面时 `SD` 还不存在，ReferenceError 让桩静默失效，
    boot() 拿 401 跳 /login.html，file:// 下就是一张「未找到文件」白页
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

from local_cfg import cfg

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "backend" / "static"
WORK = ROOT / ".tmp" / "layout"

# 地址与账号走本机 .env（以前写死 NAS 内网 IP 和口令，
# 2026-09-29 首次推公开仓库时一起泄露了）
BASE = cfg("STOCKDATA_BASE", "http://127.0.0.1:9850").rstrip("/") + "/api/v1"
EMAIL, PWD = cfg("STOCKDATA_ADMIN_EMAIL"), cfg("STOCKDATA_ADMIN_PASSWORD")

for _p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
           r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
    if pathlib.Path(_p).exists():
        EDGE = pathlib.Path(_p)
        break
else:
    EDGE = pathlib.Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

# ----------------------------------------------------------------- 夹具

#: 线上抓不到时的兜底（结构对就行，只为把卡片撑开看排版，不校验数据准确性）
FALLBACK = {
    "auth_me": {"data": {"id": 1, "email": EMAIL, "tier": "vip", "is_admin": True,
                         "is_active": True, "created_at": "2026-09-01T00:00:00",
                         "last_login": None}},
    "quota_me": {"data": {"email": EMAIL, "tier": "vip",
                          "plan": {"code": "vip", "name": "旗舰版", "max_keys": 50,
                                   "rate_limit": 1200, "daily_quota": -1},
                          "points": 10001, "plan_expires_at": None,
                          "plan_days_left": None,
                          "quota": {"max_keys": 50, "rate_limit": 1200,
                                    "daily_quota": -1, "unlimited": True,
                                    "active_keys": 0, "calls_24h": 756, "pct": 0}}},
    "quota_plans": {"data": {"items": [
        {"code": "free", "name": "免费版", "max_keys": 3, "rate_limit": 60,
         "daily_quota": 300, "redeem_points": None, "redeem_days": 30},
        {"code": "pro", "name": "专业版", "max_keys": 10, "rate_limit": 300,
         "daily_quota": 5000, "redeem_points": 15, "redeem_days": 30},
        {"code": "vip", "name": "旗舰版", "max_keys": 50, "rate_limit": 1200,
         "daily_quota": -1, "redeem_points": 30, "redeem_days": 30}]}},
    "points_me": {"data": {"points": 10001, "points_earned": 10001,
                           "can_checkin": False, "last_checkin_date": "2026-09-29",
                           "checkin_points": 1, "tier": "vip",
                           "plan": {"code": "vip", "name": "旗舰版"},
                           "plan_expires_at": None}},
    "usage": {"data": {
        "plan": {"code": "vip", "name": "旗舰版"},
        "daily": [{"date": "2026-09-23", "total": 0, "ok": 0, "err": 0}] * 13
                 + [{"date": "2026-09-29", "total": 756, "ok": 723, "err": 33}],
        "calls_24h": 756, "calls_7d": 756, "calls_total": 756,
        "errors_24h": 33, "error_rate": 4.37,
        "top_paths": [{"path": "/sources", "calls": 365}, {"path": "/kline", "calls": 135},
                      {"path": "/quote", "calls": 124}, {"path": "/special/limit_up", "calls": 31},
                      {"path": "/stock/list", "calls": 18}, {"path": "/special", "calls": 18}]}},
    "keys": {"data": []},
}

#: 首页 /public/market 的假数据
MARKET = {"code": 0, "msg": "ok", "data": {
    "indices": [
        {"code": "000001.SH", "name": "上证指数", "price": 3862.41, "change": 18.66, "change_pct": 0.49},
        {"code": "399001.SZ", "name": "深证成指", "price": 12984.55, "change": -62.13, "change_pct": -0.48},
        {"code": "399006.SZ", "name": "创业板指", "price": 3127.08, "change": 12.44, "change_pct": 0.40},
        {"code": "000688.SH", "name": "科创50", "price": 1428.96, "change": -5.71, "change_pct": -0.40}],
    "breadth": {"total": 5574, "up": 2681, "down": 2543, "flat": 350,
                "limit_up": 58, "limit_down": 9, "avg_pct": 0.31, "up_ratio": 48.1},
    "top_gainers": [{"code": "300750.SZ", "name": "宁德时代", "price": 402.66, "change_pct": 10.99},
                    {"code": "002594.SZ", "name": "比亚迪", "price": 118.42, "change_pct": 9.97},
                    {"code": "601318.SH", "name": "中国平安", "price": 62.85, "change_pct": 7.64},
                    {"code": "000858.SZ", "name": "五粮液", "price": 152.30, "change_pct": 6.28},
                    {"code": "600036.SH", "name": "招商银行", "price": 44.19, "change_pct": 5.11}],
    "top_losers": [{"code": "000001.SZ", "name": "平安银行", "price": 11.35, "change_pct": -6.42},
                   {"code": "600519.SH", "name": "贵州茅台", "price": 1235.58, "change_pct": -4.18},
                   {"code": "600000.SH", "name": "浦发银行", "price": 9.18, "change_pct": -3.55},
                   {"code": "601988.SH", "name": "中国银行", "price": 5.72, "change_pct": -2.91},
                   {"code": "601398.SH", "name": "工商银行", "price": 7.04, "change_pct": -2.30}]}}


def grab_live() -> dict | None:
    """从线上抓一份真实响应当夹具（字段最保真）。抓不到就返回 None。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(p, m="GET", b=None, tok=None):
        d = json.dumps(b).encode() if b is not None else None
        r = urllib.request.Request(BASE + p, data=d, method=m)
        r.add_header("Content-Type", "application/json")
        if tok:
            r.add_header("Authorization", "Bearer " + tok)
        with op.open(r, timeout=20) as resp:
            return json.loads(resp.read().decode())

    try:
        tok = (call("/auth/login", "POST", {"email": EMAIL, "password": PWD})
               .get("data") or {}).get("access_token")
        if not tok:
            return None
        out = {}
        for k, p in (("auth_me", "/auth/me"), ("quota_me", "/quota/me"),
                     ("quota_plans", "/quota/plans"), ("points_me", "/points/me"),
                     ("usage", "/quota/usage?days=7"), ("keys", "/apikey")):
            out[k] = call(p, tok=tok)
        print("  夹具：线上真实响应")
        return out
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"  夹具：线上不可达（{exc}），用占位数据")
        return None


# ----------------------------------------------------------------- 页面构建

PROBE = """
<script>
window.addEventListener('load', () => {
  setTimeout(async () => {
    const de = document.documentElement;
    const bad = [];
    document.querySelectorAll('*').forEach((el) => {
      if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
        const st = getComputedStyle(el);
        if (st.overflowX === 'auto' || st.overflowX === 'scroll') return;  // 设计如此
        bad.push(el.tagName.toLowerCase() + '.' + String(el.className || '').split(' ')[0]
                 + ' ' + el.clientWidth + '->' + el.scrollWidth);
      }
    });
    const nav = document.querySelector('.nav-inner');
    const box = document.querySelector('.container');
    const navW = nav ? Math.round(nav.getBoundingClientRect().width) : -1;
    const boxW = box ? Math.round(box.getBoundingClientRect().width) : -1;
    const diag = {viewport: innerWidth, docScrollWidth: de.scrollWidth,
                  navInner: navW, container: boxW, overflow: bad};
    const el = document.createElement('div');
    el.id = '__diag';
    el.style.display = 'none';
    el.textContent = JSON.stringify(diag);
    document.body.appendChild(el);

    const bar = document.createElement('div');
    bar.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:9999;padding:8px 14px;'
      + 'font:12px/1.6 monospace;background:#111c33;color:#cfe1ff;'
      + 'border-bottom:1px solid #3d7dff;white-space:pre-wrap;';
    bar.textContent = `视口=${innerWidth}  html.scrollWidth=${de.scrollWidth} `
      + `(横向溢出=${de.scrollWidth > innerWidth})  nav-inner=${navW}  .container=${boxW}  `
      + `对齐=${navW === boxW}  溢出元素 ${bad.length} 个: ` + (bad.slice(0, 6).join(' | ') || '无');
    document.body.prepend(bar);
  }, 600);
});
</script>
"""


def build(page: str, view: str | None, fixture: dict) -> pathlib.Path:
    WORK.mkdir(parents=True, exist_ok=True)
    snap = WORK / "static"
    snap.mkdir(parents=True, exist_ok=True)
    for f in ("style.css", "app.js"):
        (snap / f).write_bytes((STATIC / f).read_bytes())

    html = (STATIC / f"{page}.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/', 'href="./static/')
    html = html.replace('src="/static/', 'src="./static/')

    pre = ""
    if page == "index":
        #: 注意别写成 `json: async () => { ... }` —— 那会被当成 block 而非
        #: 对象字面量，整个 script 语法错误，桩静默失效（页面会显示
        #: "行情暂时不可用"，很像首页有 bug）。先赋给 const 最稳。
        pre = ("<script>\n"
               f"const FAKE_MARKET = {json.dumps(MARKET, ensure_ascii=False)};\n"
               "window.fetch = async function () "
               "{ return { ok: true, json: async () => FAKE_MARKET }; };\n"
               "</script>")
    elif page == "console":
        pre = ("<script>\n"
               "Object.defineProperty(SD, 'token', { get: () => 'snap', set: () => {} });\n"
               "Object.defineProperty(SD, 'refreshToken', { get: () => '', set: () => {} });\n"
               f"const FX = {json.dumps(fixture, ensure_ascii=False)};\n"
               "SD.api = async function (p) {\n"
               "  if (p.startsWith('/auth/me'))     return FX.auth_me;\n"
               "  if (p.startsWith('/quota/me'))    return FX.quota_me;\n"
               "  if (p.startsWith('/quota/plans')) return FX.quota_plans;\n"
               "  if (p.startsWith('/points/me'))   return FX.points_me;\n"
               "  if (p.startsWith('/quota/usage')) return FX.usage;\n"
               "  if (p.startsWith('/apikey'))      return FX.keys;\n"
               "  return { data: { total: 0, items: [] } };\n"
               "};\n"
               + (f"window.addEventListener('load', () => setTimeout(() => go('{view}'), 400));\n"
                  if view else "")
               + "</script>")

    if pre:
        #: 必须插在 app.js 之后、主内联脚本之前 —— 插到 </head> 前面时
        #: `SD` 还不存在，ReferenceError 会让桩静默失效
        anchor = '<script src="./static/app.js"></script>'
        if anchor in html:
            html = html.replace(anchor, anchor + pre, 1)
        else:
            html = html.replace("</head>", pre + "</head>", 1)
    html = html.replace("</body>", PROBE + "</body>", 1)

    out = WORK / f"{page}{'_' + view if view else ''}.html"
    out.write_text(html, encoding="utf-8")
    return out


def edge(args: list[str], url: str, tag: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(EDGE), "--headless=new", "--disable-gpu", "--no-first-run",
         f"--user-data-dir={WORK / ('prof_' + tag)}",
         "--virtual-time-budget=6000", *args, url],
        capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")


def main() -> None:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    widths = [int(a) for a in argv] or [1680, 1440]
    fixture = FALLBACK if "--no-live" in sys.argv else (grab_live() or FALLBACK)

    targets = [("index", None), ("docs", None),
               ("console", "overview"), ("console", "quota"), ("console", "usage")]
    fails: list[str] = []
    checks = 0

    for page, view in targets:
        path = build(page, view, fixture)
        tag = f"{page}_{view}" if view else page
        for w in widths:
            r = edge(["--window-size=%d,1400" % w, "--dump-dom"], path.as_uri(), f"d_{tag}_{w}")
            m = re.search(r'id="__diag"[^>]*>(.*?)</div>', r.stdout or "", re.S)
            if not m:
                fails.append(f"{tag}@{w} 抓不到诊断节点（页面可能没渲染）")
                continue
            d = json.loads(m.group(1))
            checks += 1
            problems = []
            if d["docScrollWidth"] > d["viewport"]:
                problems.append(f"整页横向溢出 {d['docScrollWidth']}>{d['viewport']}")
            if d["overflow"]:
                problems.append("元素溢出: " + " | ".join(d["overflow"][:4]))
            if d["navInner"] > 0 and d["container"] > 0 and d["navInner"] != d["container"]:
                problems.append(f"nav-inner({d['navInner']}) != container({d['container']})")
            status = "OK  " if not problems else "FAIL"
            print(f"  [{status}] {tag:<18} @{w:<5} nav={d['navInner']} box={d['container']} "
                  f"docW={d['docScrollWidth']} 溢出={len(d['overflow'])}"
                  + ("  <- " + "; ".join(problems) if problems else ""))
            if problems:
                fails.extend(f"{tag}@{w}: {p}" for p in problems)
            # 出图供人眼复核（诊断条会画在页面顶部）
            edge(["--window-size=%d,1400" % w,
                  f"--screenshot={WORK / f'{tag}_{w}.png'}"], path.as_uri(), f"s_{tag}_{w}")

    print()
    print("=" * 66)
    if fails:
        print(f"版面自检：{checks} 个组合，发现 {len(fails)} 个问题")
        for f in fails:
            print("  ✗", f)
    else:
        print(f"版面自检：{checks} 个组合全部通过（无横向溢出、导航与正文同宽）")
    print(f"截图目录：{WORK}")
    print("=" * 66)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
