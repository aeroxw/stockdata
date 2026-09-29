"""提取 HTML 内联 <script> 用 node --check 验语法。

前端改完最怕的就是一个括号没闭合导致整页 JS 全挂，
而这在浏览器里表现为"页面空白/按钮没反应"，很难定位。
"""

import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
NODE = r"C:\Users\ixspa\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"

FILES = ["backend/static/console.html", "backend/static/admin.html",
         "backend/static/index.html", "backend/static/login.html"]


def main() -> int:
    bad = 0
    for rel in FILES:
        p = ROOT / rel
        if not p.exists():
            print(f"  跳过（不存在）{rel}")
            continue
        src = p.read_text(encoding="utf-8")
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                            src, re.S)
        if not blocks:
            print(f"  {rel}: 无内联脚本")
            continue
        for i, code in enumerate(blocks):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as f:
                f.write(code)
                tmp = f.name
            r = subprocess.run([NODE, "--check", tmp],
                               capture_output=True, text=True)
            if r.returncode == 0:
                print(f"  [OK]   {rel} script#{i + 1}")
            else:
                bad += 1
                print(f"  [FAIL] {rel} script#{i + 1}")
                print((r.stderr or "")[:600])
            pathlib.Path(tmp).unlink(missing_ok=True)
    print(f"\n语法问题 {bad} 处")
    return 1 if bad else 0


sys.exit(main())
