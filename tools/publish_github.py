#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 StockData 推送到 GitHub（幂等，可重复跑）。

用法（Token 从环境变量读，绝不写进文件 / 不进命令行历史）:

    set GITHUB_TOKEN=<TOKEN>     # Windows cmd
    export GITHUB_TOKEN=<TOKEN>  # Linux / macOS
    python -u tools/publish_github.py

可选参数：
    --repo  仓库名，默认 stockdata
    --owner GitHub 用户名，默认 aeroxw
    --dry   只检查不推送（看仓库是否存在、分支、待推文件）

干什么：
  1. 查同名仓库是否已存在（存在就直接复用，不会重复创建）
  2. 不存在则用 API 创建公开仓库，**不勾 auto_init / 不生成 LICENSE** ——
     我们本地已有 LICENSE 和历史，自动生成会和本地冲突导致 push 被拒
  3. 配 remote（用 https + token 形式，推完抹掉 token 只留干净地址）
  4. 本地分支 master -> main（GitHub 现在默认 main，推 master 会多出一个分支）
  5. push 并输出仓库地址

注意：
- **Token 用完请去 GitHub 吊销**，推送权限是最高级别的一次性凭据。
- 推荐的 token 类型：Settings -> Developer settings -> Personal access tokens
  -> Tokens (classic) -> Generate new token (classic)，勾选 repo + workflow。
- 推完之后 remote 里就不带 token 了，日常 push 走系统凭据管理器。
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"


def git(*args, check=True, capture=True):
    """跑 git 命令。cwd 固定为仓库根目录。"""
    cmd = ["git", *args]
    res = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=capture, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and res.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args), res.stderr.strip()))
    return res.stdout.strip() if capture else ""


def api(method, path, token, payload=None):
    """调 GitHub REST API。返回 (状态码, 解析后的 json)。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "stockdata-publish-script",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return e.code, {"message": body[:400]}


def main():
    argv = sys.argv[1:]
    owner = "aeroxw"
    repo = "stockdata"
    dry = "--dry" in argv

    for flag, key in (("--repo", "repo"), ("--owner", "owner")):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                if key == "repo":
                    repo = argv[i + 1]
                else:
                    owner = argv[i + 1]

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("[错误] 没有读到 GITHUB_TOKEN 环境变量。")
        print("       先执行：set GITHUB_TOKEN=ghp_你的token   （Windows cmd）")
        print("               export GITHUB_TOKEN=ghp_你的token （Linux / macOS）")
        return 2

    full = "%s/%s" % (owner, repo)
    print("=" * 60)
    print("目标仓库： https://github.com/%s" % full)
    print("=" * 60)

    # ---- 1. 确认 token 有效、并能读到 owner ----
    code, me = api("GET", "/user", token)
    if code != 200:
        print("[错误] Token 无效或没权限读 /user：HTTP %s %s" % (code, me.get("message")))
        return 1
    login = me.get("login")
    print("[OK] Token 有效，登录身份 = %s" % login)
    if login.lower() != owner.lower():
        print("[警告] Token 身份(%s) 与期望 owner(%s) 不一致，会推到 %s 名下。"
              % (login, owner, login))

    # ---- 2. 仓库是否存在 ----
    code, info = api("GET", "/repos/%s" % full, token)
    if code == 200:
        print("[OK] 仓库已存在：%s  （直接复用，不会重复创建）" % info.get("html_url"))
    elif code == 404:
        if dry:
            print("[DRY] 仓库不存在，正式运行会创建。")
            return 0
        code, info = api("POST", "/user/repos", token, {
            "name": repo,
            "description": "多源 A 股数据整合平台 —— 六家数据源统一适配层，"
                           "自动降级与熔断，Apache-2.0 自部署。",
            "private": False,
            "auto_init": False,          # 关键：不要生成 README/LICENSE，否则和本地历史冲突
            "has_issues": True,
            "has_projects": False,
            "has_wiki": False,
            "is_template": False,
        })
        if code not in (200, 201):
            print("[错误] 创建仓库失败：HTTP %s %s" % (code, info.get("message")))
            if code == 403 and token.startswith("github_pat_"):
                # 2026-09-29 实测：fine-grained token 再怎么勾权限也没有「创建仓库」能力，
                # 这是 GitHub 的设计限制，不是用户勾漏了。第一次撞上时排查了半天。
                print()
                print("       你用的是 fine-grained token（github_pat_ 开头）。这类 token")
                print("       没有「创建仓库」权限 —— 是 GitHub 的设计限制，不是勾漏了。")
                print("       两条路：")
                print("       1) 去 https://github.com/new 手动建同名仓库 stockdata")
                print("          （README / .gitignore / LICENSE 一个都别勾），建好再跑一次")
                print("       2) 重生成一个 classic token（ghp_ 开头，勾 repo + workflow）")
            return 1
        print("[OK] 已创建公开仓库：%s" % info.get("html_url"))
    else:
        print("[错误] 查询仓库失败：HTTP %s %s" % (code, info.get("message")))
        return 1

    if dry:
        print("[DRY] 到这里为止，未做任何推送。")
        return 0

    # ---- 3. 本地分支 master -> main ----
    cur = git("branch", "--show-current")
    print("[信息] 当前本地分支：%s，提交数：%s" % (cur, git("rev-list", "--count", "HEAD")))
    if cur != "main":
        git("branch", "-M", "main")
        print("[OK] 已把本地分支重命名为 main")

    # ---- 4. 配 remote ----
    remotes = git("remote").splitlines()
    clean_url = "https://github.com/%s.git" % full
    push_url = "https://%s@github.com/%s.git" % (token, full)   # 临时带 token，用完抹掉

    if "origin" in remotes:
        git("remote", "set-url", "origin", push_url)
        print("[OK] 已更新现有 origin")
    else:
        git("remote", "add", "origin", push_url)
        print("[OK] 已添加 origin")

    # ---- 5. 推送 ----
    print("[..] 正在推送 %s 个文件，稍候…" % len(git("ls-files").splitlines()))
    try:
        # 不 capture —— 让 git 的进度条直接打到终端；-u 已经在跑法则用了 -u
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(ROOT), check=True)
    except subprocess.CalledProcessError as e:
        print("[错误] push 失败（exit %s）。常见原因：" % e.returncode)
        print("       - Token 没勾 repo 权限 → 重生成时务必勾上")
        print("       - GitHub Push Protection 拦了疑似密钥 → 看上面的报错行号")
        return 1

    # ---- 6. 抹掉 remote 里的 token ----
    git("remote", "set-url", "origin", clean_url)
    print("[OK] 已把 remote 里的 token 抹掉（现在只剩干净地址）")

    print()
    print("=" * 60)
    print("推送完成： https://github.com/%s" % full)
    print("=" * 60)
    print("下一步建议：")
    print("  1. 去 https://github.com/settings/tokens 吊销刚才那个 token")
    print("  2. 打开仓库页面核对 README / LICENSE 是否正常渲染")
    print("  3. Settings -> General 里确认 Default branch 是 main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
