"""浏览器端到端验证自动续期。

关键场景：localStorage 里放一个【已过期的 access_token】+ 一个【有效的 refresh_token】，
打开 console.html，页面应当自动续期并把所有数据渲染出来，而不是跳登录页。
"""

import json
import pathlib
import re
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from jose import jwt

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE = "http://127.0.0.1:9850"
STATIC = pathlib.Path(__file__).resolve().parent / "static"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path, body, tries=6):
    last = None
    for _ in range(tries):
        req = urllib.request.Request(
            BASE + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with _opener.open(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6)
    raise last


def make_expired_access_token():
    """用真实密钥签一个 1 小时前就过期的 access_token。"""
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from app.config import settings

    exp = datetime.now(timezone.utc) - timedelta(hours=1)
    return jwt.encode(
        {"sub": "1", "exp": exp, "type": "access"},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def build():
    d = post("/api/v1/auth/login",
             {"email": "admin@example.com", "password": "***REMOVED***"})
    rt = d["data"]["refresh_token"]
    expired_at = make_expired_access_token()

    html = (STATIC / "console.html").read_text(encoding="utf-8")
    seed = (
        "<script>"
        f'localStorage.setItem("sd_token", {json.dumps(expired_at)});'
        f'localStorage.setItem("sd_refresh", {json.dumps(rt)});'
        "</script>"
    )
    guard = """
<script>
window.__log = [];
window.addEventListener('error', function (e) {
  window.__log.push('error: ' + e.message);
}, true);
window.addEventListener('unhandledrejection', function (e) {
  var r = e.reason;
  window.__log.push('rej: ' + (r && r.message ? r.message : String(r)));
});
</script>
"""
    html = html.replace('<script src="/static/app.js"></script>',
                        guard + seed + '<script src="/static/app.js"></script>', 1)

    tail = """
<script>
window.addEventListener('load', function () {
  setTimeout(function () {
    var pg = (document.getElementById('planGrid') || {}).innerHTML || '';
    var box = document.createElement('pre');
    box.id = '__probe_out';
    box.textContent = 'PJR_START' + JSON.stringify({
      href: location.pathname,
      log: window.__log.slice(0, 12),
      tokenChanged: localStorage.getItem('sd_token') !== TOKEN_WAS,
      hasRefresh: !!localStorage.getItem('sd_refresh'),
      userInfo: (document.getElementById('userInfo') || {}).textContent || '',
      topTier: (document.getElementById('topTier') || {}).textContent || '',
      topBalance: (document.getElementById('topBalance') || {}).textContent || '',
      // 首屏进的是「概览」视图，检查它的卡片是否填好；
      // 套餐卡片属于另一个视图，不点侧栏本就不该加载
      ovPlan: (document.getElementById('ovPlan') || {}).textContent || '',
      ovBenefitsLen: ((document.getElementById('ovBenefits') || {}).innerHTML || '').length,
      keyWrapLen: ((document.getElementById('keyTableWrap') || {}).innerHTML || '').length,
    }) + 'PJR_END';
    document.body.appendChild(box);
  }, 5000);
});
var TOKEN_WAS = localStorage.getItem('sd_token');
</script>
"""
    html = html.replace("</body>", tail + "</body>", 1)
    (STATIC / "_probe_refresh.html").write_text(html, encoding="utf-8")


def run():
    prof = pathlib.Path(tempfile.gettempdir()) / "sd_probe_ref"
    prof.mkdir(parents=True, exist_ok=True)
    cmd = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", f"--user-data-dir={prof}",
           "--virtual-time-budget=12000", "--dump-dom",
           f"{BASE}/static/_probe_refresh.html"]
    r = subprocess.run(cmd, capture_output=True, timeout=180)
    dom = r.stdout.decode("utf-8", "ignore")
    tp = dom.rfind("PJR_START")
    if tp < 0:
        print("没拿到结果，DOM 尾部:", dom[-1500:])
        return None
    raw = dom[tp + len("PJR_START"):dom.find("PJR_END", tp)]
    for a, b in (("&quot;", '"'), ("&#34;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        raw = raw.replace(a, b)
    return json.loads(raw)


if __name__ == "__main__":
    build()
    res = run()
    if not res:
        raise SystemExit(1)
    print("=== 用【过期 access_token】打开控制台 ===")
    print("  当前路径              :", res["href"], "（必须还是 /console.html，不能被踢到 login）")
    print("  access_token 已换新   :", res["tokenChanged"])
    print("  refresh_token 仍在    :", res["hasRefresh"])
    print("  顶部用户信息          :", res["userInfo"])
    print("  套餐徽章              :", res["topTier"])
    print("  余额                  :", res["topBalance"])
    print("  概览-当前套餐        :", res["ovPlan"])
    print("  概览-套餐权益条目    :", res["ovBenefitsLen"], "字符")
    print("  密钥表渲染长度        :", res["keyWrapLen"])
    print("  页面错误              :", res["log"] or "（无）")

    ok = (
        res["tokenChanged"]                    # 过期 access 被换新
        and "加载中" not in res["userInfo"]      # 顶栏已填好
        and res["topTier"] not in ("—", "")     # 套餐徽章已填好
        and "加载中" not in res["ovPlan"]        # 概览卡片已填好
        and res["ovBenefitsLen"] > 50          # 权益列表已渲染
        and res["keyWrapLen"] > 100            # 密钥表已渲染
        and not res["log"]
    )
    print("\n" + ("PASS  自动续期链路正常，首屏完整渲染，用户不会被踢出"
                  if ok else "FAIL  仍有问题"))
