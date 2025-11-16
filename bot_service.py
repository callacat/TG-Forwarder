# bot_service.py
import logging
import time # (新) 导入 time
import os # (新) 导入 os，用于处理路径
from telethon import TelegramClient, events
from telethon.tl.types import Message
from typing import Callable, Awaitable
from datetime import datetime, timezone # (新) 导入 datetime, timezone
from forwarder_core import Config # (新)
from link_checker import LinkChecker # (新)
# (新) 导入 BotCommand 相关
from telethon.tl.functions.bots import SetBotCommandsRequest
# (新) 修复：导入所有需要的 Scope 类型
from telethon.tl.types import (
    BotCommand, 
    BotCommandScopeDefault, 
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators
)

logger = logging.getLogger(__name__)

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
        if event.sender_id not in self.admin_ids:
            logger.warning(f"未授权的访问: 用户 {event.sender_id} 尝试执行命令。")
            return False
        return True

    async def register_commands(self):
        """注册所有 Bot 命令处理程序"""

        # --- /start ---
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_handler(event: events.NewMessage.Event):
            if not self.is_admin(event):
                await event.reply("❌ 你无权访问此 Bot。")
                return
            
            await event.reply(
                "**TG 终极转发器 Bot 已启动**\n\n"
                "这是一个私有 Bot，用于控制转发服务。\n\n"
                "**可用命令:**\n"
                "`/status` - 查看服务运行状态。\n"
                "`/reload` - 热重载 `config.yaml` 文件。\n"
                "`/run_checklinks` - 手动触发一次失效链接检测。"
            )

        # --- /status ---
        @self.bot.on(events.NewMessage(pattern='/status'))
        async def status_handler(event: events.NewMessage.Event):
            if not self.is_admin(event): return

            uptime = datetime.now(timezone.utc) - self.start_time
            uptime_str = str(uptime).split('.')[0] # 移除微秒

            # --- (新) 核心修复 ---
            client_status = "未知"
            if self.forwarder and self.forwarder.clients:
                client_count = len(self.forwarder.clients)
                
                flood_clients = []
                for client in self.forwarder.clients:
                    # (新) 使用我们附加的 session_name 作为唯一键
                    session_key = client.session_name_for_forwarder 
                    if self.forwarder.client_flood_wait.get(session_key, 0) > time.time():
                        # (新) 直接附加 session_key (即 session_name)
                        flood_clients.append(session_key)

                if flood_clients:
                    client_status = f"⚠️ {client_count} 个客户端运行中 ( {len(flood_clients)} 个正在 FloodWait: {', '.join(flood_clients)} )"
                else:
                    client_status = f"✅ {client_count} 个客户端运行中 (全部正常)"


            await event.reply(
                "**TG 终极转发器状态**\n\n"
                f"**服务状态:** ✅ 运行中\n"
                f"**已运行时间:** {uptime_str}\n"
                f"**用户账号:** {client_status}"
            )

        # --- /reload ---
        @self.bot.on(events.NewMessage(pattern='/reload'))
        async def reload_handler(event: events.NewMessage.Event):
            if not self.is_admin(event): return
            
            await event.reply("🔄 正在热重载 `config.yaml`...")
            try:
                # 调用从 main 传入的重载函数
                result_msg = await self.reload_config()
                await event.reply(result_msg)
            except Exception as e:
                logger.error(f"热重载时发生意外错误: {e}")
                await event.reply(f"❌ 热重载时发生意外错误: {e}")

        # --- /run_checklinks ---
        @self.bot.on(events.NewMessage(pattern='/run_checklinks'))
        async def checklinks_handler(event: events.NewMessage.Event):
            if not self.is_admin(event): return

            if not self.link_checker:
                await event.reply("❌ 链接检测器未启用或未初始化。")
                return
                
            await event.reply("⌛️ 正在启动失效链接检测... (这可能需要几分钟)")
            try:
                # 异步运行检测
                await self.link_checker.run()
                await event.reply("✅ 失效链接检测完成。")
            except Exception as e:
                logger.error(f"运行链接检测时出错: {e}")
                await event.reply(f"❌ 运行链接检测时出错: {e}")

        # --- (新) 自动设置 Bot 命令列表 (修复问题1, 2, 3) ---
        try:
            logger.info("正在为 Bot 设置命令列表...")
            
            # 英文命令
            en_commands = [
                BotCommand(command="start", description="Show welcome message and help"),
                BotCommand(command="status", description="Check service running status"),
                BotCommand(command="reload", description="Reload the config.yaml file"),
                BotCommand(command="run_checklinks", description="Manually trigger a link check")
            ]
            
            # 中文命令
            zh_commands = [
                BotCommand(command="start", description="显示欢迎和帮助信息"),
                BotCommand(command="status", description="查看服务运行状态"),
                BotCommand(command="reload", description="热重载 config.yaml 配置文件"),
                BotCommand(command="run_checklinks", description="手动触发一次失效链接检测")
            ]
            
            # (新) 修复问题1：定义所有三个开关 + 默认
            scopes_to_set = [
                (BotCommandScopeDefault(), "Default (默认)"),
                (BotCommandScopeAllPrivateChats(), "All Private Chats (所有私聊)"),
                (BotCommandScopeAllGroupChats(), "All Group Chats (所有群组)"),
                (BotCommandScopeAllChatAdministrators(), "All Group Admins (所有群组管理员)")
            ]
            
            for scope, scope_name in scopes_to_set:
                logger.info(f"--- 正在设置 {scope_name} 作用域的命令 ---")
                
                # 1. 设置默认 (所有语言)，使用英语
                # lang_code="" 是必须的，作为回退
                await self.bot(SetBotCommandsRequest(
                    scope=scope,
                    lang_code="", # 空 lang_code 表示默认
                    commands=en_commands
                ))

                # 2. 专门为英语用户设置 (覆盖默认)
                await self.bot(SetBotCommandsRequest(
                    scope=scope,
                    lang_code="en",
                    commands=en_commands
                ))
                
                # 3. 专门为中文用户设置 (覆盖默认)
                # (新) 修复问题2：只使用 "zh"，因为 "zh-hans" 是无效的
                await self.bot(SetBotCommandsRequest(
                    scope=scope,
                    lang_code="zh",
                    commands=zh_commands
                ))
                
                # (新) 修复问题2：移除无效的 "zh-hans" 和 "zh-hant"

            logger.info("✅ Bot 命令列表设置成功 (Default + Private + Groups + Admins)。")
        except Exception as e:
            # (新) 修复问题2：修正 import 错误后，这里的日志不应该再出现
            logger.warning(f"⚠️ 无法设置 Bot 命令列表: {e} (这不影响 Bot 运行)")