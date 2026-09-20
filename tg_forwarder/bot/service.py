# -*- coding: utf-8 -*-
"""TG-Forwarder v3 Bot 层。

BotService 对齐 v2 bot_service.py 行为，修复：

- R6/P2 独立凭据：api_id/hash 取 ``bot_service.bot_api_id/bot_api_hash``
  若非空，否则回落 ``accounts[0]``（不再强制复用 accounts[0] 凭据）。
- admin_user_ids 门禁 + 动态读取（热重载后取最新管理员列表）。
- /status 含账号健康（在线数/FloodWait 计数）、数据库统计、规则统计。
- register_commands() 注册 handlers + SetBotCommandsRequest 菜单
  （en/zh，token 签名去重，写 app_config key 'bot_command_setup'）。
"""
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Optional

from loguru import logger

from telethon import TelegramClient, events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeDefault,
)


class BotService:
    """Bot 控制台服务（admin 门禁 + 命令 + 通知 + 命令菜单同步）。"""

    def __init__(
        self,
        config: Any,
        bot_client: Optional[TelegramClient],
        account_manager: Any = None,
        forwarder: Any = None,
        reload_func: Optional[Callable[[], Awaitable[str]]] = None,
        get_clients_func: Optional[Callable[[], List[Any]]] = None,
        db: Any = None,
        repos: Any = None,
        link_checker: Any = None,
    ):
        # v2 语义：config 为完整 RuntimeConfig，取 .bot_service 段
        self.config = config.bot_service if config is not None else None
        self.bot = bot_client
        self.account_manager = account_manager
        self.forwarder = forwarder  # 引用可能为 None
        self.reload_config = reload_func
        self.get_clients = get_clients_func or (lambda: [])
        self.db = db
        self.repos = repos  # (config_repo, source_repo, rule_repo) 元组或 None
        self.link_checker = link_checker  # 可选，/check 命令用
        self.admin_ids = self.config.admin_user_ids if self.config else []
        self.start_time = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 独立凭据（R6/P2 修复）
    # ------------------------------------------------------------------

    def resolve_bot_credentials(self, runtime_config: Any = None) -> tuple:
        """解析 Bot 独立凭据：优先 bot_service.bot_api_id/bot_api_hash（非空），
        否则回落 accounts[0]（v2 P2 痛点修复）。

        返回 (api_id, api_hash)，无可用凭据时返回 (None, None)。
        """
        bs = self.config
        if runtime_config is not None and getattr(runtime_config, "bot_service", None):
            bs = runtime_config.bot_service

        # 1. 独立凭据优先
        if bs is not None:
            api_id = getattr(bs, "bot_api_id", None)
            api_hash = getattr(bs, "bot_api_hash", None)
            if api_id and api_hash:
                return api_id, api_hash

        # 2. 回落 accounts[0]
        accounts = getattr(runtime_config, "accounts", None) if runtime_config else None
        if accounts:
            first = accounts[0]
            return getattr(first, "api_id", None), getattr(first, "api_hash", None)
        return None, None

    # ------------------------------------------------------------------
    # 门禁与通知
    # ------------------------------------------------------------------

    def is_admin(self, event: Any) -> bool:
        """检查发件人是否为管理员（动态读取最新 admin_user_ids，支持热重载）。"""
        if event.is_group and getattr(event, "sender_id", None) is None:
            return False

        # 动态获取最新的管理员 ID（若 forwarder.config 支持热重载）
        current_admin_ids = self.admin_ids
        if self.forwarder is not None:
            bs = getattr(getattr(self.forwarder, "config", None), "bot_service", None)
            if bs is not None:
                current_admin_ids = getattr(bs, "admin_user_ids", current_admin_ids)

        if getattr(event, "sender_id", None) not in current_admin_ids:
            return False
        return True

    async def notify_admin(self, message: str):
        """发送通知给所有管理员（v2 对齐）。"""
        if not self.bot or not self.bot.is_connected():
            return

        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(admin_id, message)
            except Exception as e:
                logger.warning(f"无法发送通知给管理员 {admin_id}: {e}")

    # ------------------------------------------------------------------
    # 统计（/status 使用）
    # ------------------------------------------------------------------

    async def _db_stats(self) -> dict:
        """读取数据库统计（dedup_hashes / invalid_links）。"""
        if self.db is not None:
            try:
                return await self.db.get_db_stats() or {}
            except Exception as e:
                logger.warning(f"读取数据库统计失败: {e}")
        return {}

    # ------------------------------------------------------------------
    # 命令注册
    # ------------------------------------------------------------------

    def register_commands(self):
        """注册所有 Bot 命令处理程序（同步部分；需在事件循环内调用）。

        命令菜单同步（含 token 签名去重）是 async，调用方再 await
        ``setup_command_menu()`` 完成菜单注册。
        """
        self._register_handlers()

    async def setup_command_menu(self):
        """自动设置 Bot 命令菜单（en/zh，token 签名去重，v2 对齐）。

        逻辑：生成当前 Bot Token 签名，与 app_config 表 'bot_command_setup'
        中存储的签名比对——只有 Token 变了或首次运行时才设置菜单。
        """
        try:
            if not self.config or not self.config.bot_token:
                return

            # 生成当前 Bot 的 Token 签名（简单哈希标识）
            token = self.config.bot_token
            current_token_sig = token[:10] + "***" + token[-5:]

            # 从数据库获取上次设置菜单的 Bot 签名
            stored_data = await self._get_app_config("bot_command_setup")
            stored_sig = (stored_data or {}).get("token_signature", "")

            # 只有当签名不匹配（新 Bot 或从未设置过）时才执行
            if stored_sig == current_token_sig:
                logger.info("Bot 命令菜单已是最新，跳过设置。")
                return

            logger.info("检测到 Bot 变更 (或首次运行)，正在初始化命令菜单...")

            # 英文命令（默认）
            en_commands = [
                BotCommand("status", "Show system dashboard"),
                BotCommand("reload", "Reload configuration"),
                BotCommand("ids", "Show source channel IDs"),
                BotCommand("check", "Run link checker"),
                BotCommand("start", "Show help message"),
            ]

            # 中文命令
            zh_commands = [
                BotCommand("status", "查看详细运行仪表盘"),
                BotCommand("reload", "重载配置 (Web修改后点此)"),
                BotCommand("ids", "显示监控源的真实 ID"),
                BotCommand("check", "立即运行失效链接检测"),
                BotCommand("start", "显示帮助信息"),
            ]

            # 1. 设置默认命令（Global）
            await self.bot(
                SetBotCommandsRequest(
                    scope=BotCommandScopeDefault(),
                    lang_code="",
                    commands=en_commands,
                )
            )

            # 2. 设置中文命令（lang_code 校验严格，逐个尝试并忽略错误）
            for lang in ["zh", "zh-hans", "zh-hant"]:
                try:
                    await self.bot(
                        SetBotCommandsRequest(
                            scope=BotCommandScopeDefault(),
                            lang_code=lang,
                            commands=zh_commands,
                        )
                    )
                except Exception as e:
                    logger.debug(f"设置 Bot 菜单语言 '{lang}' 失败 (可忽略): {e}")

            # 3. 更新数据库状态
            await self._save_app_config("bot_command_setup", {"token_signature": current_token_sig})
            logger.success("✅ Bot 命令菜单已成功同步。")

        except Exception as e:
            logger.warning(f"Bot 命令菜单同步过程出现异常 (不影响核心功能): {e}")

    async def _get_app_config(self, key: str) -> dict:
        """读取 app_config（优先仓储，其次 db.get_config_json 兼容层）。"""
        if self.repos:
            config_repo = self.repos[0]
            try:
                data = config_repo.get(key)
                if asyncio_iscoroutine(data):
                    data = await data
                if isinstance(data, dict):
                    return data
                return {}
            except Exception as e:
                logger.debug(f"读取 app_config '{key}' 失败: {e}")
        if self.db is not None and hasattr(self.db, "get_config_json"):
            try:
                return await self.db.get_config_json(key) or {}
            except Exception as e:
                logger.debug(f"读取 app_config '{key}' 失败(db 兼容层): {e}")
        return {}

    async def _save_app_config(self, key: str, data: dict):
        """写入 app_config（优先仓储，其次 db.save_config_json 兼容层）。"""
        if self.repos:
            config_repo = self.repos[0]
            try:
                result = config_repo.save(key, data)
                if asyncio_iscoroutine(result):
                    await result
                return
            except Exception as e:
                logger.debug(f"写入 app_config '{key}' 失败: {e}")
        if self.db is not None and hasattr(self.db, "save_config_json"):
            try:
                await self.db.save_config_json(key, data)
            except Exception as e:
                logger.debug(f"写入 app_config '{key}' 失败(db 兼容层): {e}")

    def _register_handlers(self):
        """注册 /start /status /reload /check /ids handlers（v2 文案对齐）。"""

        @self.bot.on(events.NewMessage(pattern="/start"))
        async def start_handler(event):
            if not self.is_admin(event):
                return
            await event.reply(
                "**🤖 TG 终极转发器控制台**\n\n"
                "Web 面板已就绪，你可以通过 Bot 进行快捷运维。\n\n"
                "**可用命令:**\n"
                "`/status` - 查看详细运行状态\n"
                "`/reload` - 重载所有配置文件\n"
                "`/check` - 启动失效链接检测\n"
                "`/ids` - 导出源频道 ID 列表"
            )

        @self.bot.on(events.NewMessage(pattern="/status"))
        async def status_handler(event):
            if not self.is_admin(event):
                return

            # 1. 运行时间
            uptime = datetime.now(timezone.utc) - self.start_time
            days = uptime.days
            hours, rem = divmod(uptime.seconds, 3600)
            minutes, seconds = divmod(rem, 60)
            uptime_str = f"{days}天 {hours}小时 {minutes}分"

            # 2. 客户端状态（get_clients 回调取最新列表 + FloodWait 计数）
            current_clients = self.get_clients() or []
            client_status = "❌ 无可用账号"
            if current_clients:
                count = len(current_clients)
                flood_info = ""
                if self.forwarder is not None:
                    flood_map = getattr(self.forwarder, "client_flood_wait", {}) or {}
                    flood_clients = [
                        c
                        for c in current_clients
                        if flood_map.get(getattr(c, "session_name_for_forwarder", None), 0)
                        > time.time()
                    ]
                    if flood_clients:
                        flood_info = f" ({len(flood_clients)} 个 FloodWait)"
                client_status = f"✅ {count} 个在线{flood_info}"

            # 3. 数据库与规则统计
            try:
                db_stats = await self._db_stats()
                rs = await self._rule_stats_async()
                stats_msg = (
                    f"**📊 核心指标**\n"
                    f"• 运行时间: `{uptime_str}`\n"
                    f"• 用户账号: {client_status}\n"
                    f"• 数据库去重: `{db_stats.get('dedup_hashes', 0)}` 条\n"
                    f"• 失效链接: `{db_stats.get('invalid_links', 0)}` 个\n\n"
                    f"**🛡 规则统计**\n"
                    f"• 监控源: `{rs['source_count']}` | 分发规则: `{rs['rule_count']}`\n"
                    f"• 黑名单: `{rs['bl_count']}` | 白名单: `{rs['wl_count']}`\n"
                    f"• 过滤词: `{rs['cf_count']}` | 替换词: `{rs['rep_count']}`"
                )
            except Exception as e:
                logger.error(f"获取 Bot 统计失败: {e}")
                stats_msg = f"❌ 获取统计数据失败: {e}"

            await event.reply(stats_msg)

        @self.bot.on(events.NewMessage(pattern="/reload"))
        async def reload_handler(event):
            if not self.is_admin(event):
                return

            msg = await event.reply("🔄 正在重新加载配置和规则数据库...")
            try:
                start_ts = time.time()
                result_msg = await self.reload_config()
                duration = round(time.time() - start_ts, 2)
                await msg.edit(f"✅ **重载完成** ({duration}s)\n\n{result_msg}")
            except Exception as e:
                logger.error(f"热重载失败: {e}")
                await msg.edit(f"❌ **重载失败**\n\n错误信息: `{e}`")

        @self.bot.on(events.NewMessage(pattern="/check"))
        async def checklinks_handler(event):
            if not self.is_admin(event):
                return

            if not self.link_checker:
                await event.reply("❌ 链接检测器未启用。请检查配置。")
                return

            msg = await event.reply("🕵️‍♂️ **开始检测失效链接...**\n这可能需要几分钟，请稍候。")
            try:
                await self.link_checker.run()
                db_stats = await self._db_stats()
                invalid_count = db_stats.get("invalid_links", 0)
                await msg.edit(
                    f"✅ **检测完成**\n\n当前数据库中共有 `{invalid_count}` 个失效链接记录。"
                )
            except Exception as e:
                logger.error(f"链接检测出错: {e}")
                await msg.edit(f"❌ 检测过程中出错: {e}")

        @self.bot.on(events.NewMessage(pattern="/ids"))
        async def export_sources_handler(event):
            if not self.is_admin(event):
                return

            sources = self._get_sources()
            if not sources:
                await event.reply("📭 当前没有配置任何监控源。")
                return

            output = "**📋 监控源列表 (ID 映射)**\n\n"
            for s in sources:
                name = getattr(s, "cached_title", None) or getattr(s, "identifier", "?")
                resolved = getattr(s, "resolved_id", None)
                status = "✅" if resolved else "⚠️"
                id_str = f"`{resolved}`" if resolved else "*未解析*"
                output += f"{status} **{name}**\n└ ID: {id_str}\n\n"

            await event.reply(output)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _get_sources(self) -> list:
        """获取监控源列表：优先 forwarder/内存，其次仓储。"""
        # 1. forwarder 内存（若提供）
        if self.forwarder is not None:
            snapshot = getattr(self.forwarder, "config", None)
            if snapshot is not None and getattr(snapshot, "sources", None):
                return list(snapshot.sources)
        # 2. 仓储（同步 get）
        if self.repos:
            source_repo = self.repos[1]
            try:
                data = source_repo.get_all()
                if asyncio_iscoroutine(data):
                    return []
                return [self._to_source_obj(d) for d in (data or [])]
            except Exception as e:
                logger.debug(f"读取监控源失败: {e}")
        return []

    @staticmethod
    def _to_source_obj(d: dict):
        """dict -> 简单对象（属性访问兼容 /ids 文案）。"""

        class _S:
            pass

        obj = _S()
        for k, v in (d or {}).items():
            setattr(obj, k, v)
        return obj

    async def _rule_stats_async(self) -> dict:
        """异步读取规则统计（仓储为异步时可用）。"""
        stats = {
            "source_count": 0,
            "rule_count": 0,
            "bl_count": 0,
            "wl_count": 0,
            "cf_count": 0,
            "rep_count": 0,
        }
        try:
            if self.repos:
                config_repo, source_repo, rule_repo = self.repos
                # app_config 段
                for key, field in [
                    ("ad_filter", "bl_count"),
                    ("whitelist", "wl_count"),
                    ("content_filter", "cf_count"),
                    ("replacements", "rep_count"),
                ]:
                    data = config_repo.get(key)
                    if asyncio_iscoroutine(data):
                        data = await data
                    if key == "replacements" and isinstance(data, dict):
                        stats[field] = len(data)
                    elif isinstance(data, dict):
                        if key == "ad_filter":
                            stats[field] = (
                                len(data.get("keywords_substring") or [])
                                + len(data.get("keywords_word") or [])
                                + len(data.get("file_name_keywords") or [])
                                + len(data.get("patterns") or [])
                            )
                        elif key == "whitelist":
                            stats[field] = len(data.get("keywords") or [])
                        elif key == "content_filter":
                            stats[field] = len(data.get("meaningless_words") or [])
                # sources / rules
                sources = source_repo.get_all()
                if asyncio_iscoroutine(sources):
                    sources = await sources
                stats["source_count"] = len(sources or [])
                rules = rule_repo.get_all()
                if asyncio_iscoroutine(rules):
                    rules = await rules
                stats["rule_count"] = len(rules or [])
        except Exception as e:
            logger.debug(f"异步读取规则统计失败: {e}")
        return stats


# ---------------------------------------------------------------------------
# 协程检测工具（本地小工具，避免循环依赖）
# ---------------------------------------------------------------------------

def asyncio_iscoroutine(obj) -> bool:
    """判断对象是否协程或协程函数（本地实现避免 import asyncio 循环）。"""
    import asyncio as _asyncio
    import inspect as _inspect

    return _inspect.iscoroutine(obj) or _asyncio.iscoroutinefunction(obj)
