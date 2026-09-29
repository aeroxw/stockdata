"""NAS 操作小工具：SSH 执行命令 / 上传文件。

绿联 NAS 的两个坑（踩过）：
  1. SFTP 子系统被禁（sftp.get 报 Operation unsupported）
     → 上传用 exec_command("cat > file") + 写 stdin + shutdown_write()
  2. docker 需要 sudo 提权（aeroxw 直跑 permission denied）
     → echo '<pw>' | sudo -S -p '' docker ...
     → 注意 sudo -S 只作用于紧随的第一条命令，用 && 串联时每条都要再加 sudo
"""
import sys

import paramiko

from local_cfg import require

# NAS 地址与口令**不写死这里** —— 2026-09-29 之前是硬编码的，
# 首次推公开仓库时连同 ssh 口令一起泄露了。现在走环境变量 / 项目根 .env。
# 本机 .env 里配上这三行就能照常用：
#     NAS_HOST=192.168.x.x
#     NAS_USER=你的用户名
#     NAS_SSH_PASSWORD=你的SSH口令
HOST = require("NAS_HOST", "NAS 的内网 IP，例如 192.168.1.10")
USER = require("NAS_USER", "NAS 的 SSH 登录用户名，例如 admin")
PWD = require("NAS_SSH_PASSWORD", "NAS 的 SSH 登录口令")


def conn():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PWD, timeout=20)
    return c


def run(cmd, timeout=120, show=True):
    """执行远程命令，返回 (rc, out, err)。"""
    c = conn()
    try:
        _, so, se = c.exec_command(cmd, timeout=timeout)
        out = so.read().decode("utf-8", "replace")
        err = se.read().decode("utf-8", "replace")
        rc = so.channel.recv_exit_status()
        if show:
            print(f"$ {cmd}")
            if out.strip():
                print(out.rstrip())
            if err.strip():
                print("[stderr]", err.rstrip())
            print(f"[rc={rc}]")
        return rc, out, err
    finally:
        c.close()


def sudo(cmd, timeout=300, show=True):
    """带 sudo 提权执行。整条命令都包在 sudo sh -c 里，避免 && 后丢权限。"""
    inner = cmd.replace("'", "'\"'\"'")
    return run(f"echo '{PWD}' | sudo -S -p '' sh -c '{inner}'", timeout=timeout, show=show)


def put(local_path, remote_path, mode=None):
    """上传文件（绕过被禁的 SFTP）。"""
    c = conn()
    try:
        sftp = None
        data = open(local_path, "rb").read()
        # 先确保目录存在
        parent = remote_path.rsplit("/", 1)[0]
        c.exec_command(f"mkdir -p {parent}", timeout=20)
        _, so, se = c.exec_command(f"cat > {remote_path}", timeout=120)
        so.channel.sendall(data)
        so.channel.shutdown_write()
        rc = so.channel.recv_exit_status()
        if rc != 0:
            err = se.read().decode("utf-8", "replace")
            print(f"  上传失败 {remote_path}: rc={rc} {err}")
            return False
        if mode:
            #: mode 传进来是 Python int（例如 0o644）。直接 f"{mode}" 会得到
            #: **十进制**字符串 "420"，而 chmod 把参数当**八进制**解析，
            #: 于是 chmod 420 = r---w---- —— 属主只有读权限，后续再想覆盖
            #: 这个文件就会 Permission denied，且症状很隐蔽（上传脚本只报
            #: 一句 rc=1，很容易被忽略，结果线上跑的其实一直是旧代码）。
            #: 必须按八进制格式化。
            m = f"{mode:o}" if isinstance(mode, int) else str(mode)
            c.exec_command(f"chmod {m} {remote_path}", timeout=20)
        return True
    finally:
        c.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        run(" ".join(sys.argv[2:]))
    elif len(sys.argv) > 1 and sys.argv[1] == "sudo":
        sudo(" ".join(sys.argv[2:]))
    else:
        print(__doc__)
