"""用量统计页渲染探针（自托管版）。

思路：把注入了登录态的探针页写到 backend/static/ 下，通过
http://127.0.0.1:9850/__probe_usage.html 打开。
这样页面与 /api/v1 同源，fetch 不会被浏览器拦截（file:// 打开会被跨域挡掉，
表现为「资料加载失败：无法连接服务器」，图表永远是「加载中…」）。
用完即删。

背景：原图表用 viewBox="0 0 100 220" + width:100%，
100 宽的坐标系被横拉到 1000+ px，SVG 里所有 <text> 跟着被横向拉扁，
表现为「数字糊成一团」。这个探针用于量化验证修复效果。

另一个坑：不能在 --dump-dom 的输出里找 SVG —— 匹配到的往往是 JS
模板字符串。要断言真实渲染结果，得在浏览器里量好再回传。
"""

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:9850"
HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
TMP = HERE / ".probe"
TMP.mkdir(exist_ok=True)

#: 探针页放在 static 下，与 API 同源
PROBE_NAME = "__probe_usage.html"
PROBE_URL = f"{BASE}/{PROBE_NAME}"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

#: 回传标记。DOM 里源码字符串也含同名文案，解析时取最后一个（rfind）
MARK_A = "__PROBE_RESULT_START__"
MARK_B = "__PROBE_RESULT_END__"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def edge() -> str:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    sys.exit("找不到 msedge.exe")


def req(path, method="GET", body=None):
    """带重试的请求。

    本机偶发 WinError 10054（连接被对端重置）。探针里全是**只读**请求
    （登录 / 拉用量数据），重试完全无害 —— 不重试的话一次抖动就让整个
    探针崩掉，白等一轮浏览器启动。
    """
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for attempt in range(5):
        r = urllib.request.Request(BASE + path, data=data, method=method)
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            return json.loads(_opener.open(r, timeout=30).read().decode())
        except urllib.error.HTTPError:
            raise
        except OSError as e:
            last = e
            time.sleep(0.6 * (attempt + 1))
    raise last  # type: ignore


PROBE_JS = r"""
window.__probe = function () {
  try {
    var svg = document.querySelector('#usChart svg');
    if (!svg) {
      var chartBox = document.getElementById('usChart');
      return { step: 'svg', error: '没有 svg 元素',
               html: chartBox ? chartBox.textContent.slice(0, 200) : '(无 usChart)' };
    }
    // 取 viewBox。别用 getAttribute('viewBox') —— 某些序列化路径会把
    // 属性名小写化；用 svg.viewBox.baseVal 走 IDL 最稳。
    var vbRaw = '';
    var vb = [0, 0, 0, 0];
    try {
      var bv = svg.viewBox && svg.viewBox.baseVal;
      if (bv && bv.width) {
        vb = [bv.x, bv.y, bv.width, bv.height];
      }
    } catch (e) { /* 落到下面的文本解析 */ }
    if (!vb[2]) {
      vbRaw = svg.getAttribute('viewBox') || svg.getAttribute('viewbox') || '';
      var nums = vbRaw.trim().split(/[\s,]+/).filter(function (s) { return s !== ''; });
      if (nums.length >= 4) {
        vb = nums.map(function (s) { return parseFloat(s) || 0; });
      }
    }
    var box = svg.getBoundingClientRect();
    if (!vb[2] || !box.width) {
      return { step: 'svg', error: 'viewBox 读取失败: ' + JSON.stringify(vbRaw)
                 + ' idl=' + JSON.stringify(vb) };
    }
    var texts = Array.prototype.slice.call(svg.querySelectorAll('text'));
    var vals = [], xlabels = [];
    texts.forEach(function (t) {
      if (t.getAttribute('font-weight') === '600') vals.push(t.textContent);
      var s = t.textContent;
      if (/^\d\d-\d\d$/.test(s)) xlabels.push(s);
    });
    var bars = 0;
    svg.querySelectorAll('rect').forEach(function (r) {
      if ((r.getAttribute('fill') || '') !== 'transparent') bars++;
    });
    // 变形比：viewBox 单位 / 实际显示像素。理想等比时两轴相等。
    // viewBox = "minX minY width height" —— 宽高在 [2]/[3]，不是 [0]/[1]。
    var vw = vb[2] || 0, vh = vb[3] || 0;
    var sx = box.width ? vw / box.width : 0;
    var sy = box.height ? vh / box.height : 0;
    var distort = (sx && sy) ? +(sx / sy).toFixed(3) : null;
    return {
      step: 'ok',
      viewBox: vb,
      displayW: Math.round(box.width),
      displayH: Math.round(box.height),
      scaleX: +sx.toFixed(4),
      scaleY: +sy.toFixed(4),
      distort: distort,   // 1.0 = 等比，无变形
      bars: bars,
      textCount: texts.length,
      barValues: vals.slice(0, 16),
      xLabels: xlabels,
      legend: Array.prototype.map.call(
        document.querySelectorAll('#usChart .chart-legend span'),
        function (s) { return s.textContent.trim(); }
      ),
      topPaths: Array.prototype.map.call(
        document.querySelectorAll('#usTop .bar-row'),
        function (r) {
          var el = function (c) { var x = r.querySelector(c); return x ? x.textContent.trim() : null; };
          return { name: el('.bar-name'), pct: el('.bar-pct'), val: el('.bar-val') };
        }
      ),
      errorRateSub: (document.getElementById('usErrSub') || {}).textContent,
      todayCalls: (document.getElementById('us24') || {}).textContent,
      statCards: Array.prototype.map.call(
        document.querySelectorAll('#view-usage .stat-card'),
        function (c) {
          return {
            label: c.querySelector('.stat-label').textContent.trim(),
            value: c.querySelector('.stat-value').textContent.trim(),
            sub: c.querySelector('.stat-sub') ? c.querySelector('.stat-sub').textContent.trim() : ''
          };
        }
      )
    };
  } catch (e) {
    return { step: 'err', error: String(e && e.message || e) };
  }
};
"""


def build_page() -> Path:
    tok = req("/api/v1/auth/login", "POST",
              {"email": "admin@example.com", "password": "***REMOVED***"})["data"]

    html = _opener.open(BASE + "/console.html", timeout=20).read().decode()
    # 探针页在根路径，页面内 /static/ 本来就是对的，无需改写

    acc, ref = json.dumps(tok["access_token"]), json.dumps(tok["refresh_token"])

    inject = """<script>
localStorage.setItem('sd_token', %s);
localStorage.setItem('sd_refresh', %s);
%s
window.addEventListener('load', function () {
  setTimeout(function () {
    try { go('usage'); } catch (e) {
      document.title = 'GOTO_ERR:' + (e && e.message);
    }
    setTimeout(function () {
      var r = window.__probe();
      var pre = document.createElement('pre');
      pre.id = 'probeOut';
      pre.textContent = '%s' + JSON.stringify(r) + '%s';
      document.body.appendChild(pre);
      document.title = 'PROBE_DONE';
    }, 4000);
  }, 1200);
});
</script>""" % (acc, ref, PROBE_JS, MARK_A, MARK_B)

    pos = html.find('<script src=')
    if pos < 0:
        sys.exit("注入失败：没找到 <script src=")
    patched = html[:pos] + inject + "\n" + html[pos:]

    target = STATIC / PROBE_NAME
    target.write_text(patched, encoding="utf-8")
    return target


def main() -> int:
    target = build_page()
    shot = TMP / "usage_chart.png"

    base_cmd = [
        edge(), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--force-device-scale-factor=1",
        "--window-size=1440,1300",
        "--virtual-time-budget=16000",
    ]

    try:
        r = subprocess.run(base_cmd + [f"--screenshot={shot}", PROBE_URL],
                           capture_output=True, timeout=150)
        if not shot.exists():
            print(r.stderr.decode("utf-8", "ignore")[-1500:])
            sys.exit("截图未生成")

        r2 = subprocess.run(base_cmd + ["--dump-dom", PROBE_URL],
                            capture_output=True, timeout=150)
        dom = r2.stdout.decode("utf-8", "ignore")
        dom = (dom.replace("&lt;", "<").replace("&gt;", ">")
                  .replace("&quot;", '"').replace("&amp;", "&"))
        i, j = dom.rfind(MARK_A), dom.rfind(MARK_B)
        if i < 0 or j < 0:
            print(dom[-3000:])
            sys.exit("没拿到探针回传数据")
        data = json.loads(dom[i + len(MARK_A):j])
    finally:
        # 探针页不留在服务器上
        target.unlink(missing_ok=True)

    if data.get("step") != "ok":
        print("渲染失败:", json.dumps(data, ensure_ascii=False))
        print("结果: FAIL")
        return 1

    print("=== 真实渲染测量 ===")
    print(f"  显示尺寸 {data['displayW']}×{data['displayH']}  "
          f"viewBox {data['viewBox']}")
    print(f"  scaleX={data['scaleX']}  scaleY={data['scaleY']}  "
          f"变形比={data['distort']}（1.0 = 等比无变形）")

    ok = True
    d = data.get("distort")
    # 注意：viewBox = "minX minY width height"，宽度在**索引 2**，
    # 索引 0 是左上角 x 偏移（通常为 0）。之前拿 [0] 当宽度，才误判成 0。
    vb_w = data["viewBox"][2] if len(data["viewBox"]) > 2 else 0
    if vb_w < 600:
        print(f"  FAIL viewBox 宽 {vb_w} 太窄，文字会被横向拉扁")
        ok = False
    if d is None:
        print("  FAIL 变形比算不出来（viewBox 或显示尺寸为 0）")
        ok = False
    elif not (0.85 <= d <= 1.18):
        print(f"  FAIL 横向变形比 {d}，文字被拉伸/压缩")
        ok = False
    else:
        print(f"  OK 等比渲染（{d}），文字不会被拉伸")

    print(f"\n  柱体 {data['bars']} 根 | SVG 文字 {data['textCount']} 个")
    print(f"  柱顶数值: {data['barValues'] or '（全为 0，不显示标签——正常）'}")
    print(f"  X 轴日期: {data['xLabels']}")
    print(f"  图例: {data['legend']}")
    print(f"\n  卡片:")
    for c in data["statCards"]:
        print(f"    {c['label']:<8} {c['value']:<10} {c['sub']}")
    print("  接口排行:")
    for p in data["topPaths"]:
        print(f"    {p['name']:<18} {p['pct']:>6}  {p['val']:>6}")

    if data["topPaths"] and not data["topPaths"][0]["pct"]:
        print("  FAIL 接口排行缺少占比列")
        ok = False
    if not data["xLabels"]:
        print("  FAIL X 轴没有任何日期标签")
        ok = False

    print(f"\n截图: {shot}")
    print("结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
