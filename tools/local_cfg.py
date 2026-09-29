#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本机部署配置的读取口 —— 仓库里不放任何真实值。

为什么要这层：
    以前 NAS 的 IP、SSH 密码、管理员账号是直接硬编码在各个 tools/*.py
    和 backend/test_*.py 里的。2026-09-29 首次推公开仓库时把它们一起捅了出去
    （其中就包括能 ssh 进 NAS 的明文口令）。所以现在统一改成：

        环境变量 > 项目根 .env > 占位字符串

    `.env` 已被 .gitignore 排除，真实值只在本地，不上网。

用法（tools/ 下的脚本）：

    from local_cfg import cfg
    NAS_HOST = cfg("NAS_HOST")

用法（backend/ 下的脚本，需要先补路径）：

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from local_cfg import cfg

    ADMIN = (cfg("STOCKDATA_ADMIN_EMAIL"), cfg("STOCKDATA_ADMIN_PASSWORD"))

本机 .env 里需要的键（按自己的实际环境填）：

    NAS_HOST=192.168.x.x
    NAS_USER=your_user
    NAS_SSH_PASSWORD=your_password
    STOCKDATA_BASE=http://192.168.x.x:9850
    STOCKDATA_ADMIN_EMAIL=you@example.com
    STOCKDATA_ADMIN_PASSWORD=your_password

忘了配会拿到空串，脚本会以「连不上 / 401」的形式失败 —— 这是故意的，
宁可直接报错，也不给一个看起来能跑的默认值。
"""

import os
from pathlib import Path

# 项目根（tools/ 的上一级）
ROOT = Path(__file__).resolve().parent.parent

_CACHE = None


def _load_env_file():
    """解析项目根 .env。文件不存在就返回空字典 —— 不报错，交给调用方兜底。"""
    p = ROOT / ".env"
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        # 去掉成对引号
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def cfg(key, default=""):
    """取配置项。优先级：环境变量 > .env > default。"""
    global _CACHE
    v = os.getenv(key)
    if v:
        return v
    if _CACHE is None:
        _CACHE = _load_env_file()
    return _CACHE.get(key, default)


def require(key, tip=""):
    """取必填项，缺了直接把话说清楚再退出 —— 别让脚本带着空串跑到一半挂。"""
    v = cfg(key)
    if not v:
        raise SystemExit(
            f"[配置缺失] {key} 没取到值。\n"
            f"  设置环境变量，或写进项目根的 .env： {key}=你的值\n"
            + (f"  提示：{tip}\n" if tip else "")
        )
    return v


# 常用的组合，省得每个脚本都拼一遍
def base_url():
    """站台根地址，如 http://192.168.x.x:9850。"""
    return require("STOCKDATA_BASE", "例如 http://192.168.1.10:9850").rstrip("/")


def api_base():
    return base_url() + "/api/v1"


def admin_account():
    """(邮箱, 密码)"""
    return (require("STOCKDATA_ADMIN_EMAIL"), require("STOCKDATA_ADMIN_PASSWORD"))


if __name__ == "__main__":
    for k in ("NAS_HOST", "NAS_USER", "NAS_SSH_PASSWORD",
              "STOCKDATA_BASE", "STOCKDATA_ADMIN_EMAIL", "STOCKDATA_ADMIN_PASSWORD"):
        v = cfg(k)
        print(f"{k:<28} {'（已配置，长度 ' + str(len(v)) + '）' if v else '（未配置）'}")
