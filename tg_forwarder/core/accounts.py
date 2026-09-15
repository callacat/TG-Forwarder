# -*- coding: utf-8 -*-
"""账号生命周期管理（R1/R2/R5/R6，P1/P2/P3/P9 根治）。

- P1 根治：启动连接指数退避重试 ≥5 次，全部失败 → 抛 AccountStartupError
  （main 层转为结构化告警 + 非零退出，Docker restart 拉起）；
- P9 根治：session 文件不存在 → 结构化 ERROR + 标记 unavailable，禁止交互式
  attach 登录阻塞无人值守进程；
- P3/R5：proxy 多级降级链 direct(443) → direct(80) → http 传输模式 → 配置代理；
- R2：运行期 maintain() 周期探活，断连自动重连（同退避策略）。
"""
import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

# 降级链级别
PROXY_LEVEL_DIRECT_443 = 0
PROXY_LEVEL_DIRECT_80 = 1
PROXY_LEVEL_HTTP_MODE = 2
PROXY_LEVEL_CONFIGURED = 3

PROXY_LEVEL_NAMES = {
    PROXY_LEVEL_DIRECT_443: "direct:443",
    PROXY_LEVEL_DIRECT_80: "direct:80",
    PROXY_LEVEL_HTTP_MODE: "http-mode",
    PROXY_LEVEL_CONFIGURED: "configured-proxy",
}

# 连接超时（单次 socket 操作，交给 telethon MTProtoSender）
CONNECT_TIMEOUT_SECONDS = 20
# 降级链每级墙钟上限：blocked 端口在此被取消并试下一级，
# 防 connection_retries=None 的内部无限重试吞掉降级
LEVEL_CONNECT_TIMEOUT_SECONDS = 25

# telethon 连接子类：强制 80 端口（不触碰 session，auth_key 与 DC 绑定不受影响）
from telethon.network.connection.tcpfull import ConnectionTcpFull


class ConnectionTcpPort80(ConnectionTcpFull):
    def __init__(self, ip, port, dc_id, **kwargs):
        super().__init__(ip, 80, dc_id, **kwargs)


def backoff_delays(attempts: int = 5, base: float = 2.0, factor: float = 2.0,
                   max_delay: float = 60.0) -> List[float]:
    """指数退避延迟序列（base * factor^i，封顶 max_delay）。供测试与重试共用。"""
    return [min(base * (factor ** i), max_delay) for i in range(attempts)]


@dataclass
class AccountState:
    """单账号运行状态（/status、看门狗、HEALTHCHECK 的数据源）。"""

    session_name: str
    api_id: int
    api_hash: str
    connected: bool = False
    authorized: bool = False
    healthy: bool = False
    unavailable: bool = False  # 无 session/未授权等终态，不再重试
    proxy_level: int = PROXY_LEVEL_DIRECT_443
    flood_wait_until: float = 0.0
    flood_wait_count: int = 0
    last_error: Optional[str] = None
    client: Any = None
    # 重连任务与控制
    reconnect_task: Optional[Any] = field(default=None, repr=False)


class AccountStartupError(RuntimeError):
    """启动阶段全部账号连接失败（R1：进程应结构化告警后非零退出）。"""


class ProxyFallback:
    """proxy 多级降级链（R5）。

    链：direct(443) → direct(80) → http 传输模式 → 配置的代理。
    每级失败记录 warning 并尝试下一级；成功后记录生效级别。
    可通过 ``levels`` 参数裁剪链（测试注入用）。

    direct(80) 实现（telethon API 已核实）：连接端口来自 session
    （``self._connection(session.server_address, session.port, ...)``），
    在连接前用公开 API ``session.set_dc(dc_id, ip, 80)`` 覆盖端口；
    auth_key 与 DC 绑定不受端口影响。
    http 模式：Telethon 自带 ConnectionTcpHttp 传输（与 TCP 同端口 443，
    伪装为 HTTP 流量，用于 443 被深度干扰但 TCP 可通的场景）。
    """

    def __init__(
        self,
        configured_proxy=None,
        levels: Optional[List[int]] = None,
        connect_factory: Optional[Callable] = None,
    ):
        # configured_proxy: ProxyConfig.get_telethon_proxy() 结果（None=未配代理）
        self.configured_proxy = configured_proxy
        self.levels = levels or [
            PROXY_LEVEL_DIRECT_443,
            PROXY_LEVEL_DIRECT_80,
            PROXY_LEVEL_HTTP_MODE,
        ] + ([PROXY_LEVEL_CONFIGURED] if configured_proxy else [])

        # connect_factory(level, session_path, api_id, api_hash) -> coroutine(client)
        # 生产用 _default_connect_factory；测试注入 mock
        self._connect_factory = connect_factory or self._default_connect_factory

    # ------------------------------------------------------------------
    # 生产连接工厂（真实 Telethon）
    # ------------------------------------------------------------------

    async def _default_connect_factory(self, level: int, session_path: str,
                                       api_id: int, api_hash: str):
        """真实 Telethon 工厂：构造 + **连接**（老马验收发现的 P0 修复点）。

        旧版只构造不 connect()，下游 is_user_authorized() 立即
        "Cannot send requests while disconnected"，各级全部假性失败。
        连接失败自行清理（disconnect 吞异常后重抛），由降级链捕获后试下一级。
        """
        from telethon import TelegramClient

        proxy = None
        if level == PROXY_LEVEL_CONFIGURED and self.configured_proxy:
            proxy = self.configured_proxy

        # connection_retries=None：运行期 telethon 无限自动重连（R2，v2 语义）；
        # 启动降级链靠下面 wait_for 限时防内部重试吞掉降级，二者解耦。
        client = TelegramClient(
            session_path,
            api_id,
            api_hash,
            proxy=proxy,
            use_ipv6=False,
            connection_retries=None,
            retry_delay=1,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

        # direct(80)：用连接子类强制端口，不动 session（set_dc 是同步函数，
        # await 会 TypeError；且会重写 session 行/auth_key 选择，有副作用）
        if level == PROXY_LEVEL_DIRECT_80:
            client._connection = ConnectionTcpPort80

        # http 模式：HTTP 传输连接类（telethon 实际类名是 ConnectionHttp）
        if level == PROXY_LEVEL_HTTP_MODE:
            from telethon.network.connection.http import ConnectionHttp

            client._connection = ConnectionHttp

        try:
            # 每级限时：blocked 级别在 LEVEL_CONNECT_TIMEOUT 内被取消并试下一级，
            # 不让 connection_retries=None 的内部无限重试吞掉降级链
            await asyncio.wait_for(client.connect(), LEVEL_CONNECT_TIMEOUT_SECONDS)
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        return client

    # ------------------------------------------------------------------
    # 降级链执行
    # ------------------------------------------------------------------

    async def connect_with_fallback(self, session_path: str, api_id: int,
                                    api_hash: str) -> Any:
        """按降级链逐级尝试连接，返回 (client, 生效级别)。全链失败抛最后异常。

        工厂契约：入参 (level, session_path, api_id, api_hash)，成功返回
        **已连接** 的 client（生产工厂内部 connect+失败自清理），失败抛异常。
        """
        last_error: Optional[Exception] = None
        for level in self.levels:
            try:
                logger.debug(f"连接尝试: {PROXY_LEVEL_NAMES[level]} ({session_path})")
                client = await self._connect_factory(
                    level, session_path, api_id, api_hash
                )
                return client, level
            except Exception as e:
                last_error = e
                logger.warning(
                    f"连接失败 [{PROXY_LEVEL_NAMES[level]}] {session_path}: "
                    f"{type(e).__name__}: {e}"
                )
                # 失败 client 由工厂自行清理（生产实现约定）
        raise last_error if last_error else RuntimeError("降级链为空")


class AccountManager:
    """账号生命周期管理器：启动重试 / 健康探活 / 自动重连 / FloodWait 记录。"""

    def __init__(self, db=None, data_dir: str = "/app/data",
                 fallback_factory: Optional[Callable] = None):
        self.db = db
        self.data_dir = data_dir
        self._states: Dict[str, AccountState] = {}
        # ProxyFallback 工厂（测试可注入 mock fallback）
        self._fallback_factory = fallback_factory or self._default_fallback_factory

    # ------------------------------------------------------------------
    # 启动（R1：指数退避重试，全部失败 → AccountStartupError）
    # ------------------------------------------------------------------

    async def start(self, config, max_attempts: int = 5) -> List[Any]:
        """连接全部启用账号。返回健康客户端列表。

        - 有任何账号成功：不抛（部分可用即可运行，其余进重连循环）；
        - 全部失败且账号列表非空：抛 AccountStartupError（R1）。
        """
        enabled = [a for a in (config.accounts or []) if a.enabled]
        if not enabled:
            logger.warning("⚠️ 没有配置任何启用的用户账号！")
            return []

        for acc in enabled:
            await self._start_one(acc, config, max_attempts)

        healthy = self.healthy_accounts()
        if not healthy:
            raise AccountStartupError(
                "全部账号启动失败（指数退避重试耗尽）。"
                f"错误: {[(s.session_name, s.last_error) for s in self._states.values()]}"
            )
        return healthy

    def _default_fallback_factory(self, config) -> ProxyFallback:
        proxy = config.proxy.get_telethon_proxy() if config.proxy else None
        return ProxyFallback(configured_proxy=proxy)

    async def _start_one(self, acc, config, max_attempts: int) -> None:
        state = AccountState(session_name=acc.session_name, api_id=acc.api_id,
                             api_hash=acc.api_hash)
        self._states[acc.session_name] = state

        # P9：无 session 文件 → 结构化 ERROR + unavailable，禁止交互登录
        session_path = os.path.join(self.data_dir, f"{acc.session_name}.session")
        if not os.path.exists(session_path):
            state.unavailable = True
            state.last_error = "session_file_missing"
            logger.error(
                f'[account_no_session] 账号 {acc.session_name} 无 session 文件，'
                f'跳过（无人值守模式禁止交互登录）。路径: {session_path}'
            )
            return

        fallback = self._fallback_factory(config)
        delays = backoff_delays(max_attempts)

        for attempt in range(1, max_attempts + 1):
            try:
                client, level = await fallback.connect_with_fallback(
                    session_path, acc.api_id, acc.api_hash
                )
                # is_user_authorized 校验（未授权等同失败）
                if not await client.is_user_authorized():
                    await client.disconnect()
                    raise RuntimeError("账号未授权 (not authorized)")
                state.client = client
                state.connected = True
                state.authorized = True
                state.healthy = True
                state.proxy_level = level
                state.unavailable = False
                state.last_error = None
                # 转发轮询键（forwarder._get_next_client 使用，v2 对齐）
                client.session_name_for_forwarder = acc.session_name
                logger.success(
                    f"✅ 账号 {acc.session_name} 登录成功 "
                    f"[{PROXY_LEVEL_NAMES[level]}] (第 {attempt} 次尝试)"
                )
                return
            except Exception as e:
                state.last_error = f"{type(e).__name__}: {e}"[:300]
                logger.warning(
                    f"⚠️ 账号 {acc.session_name} 第 {attempt}/{max_attempts} 次"
                    f"连接失败: {state.last_error}"
                )
                if attempt < max_attempts:
                    delay = delays[attempt - 1] * random.uniform(0.8, 1.2)  # jitter
                    await asyncio.sleep(delay)

        state.unavailable = True
        logger.error(
            f"[account_startup_failed] 账号 {acc.session_name} 重试 "
            f"{max_attempts} 次全部失败，标记 unavailable。"
        )

    # ------------------------------------------------------------------
    # 查询（/status、看门狗、HEALTHCHECK 数据源）
    # ------------------------------------------------------------------

    def healthy_accounts(self) -> List[Any]:
        """健康客户端列表（connected + authorized）。"""
        return [s.client for s in self._states.values()
                if s.healthy and s.client is not None]

    def all_status(self) -> Dict[str, Any]:
        """全账号状态（/api/status 与 /health 消费）。"""
        accounts = []
        for s in self._states.values():
            flood_active = time.time() < s.flood_wait_until
            accounts.append({
                "session_name": s.session_name,
                "connected": s.connected,
                "authorized": s.authorized,
                "healthy": s.healthy,
                "unavailable": s.unavailable,
                "flood_wait": int(s.flood_wait_until - time.time()) if flood_active else 0,
                "flood_wait_count": s.flood_wait_count,
                "proxy_level": PROXY_LEVEL_NAMES.get(s.proxy_level, str(s.proxy_level)),
                "last_error": s.last_error,
            })
        return {"accounts": accounts}

    def record_flood_wait(self, session_name: str, seconds: int) -> None:
        """转发触发 FloodWait 时由 forwarder 回写。"""
        state = self._states.get(session_name)
        if state:
            state.flood_wait_until = time.time() + seconds + 5
            state.flood_wait_count += 1
            logger.warning(
                f"客户端 {session_name} 触发 FloodWait: {seconds} 秒 "
                f"(累计 {state.flood_wait_count} 次)"
            )

    # ------------------------------------------------------------------
    # 运行期维护（R2：断连自动重连）
    # ------------------------------------------------------------------

    async def maintain_once(self) -> None:
        """单轮探活（R2）：断连账号立即置 unhealthy 再后台重连。

        P2 修复：is_connected()==False 时**立即**把 healthy/connected 翻 False
        （旧版只在 _reconnect 首次 connect 失败后才置位，且本方法零调用点 →
        运行期假活防线失效）。这样 Supervisor._healthy_count() 与 /health 立刻
        看到真实断连，超阈值即触发 R1 退出。
        """
        for state in list(self._states.values()):
            if state.unavailable or state.client is None:
                continue
            try:
                connected = state.client.is_connected()
            except Exception:
                connected = False

            if not connected:
                # 立即置 unhealthy（不依赖重连结果），供看门狗/health 读真实值
                if state.healthy or state.connected:
                    state.healthy = False
                    state.connected = False
                    logger.warning(
                        f"⚠️ 账号 {state.session_name} 运行期断连，立即标记 "
                        f"unhealthy → 后台重连（计入看门狗无可用账号计时）"
                    )
                # 断连 → 重连（后台任务，避免阻塞探活循环）
                if state.reconnect_task is None or state.reconnect_task.done():
                    state.reconnect_task = asyncio.create_task(
                        self._reconnect(state)
                    )
            else:
                state.connected = True
                state.healthy = True

    async def maintenance_loop(self, interval_seconds: int = 30) -> None:
        """周期维护循环（R2）：每 interval 探活断连账号并触发重连。

        main 层纳入 tasks 列表，随进程优雅取消（asyncio.CancelledError）。
        与 Supervisor 并存：本循环负责置 unhealthy，看门狗负责据此超时退出。
        """
        logger.info(f"🩺 账号维护循环启动（每 {interval_seconds}s 探活+重连）")
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                await self.maintain_once()
        except asyncio.CancelledError:
            logger.info("账号维护循环收到取消信号，优雅退出。")
            raise

    async def _reconnect(self, state: AccountState, max_attempts: int = 5) -> None:
        """断线重连：指数退避；连续失败仅标记，由看门狗按 R1 处置。"""
        try:
            await state.client.disconnect()
        except Exception:
            pass

        delays = backoff_delays(max_attempts)
        for attempt in range(1, max_attempts + 1):
            try:
                await asyncio.wait_for(
                    state.client.connect(), timeout=CONNECT_TIMEOUT_SECONDS
                )
                if await state.client.is_user_authorized():
                    state.connected = True
                    state.healthy = True
                    state.last_error = None
                    logger.success(
                        f"✅ 账号 {state.session_name} 重连成功 (第 {attempt} 次)"
                    )
                    return
                raise RuntimeError("重连后未授权")
            except Exception as e:
                state.last_error = f"{type(e).__name__}: {e}"[:300]
                state.connected = False
                state.healthy = False
                logger.warning(
                    f"⚠️ 账号 {state.session_name} 重连第 {attempt}/{max_attempts}"
                    f"次失败: {state.last_error}"
                )
                if attempt < max_attempts:
                    await asyncio.sleep(delays[attempt - 1])

        logger.error(
            f"[account_reconnect_failed] 账号 {state.session_name} "
            f"重连 {max_attempts} 次失败，等待下一轮探活重试。"
        )

    def mark_unhealthy(self, session_name: str, error: str) -> None:
        """标记账号不可用（main 层 client 轮询循环异常退出时调用，R1）。

        让维护循环/看门狗立即读到真实状态，触发重连尝试；持续无可用账号时
        由 Supervisor 输出 [account_all_dead] 结构化告警并 exit 1。
        """
        state = self._states.get(session_name)
        if state and (state.healthy or state.connected):
            state.connected = False
            state.healthy = False
            state.last_error = f"poll_died: {error}"[:300]
            logger.warning(
                f"⚠️ 账号 {session_name} 轮询循环异常退出，标记 unhealthy（等待重连）"
            )

    async def stop(self) -> None:
        """优雅退出：取消重连任务并断开全部客户端。"""
        for state in self._states.values():
            if state.reconnect_task and not state.reconnect_task.done():
                state.reconnect_task.cancel()
            if state.client is not None:
                try:
                    if state.client.is_connected():
                        await state.client.disconnect()
                except Exception:
                    pass
        logger.info("账号管理器已停止。")
