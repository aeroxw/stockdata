"""重启本地开发服务：先把占着端口的旧实例清干净，再起新的。

为什么要这个脚本
----------------
开发期最容易踩的坑：**旧实例没杀干净，新代码根本没生效**。

Python 把模块加载进内存后就不会重读磁盘（没开 --reload 时），
所以一个很早之前起的服务会一直跑着旧代码：
  - 新加的路由（如 /billing/me）在它那儿就是 404
  - 但它共用同一个数据库，于是测试数据还会互相干扰
页面上只会显示一句 "Not Found（/api/v1/xxx）"，极难联想到是端口搞错了。

这个脚本在起服务前先按端口找到所有旧进程并杀掉，
同时把「其它常见端口上的同类实例」也一起清掉，避免留僵尸。

用法：
    python restart.py            # 默认起在 9850
    python restart.py 9851       # 指定端口
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

#: 本项目的服务习惯用这几个端口，起新服务时一并扫描清理
#: 注意：8000/8018 是 NAS 上 easy-tdx / kpl-proxy 的地址，但它们也可能
#: 被本机占用（罕见），扫到就杀，属于预期行为
COMMON_PORTS = (9850, 8000, 8899)


def listening_pids(port: int) -> set[str]:
    """返回占用该端口且处于 LISTENING 的 PID 集合。

    Windows 的 netstat 输出是 GBK，按 utf-8 解码会直接崩。
    """
    raw = subprocess.run(
        ["netstat", "-ano"], capture_output=True
    ).stdout.decode("gbk", "ignore")

    pids: set[str] = set()
    for line in raw.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        addr = parts[1]
        # 形如 127.0.0.1:8000 / [::]:8000 / 0.0.0.0:8000
        if addr.rsplit(":", 1)[-1] != str(port):
            continue
        pids.add(parts[-1])
    return pids


def kill(pids: set[str], port: int) -> None:
    for pid in sorted(pids):
        r = subprocess.run(
            ["taskkill", "/F", "/PID", pid], capture_output=True
        )
        out = (r.stdout or b"").decode("gbk", "ignore").strip()
        err = (r.stderr or b"").decode("gbk", "ignore").strip()
        print(f"  端口 {port}: 已终止 PID {pid}  {out or err}")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9850
    here = Path(__file__).resolve().parent

    print(f"=== 清理旧实例（扫描端口 {COMMON_PORTS}）===")
    for p in COMMON_PORTS:
        pids = listening_pids(p)
        # 目标端口上的必须杀（否则新服务起不来）；
        # 其它端口上的也杀，避免留下跑着旧代码的僵尸实例误导调试
        if pids:
            kill(pids, p)
        else:
            print(f"  端口 {p}: 空闲")

    # 等端口真正释放
    time.sleep(1.5)

    print(f"\n=== 在端口 {port} 启动服务 ===")
    print("（Ctrl-C 停止）\n")
    cmd = [
        sys.executable, "-u", "-m", "uvicorn",
        "app.main:app", "--host", "127.0.0.1", "--port", str(port),
    ]
    r = subprocess.run(cmd, cwd=str(here))
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
