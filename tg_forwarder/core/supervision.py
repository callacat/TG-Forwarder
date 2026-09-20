# -*- coding: utf-8 -*-
"""看门狗（R1 假活根治核心）。

v2 事故：账号初始化失败只打一行 warning，进程不死、容器不退出 → 停摆 37 天假活。
v3 对策：周期检查健康账号数，无可用账号持续超过阈值（默认 5 分钟）→
① 结构化告警（Bot 通知 + stderr JSON 行）② CRITICAL 日志 ③ 非零退出（exit 1），
由 Docker restart 策略拉起重连。
"""
import asyncio
import json
import os
import sys
import time
from typing import Any, Callable, Optional

from loguru import logger

import tg_forwarder.core.accounts as accounts_mod


class Supervisor:
    """无可用账号超时 → 结构化告警 + 非零退出。"""

    def __init__(
        self,
        account_manager: Any,
        config: Any,
        on_critical: Optional[Callable[[str], Any]] = None,
        exit_func: Optional[Callable[[int], Any]] = None,
    ):
        self._am = account_manager
        self._timeout_minutes = 5
        self._interval_seconds = 60
        try:
            watchdog = getattr(config, "watchdog", None)
            if watchdog is not None:
                self._timeout_minutes = watchdog.timeout_minutes
                self._interval_seconds = watchdog.interval_seconds
        except Exception:
            pass

        self._on_critical = on_critical
        # 测试可注入；生产 os._exit（在 asyncio 内 SystemExit 不可靠）
        self._exit = exit_func or os._exit
        self._no_healthy_since: Optional[float] = None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """周期检查健康账号；无可用账号超时 → 告警 + 退出。"""
        logger.info(
            f"🐕 看门狗启动（间隔 {self._interval_seconds}s，"
            f"无可用账号阈值 {self._timeout_minutes}min）"
        )
        while True:
            await asyncio.sleep(self._interval_seconds)
            healthy = self._healthy_count()
            now = time.time()

            if healthy > 0:
                if self._no_healthy_since is not None:
                    logger.info("✅ 账号恢复健康，看门狗计时器已重置。")
                self._no_healthy_since = None
                continue

            if self._no_healthy_since is None:
                self._no_healthy_since = now
                logger.warning("⚠️ 无可用账号，看门狗开始计时。")
                continue

            elapsed_min = (now - self._no_healthy_since) / 60.0
            if elapsed_min >= self._timeout_minutes:
                await self._trigger_shutdown(elapsed_min)
                return
            logger.warning(
                f"⚠️ 无可用账号已持续 {elapsed_min:.1f}min "
                f"(阈值 {self._timeout_minutes}min)"
            )

    def _healthy_count(self) -> int:
        try:
            return len(self._am.healthy_accounts())
        except Exception as e:
            logger.error(f"看门狗读取健康账号失败: {e}")
            return 0

    # ------------------------------------------------------------------
    # 触发退出（R1）
    # ------------------------------------------------------------------

    async def _trigger_shutdown(self, elapsed_min: float) -> None:
        event = {
            "event": "account_all_dead",
            "no_healthy_minutes": round(elapsed_min, 1),
            "threshold_minutes": self._timeout_minutes,
            "accounts": self._am.all_status().get("accounts", []),
        }
        message = (
            f"🚨 [account_all_dead] 无可用账号已持续 "
            f"{elapsed_min:.1f} 分钟（阈值 {self._timeout_minutes} 分钟），"
            f"进程将退出等待 Docker 重启。账号状态: "
            f"{[(a.get('session_name'), a.get('last_error')) for a in event['accounts']]}"
        )

        # ① 结构化 stderr 行（容器日志可 grep）
        try:
            print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
        except Exception:
            pass

        # ② CRITICAL 日志
        logger.critical(message)

        # ③ 外部告警（Bot 通知），最多等 10s，失败不阻断退出
        if self._on_critical:
            try:
                res = self._on_critical(message)
                if asyncio.iscoroutine(res):
                    await asyncio.wait_for(res, timeout=10)
            except Exception as e:
                logger.error(f"看门狗告警回调失败（忽略，继续退出）: {e}")

        logger.critical("看门狗触发非零退出 (exit 1)。")
        # os._exit 避开 asyncio 清理卡死，确保容器收到非零码
        self._exit(1)
