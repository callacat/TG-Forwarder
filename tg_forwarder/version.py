# -*- coding: utf-8 -*-
"""版本号单一事实源。

解析优先级（高 → 低）：

1. 环境变量 ``TG_FORWARDER_VERSION``：镜像构建期由 Dockerfile ARG → ENV 注入
   （CI 用 ``git describe --tags --always`` 取值），生产路径走这条；
2. ``git describe --tags --always --dirty``：源码直接运行时兜底（容器内无 git
   命令，自动跳过）；
3. ``_FALLBACK_VERSION``：既无注入又无 git（sdist/裁剪环境）时的诚实占位。

对外一律去掉 ``v`` 前缀（``v3.0.0-rc.6`` → ``3.0.0-rc.6``）；前端展示、
``/api/version`` 与 FastAPI ``version=`` 全部取此处，禁止任何地方再硬编码第二份。
"""
import os
import subprocess

ENV_KEY = "TG_FORWARDER_VERSION"

# 未注入且无 git 时的占位：写成 0.0.0-dev 而非某个具体版本号，避免「看起来像
# 真版本」的假信息（发版镜像一定带 env，见 Dockerfile 与 CI build-args）。
_FALLBACK_VERSION = "0.0.0-dev"


def _from_git() -> str:
    """git describe 兜底；任何失败（无 git/超时/非仓库）返回空串，不抛。"""
    try:
        proc = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:  # noqa: BLE001 —— 环境不可用一律视为拿不到，回落占位
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def resolve_version() -> str:
    """归一化版本串（去 v 前缀）。调用方在启动期取一次即可（见 server.APP_VERSION）。"""
    raw = os.environ.get(ENV_KEY, "").strip() or _from_git() or _FALLBACK_VERSION
    return raw[1:] if raw.startswith("v") else raw
