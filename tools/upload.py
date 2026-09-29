"""把 StockData 项目文件上传到绿联 NAS。

用法：python -u tools/upload.py

要点：
  - SFTP 被禁 → 用 exec_command("cat > file") + 写 stdin（见 nas.py）
  - .py / Dockerfile / nginx.conf 必须转成 LF，否则 Linux 里直接崩
  - 不传 backend/.env（容器内配置走 compose 的 environment:）
  - 不传 backend/stockdata.db（本地 SQLite，NAS 上用 PG）
  - 不传 __pycache__ / .probe / 测试脚本（传上去也没用，还会让镜像变脏）
"""
import hashlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from nas import put, run, sudo  # noqa: E402


def md5(path: pathlib.Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()

#: 本文件在 <项目根>/tools/ 下，往上两级才是项目根
ROOT = pathlib.Path(__file__).resolve().parent.parent
REMOTE = "/volume1/docker/stockdata"

#: 需要转成 LF 的文本后缀（Windows 下 CRLF 会让 shell 脚本和 Dockerfile 崩）
TEXT_EXT = {".py", ".txt", ".conf", ".yaml", ".yml", ".md", ".sh", ".json"}
#: 二进制原样传
BIN_EXT = {".html", ".css", ".js", ".png", ".ico", ".svg", ".woff", ".woff2"}
#: 明确排除
EXCLUDE_DIRS = {"__pycache__", ".probe", ".git", ".workbuddy", "docs", "data"}
EXCLUDE_FILES = {".env", "stockdata.db", "stockdata.db-journal"}

#: Docker 构建上下文：backend/ 下只需要这些
BACKEND_FILES = ["Dockerfile", "requirements.txt"]


def skip(p: pathlib.Path) -> bool:
    """是否跳过该文件。

    注意：排除逻辑**必须对每个遍历分支都生效**。之前只在 collector 分支写了
    判断，backend/app 分支漏了，结果 60 个文件里混进 22 个 .pyc，
    被 Dockerfile 一起 COPY 进镜像，白占空间还容易被 Python 吃到旧字节码。
    """
    if p.name in EXCLUDE_FILES:
        return True
    return any(x in p.parts for x in EXCLUDE_DIRS)


def main():
    stats = {"ok": 0, "fail": 0}
    #: 记录 (本地真实文件, 远端路径)，最后统一 md5 校验。
    #: 这一层校验不是多余：远端文件一旦被设成只读（历史上就被 chmod 420 坑过），
    #: `cat > file` 会静默失败，而 put 只返回 rc —— 结果是线上一直跑旧代码，
    #: 排查起来极其费时间。上传完必须验证内容一致。
    manifest: list[tuple[pathlib.Path, str]] = []

    def up(local: pathlib.Path, remote: str, lf: bool = True):
        if not local.exists():
            print(f"  跳过（不存在）{local}")
            return
        if lf and local.suffix in TEXT_EXT:
            data = local.read_bytes().replace(b"\r\n", b"\n")
            tmp = local.with_suffix(local.suffix + ".lf.tmp")
            tmp.write_bytes(data)
            ok = put(str(tmp), remote)
            tmp.unlink()
            #: 校验要比对**转换后**的内容，所以记临时文件的 md5
            expect = hashlib.md5(data).hexdigest()
        else:
            ok = put(str(local), remote)
            expect = md5(local)
        if ok:
            stats["ok"] += 1
            manifest.append((remote, expect))
        else:
            stats["fail"] += 1
            print(f"  !! 失败 {local} -> {remote}")

    print("=== 1. 顶层 compose ===")
    up(ROOT / "docker-compose.yaml", f"{REMOTE}/docker-compose.yaml")

    print("=== 2. nginx ===")
    up(ROOT / "nginx" / "nginx.conf", f"{REMOTE}/nginx/nginx.conf")

    print("=== 3. backend 构建文件 ===")
    for n in BACKEND_FILES:
        up(ROOT / "backend" / n, f"{REMOTE}/backend/{n}")

    print("=== 4. backend/app ===")
    app = ROOT / "backend" / "app"
    for p in sorted(app.rglob("*")):
        if not p.is_file() or skip(p):
            continue
        rel = p.relative_to(app).as_posix()
        up(p, f"{REMOTE}/backend/app/{rel}")

    print("=== 5. backend/static ===")
    st = ROOT / "backend" / "static"
    for p in sorted(st.rglob("*")):
        if not p.is_file() or skip(p):
            continue
        rel = p.relative_to(st).as_posix()
        up(p, f"{REMOTE}/backend/static/{rel}", lf=False)

    print("=== 6. collector ===")
    col = ROOT / "collector"
    for p in sorted(col.rglob("*")):
        if not p.is_file() or skip(p):
            continue
        rel = p.relative_to(col).as_posix()
        up(p, f"{REMOTE}/collector/{rel}")

    print(f"\n上传完成：成功 {stats['ok']}，失败 {stats['fail']}")

    verify(manifest)


def verify(manifest: list[tuple[str, str]]) -> None:
    """逐个比对远端文件 md5，列出不一致的。

    远端一次 md5sum 太多文件会超出命令行长度，这里按 60 个一批。
    """
    if not manifest:
        return
    print(f"\n=== 校验远端内容（{len(manifest)} 个文件）===")
    bad: list[str] = []

    for i in range(0, len(manifest), 60):
        batch = manifest[i:i + 60]
        #: 只传相对路径，远端 cd 过去再算，避免绝对路径里出现空格等麻烦
        rels = [r.replace(REMOTE + "/", "") for r, _ in batch]
        cmd = f"cd {REMOTE} && md5sum " + " ".join(f"'{r}'" for r in rels)
        out = sudo(cmd, timeout=180, show=False)[1]
        got = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                got[parts[-1].lstrip("./")] = parts[0]

        for rel, expect in batch:
            actual = got.get(rel.replace(REMOTE + "/", ""))
            if actual is None:
                bad.append(f"{rel}（远端不存在）")
            elif actual != expect:
                bad.append(f"{rel}（内容不一致 本地{expect[:8]} 远端{actual[:8]}）")

    if bad:
        print(f"  !! {len(bad)} 个文件未正确上传：")
        for b in bad:
            print(f"     - {b}")
        print("  常见原因：远端文件被设为只读，或目录不可写。")
    else:
        print(f"  全部 {len(manifest)} 个文件校验通过 ✓")


if __name__ == "__main__":
    main()
