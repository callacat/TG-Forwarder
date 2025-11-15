# bot_service.py
import logging
from telethon import TelegramClient, events
from telethon.tl.types import Message
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

class BotService:
    def __init__(self, config, bot_client: TelegramClient, forwarder, link_checker, reload_config_func: Callable[[], Awaitable[str]]):
        self.config = config.bot_service
        self.bot = bot_client
        self.forwarder = forwarder
        self.link_checker = link_checker
        self.admin_ids = self.config.admin_user_ids
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

            # (新) 尝试从 forwarder 获取客户端状态
            client_status = "未知"
            if self.forwarder and self.forwarder.clients:
                client_count = len(self.forwarder.clients)
                # 检查 FloodWait
                flood_clients = [
                    cid[:5] for cid, expiry in self.forwarder.client_flood_wait.items() 
                    if expiry > time.time()
                ]
                if flood_clients:
                    client_status = f"⚠️ {client_count} 个客户端运行中 ( {len(flood_clients)} 个正在 FloodWait: {', '.join(flood_clients)}... )"
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