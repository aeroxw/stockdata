"""在 NAS 上构建并启动 StockData。

分两步走，避免"构建慢导致整条命令超时、看不到结果"：
  step1: 只起 postgres / redis（秒级），先确认数据层 OK
  step2: 构建并起 api / collector / nginx（慢，几分钟到十几分钟）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nas import sudo  # noqa: E402

D = "/volume1/docker/stockdata"
STEP = sys.argv[1] if len(sys.argv) > 1 else "all"


def sh(cmd, timeout=1800):
    return sudo(f"cd {D} && {cmd}", timeout=timeout)


if STEP in ("1", "all"):
    print("=" * 60)
    print("STEP 1: 起数据层（postgres / redis）")
    print("=" * 60)
    sh("docker compose up -d postgres redis", timeout=600)
    sh("sleep 8; docker compose ps")

    print("\n--- PG 就绪检查 ---")
    sh("docker compose exec -T postgres pg_isready -U stockdata -d stockdata || true", timeout=120)

if STEP in ("2", "all"):
    print("=" * 60)
    print("STEP 2: 构建并起 api / collector / nginx（较慢，请耐心）")
    print("=" * 60)
    sh("docker compose up -d --build api collector nginx", timeout=1800)

    #: **必须重启 nginx**。nginx 会缓存 upstream 的 DNS 解析结果，
    #: api 容器 recreate 后 IP 变了，nginx 还指着旧 IP → 全部 502。
    #: 坑点在于这时的表现极具迷惑性：容器全 Up、后端日志完全正常，
    #: 就是访问 502，很容易误判成后端崩了。
    print("\n--- 重启 nginx（刷新 upstream DNS）---")
    sudo("docker restart stockdata-nginx", timeout=120)
    sh("sleep 5", timeout=60)

print("\n--- 最终状态 ---")
sh("docker compose ps", timeout=120)
print("\n--- 健康检查 ---")
sudo("curl -s -m 8 -o /dev/null -w 'health=%{http_code}\\n' http://127.0.0.1:9850/health || true",
     timeout=60)
