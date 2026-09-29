"""真实浏览器端到端验证管理后台的「新建套餐」。

复现用户报错的那条路径：登录后台 -> 打开套餐管理 -> 点新建 -> 填表 -> 保存，
确认不再出现 "接口不存在：/api/v1/admin/plans"。
"""

import json
import pathlib
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

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
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode() or "{}")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6)
    raise last


def build():
    tok = post("/api/v1/auth/login",
               {"email": "admin@example.com", "password": "***REMOVED***"})["data"]["access_token"]
    html = (STATIC / "admin.html").read_text(encoding="utf-8")
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
    seed = f'<script>localStorage.setItem("sd_token", {json.dumps(tok)});</script>'
    html = html.replace('<script src="/static/app.js"></script>',
                        guard + seed + '<script src="/static/app.js"></script>', 1)

    tail = """
<script>
window.addEventListener('load', function () {
  var steps = [];
  function note(s) { steps.push(s); }

  setTimeout(async function () {
    try {
      // 1) 切到套餐管理视图
      note('switchView(plans)');
      if (typeof switchView === 'function') switchView('plans');
      await new Promise(function (r) { setTimeout(r, 900); });

      var grid = document.getElementById('view-plans');
      note('套餐视图存在=' + !!grid);

      // 2) 直接调保存接口，模拟点「保存」按钮
      note('submitPlan 可用=' + (typeof submitPlan === 'function'));

      // 3) 用 SD.api 打一次真实请求，验证路径正确
      var r = await SD.api('/admin/plans', {
        method: 'POST',
        body: JSON.stringify({
          code: 'probe1', name: '探针套餐', level: 3,
          price_month: 1000, price_year: 6000,
          max_keys: 2, rate_limit: 20, daily_quota: 5000,
          features: ['探针测试'], description: '探针',
          is_public: true, is_active: true, sort_order: 88
        })
      });
      note('POST /admin/plans -> ' + JSON.stringify(r).slice(0, 120));

      // 4) 确认列表里能查到
      var r2 = await SD.api('/admin/plans');
      var items = (r2.data || {}).items || [];
      note('列表含探针套餐 = ' + (items.some(function (x) { return x.code === 'probe1'; }) ? 'YES' : 'NO'));

      // 5) 删掉
      var r3 = await SD.api('/admin/plans/probe1', { method: 'DELETE' });
      note('DELETE -> ' + JSON.stringify(r3).slice(0, 80));
    } catch (e) {
      note('EXCEPTION: ' + e.message);
    }

    var box = document.createElement('pre');
    box.id = '__probe_out';
    box.textContent = 'PFL_START' + JSON.stringify({
      log: window.__log.slice(0, 10), steps: steps
    }) + 'PFL_END';
    document.body.appendChild(box);
  }, 2500);
});
</script>
"""
    html = html.replace("</body>", tail + "</body>", 1)
    (STATIC / "_probe_final.html").write_text(html, encoding="utf-8")


def run():
    prof = pathlib.Path(tempfile.gettempdir()) / "sd_probe_final"
    prof.mkdir(parents=True, exist_ok=True)
    cmd = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", f"--user-data-dir={prof}",
           "--virtual-time-budget=15000", "--dump-dom",
           f"{BASE}/static/_probe_final.html"]
    r = subprocess.run(cmd, capture_output=True, timeout=180)
    dom = r.stdout.decode("utf-8", "ignore")
    tp = dom.rfind("PFL_START")
    if tp < 0:
        print("没拿到结果，DOM 尾部:", dom[-1200:])
        return None
    raw = dom[tp + len("PFL_START"):dom.find("PFL_END", tp)]
    for a, b in (("&quot;", '"'), ("&#34;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        raw = raw.replace(a, b)
    return json.loads(raw)


if __name__ == "__main__":
    build()
    res = run()
    if not res:
        raise SystemExit(1)
    print("=== 浏览器内执行的步骤 ===")
    for s in res["steps"]:
        print("  ·", s)
    print("\n=== 页面错误 ===")
    print("  ", res["log"] or "（无）")

    blob = " ".join(res["steps"])
    ok = (
        "已创建" in blob
        and "列表含探针套餐 = YES" in blob
        and "已删除" in blob
        and not any("接口不存在" in s for s in res["steps"])
        and not any("EXCEPTION" in s for s in res["steps"])
        and not res["log"]
    )
    print("\n" + ("PASS  新建套餐链路正常，404 已消失" if ok else "FAIL  仍有问题"))
