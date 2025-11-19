# bot_service.py
import logging
import time 
import os 
import asyncio
from telethon import TelegramClient, events, Button
from telethon.tl.types import Message
from typing import Callable, Awaitable
from datetime import datetime, timezone 
from models import Config 
from link_checker import LinkChecker 
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (
    BotCommand, 
    BotCommandScopeDefault
)

import database
import web_server # 引入 web_server 以获取实时规则统计

from loguru import logger

class BotService:
    def __init__(self, config: Config, bot_client: TelegramClient, forwarder: 'UltimateForwarder', link_checker: LinkChecker, reload_config_func: Callable[[], Awaitable[str]]):
        self.config = config.bot_service
        self.bot = bot_client
        self.forwarder = forwarder
        self.link_checker = link_checker
        self.admin_ids = self.config.admin_user_ids if self.config else []
        self.reload_config = reload_config_func
        self.start_time = datetime.now(timezone.utc)

    def is_admin(self, event: events.NewMessage.Event) -> bool:
        """检查发件人是否为管理员"""
        if event.is_group and event.sender_id is None:
            return False
        
        # 动态获取最新的管理员 ID (如果配置支持热重载)
        current_admin_ids = self.admin_ids
        if self.forwarder and self.forwarder.config.bot_service:
            current_admin_ids = self.forwarder.config.bot_service.admin_user_ids

        if event.sender_id not in current_admin_ids:
            return False
        return True

    async def register_commands(self):
        """注册所有 Bot 命令处理程序"""

        # --- /start ---
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_handler(event: events.NewMessage.Event):
            if not self.is_admin(event): return
            
            await event.reply(
                "**🤖 TG 终极转发器控制台**\n\n"
                "Web 面板已就绪，你可以通过 Bot 进行快捷运维。\n\n"
                "**常用命令:**\n"
                "`/status` - 查看详细运行状态\n"
                "`/reload` - 重载所有配置文件\n" 
                "`/check` - 启动失效链接检测\n"
                "`/ids` - 导出源频道 ID 列表"
            )

        # --- /status (升级版) ---
        @self.bot.on(events.NewMessage(pattern='/status'))
        async def status_handler(event: events.NewMessage.Event):
            if not self.is_admin(event): return

            # 1. 运行时间
            uptime = datetime.now(timezone.utc) - self.start_time
            days = uptime.days
            hours, rem = divmod(uptime.seconds, 3600)
            minutes, seconds = divmod(rem, 60)
            uptime_str = f"{days}天 {hours}小时 {minutes}分"

            # 2. 客户端状态
            client_status = "❌ 无可用账号"
            if self.forwarder and self.forwarder.clients:
                count = len(self.forwarder.clients)
                flood_clients = [c.session_name_for_forwarder for c in self.forwarder.clients 
                                 if self.forwarder.client_flood_wait.get(c.session_name_for_forwarder, 0) > time.time()]
                
                if flood_clients:
                    client_status = f"⚠️ {count} 个在线 ({len(flood_clients)} 个 FloodWait)"
                else:
                    client_status = f"✅ {count} 个在线 (状态良好)"

            # 3. 数据库与规则统计 (与 Web 端对齐)
            try:
                db_stats = await database.get_db_stats()
                
                # 从内存中获取规则统计
                bl = web_server.rules_db.ad_filter
                bl_count = len(bl.keywords_substring or []) + len(bl.keywords_word or []) + len(bl.file_name_keywords or []) + len(bl.patterns or [])
                wl_count = len(web_server.rules_db.whitelist.keywords or [])
                
                cf_count = 0
                if web_server.rules_db.content_filter and web_server.rules_db.content_filter.meaningless_words:
                    cf_count = len(web_server.rules_db.content_filter.meaningless_words)
                
                rep_count = len(web_server.rules_db.replacements or {})
                rule_count = len(web_server.rules_db.distribution_rules)
                source_count = len(web_server.rules_db.sources)

                stats_msg = (
                    f"**📊 核心指标**\n"
                    f"• 运行时间: `{uptime_str}`\n"
                    f"• 用户账号: {client_status}\n"
                    f"• 数据库去重: `{db_stats.get('dedup_hashes', 0)}` 条\n"
                    f"• 失效链接: `{db_stats.get('invalid_links', 0)}` 个\n\n"
                    f"**🛡 规则统计**\n"
                    f"• 监控源: `{source_count}` | 分发规则: `{rule_count}`\n"
                    f"• 黑名单: `{bl_count}` | 白名单: `{wl_count}`\n"
                    f"• 过滤词: `{cf_count}` | 替换词: `{rep_count}`"
                )
            except Exception as e:
                logger.error(f"获取 Bot 统计失败: {e}")
                stats_msg = f"❌ 获取统计数据失败: {e}"

            # 发送带按钮的消息 (未来可扩展功能)
            await event.reply(stats_msg)

        # --- /reload ---
        @self.bot.on(events.NewMessage(pattern='/reload'))
        async def reload_handler(event: events.NewMessage.Event):
            if not self.is_admin(event): return
            
            msg = await event.reply("🔄 正在重新加载配置和规则数据库...")
            try:
                start_ts = time.time()
                result_msg = await self.reload_config()
                duration = round(time.time() - start_ts, 2)
                
                await msg.edit(f"✅ **重载完成** ({duration}s)\n\n{result_msg}")
            except Exception as e:
                logger.error(f"热重载失败: {e}")
                await msg.edit(f"❌ **重载失败**\n\n错误信息: `{e}`")

        # --- /check (原 /run_checklinks) ---
        @self.bot.on(events.NewMessage(pattern='/check'))
        async def checklinks_handler(event: events.NewMessage.Event):
            if not self.is_admin(event): return

            if not self.link_checker:
                await event.reply("❌ 链接检测器未启用。请检查配置。")
                return
                
            msg = await event.reply("🕵️‍♂️ **开始检测失效链接...**\n这可能需要几分钟，请稍候。")
            try:
                await self.link_checker.run()
                # 再次获取统计以显示结果
                db_stats = await database.get_db_stats()
                invalid_count = db_stats.get('invalid_links', 0)
                await msg.edit(f"✅ **检测完成**\n\n当前数据库中共有 `{invalid_count}` 个失效链接记录。")
            except Exception as e:
                logger.error(f"链接检测出错: {e}")
                await msg.edit(f"❌ 检测过程中出错: {e}")

        # --- /ids (原 /export_sources) ---
        @self.bot.on(events.NewMessage(pattern='/ids'))
        async def export_sources_handler(event: events.NewMessage.Event):
            if not self.is_admin(event): return

            # 优先从 Web 数据库读取，因为那里是最新的
            sources = web_server.rules_db.sources
            if not sources:
                await event.reply("📭 当前没有配置任何监控源。")
                return
            
            output = "**📋 监控源列表 (ID 映射)**\n\n"
            
            for s in sources:
                name = s.cached_title or s.identifier
                status = "✅" if s.resolved_id else "⚠️"
                id_str = f"`{s.resolved_id}`" if s.resolved_id else "*未解析*"
                
                output += f"{status} **{name}**\n"
                output += f"└ ID: {id_str}\n\n"
            
            await event.reply(output)

        # --- 自动设置 Bot 命令菜单 ---
        try:
            commands = [
                BotCommand("status", "查看详细运行仪表盘"),
                BotCommand("reload", "重载配置 (Web修改后点此)"),
                BotCommand("ids", "显示监控源的真实 ID"),
                BotCommand("check", "立即运行失效链接检测"),
                BotCommand("start", "显示帮助信息")
            ]
            
            await self.bot(SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="",
                commands=commands
            ))
            logger.info("✅ Bot 命令菜单已自动同步。")
        except Exception as e:
            logger.warning(f"无法设置 Bot 菜单: {e}")