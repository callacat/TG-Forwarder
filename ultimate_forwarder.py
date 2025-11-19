import sys
import os
import asyncio
import argparse
import yaml
import logging
from typing import List, Dict
from datetime import datetime, timezone

from loguru import logger

from telethon import TelegramClient, events, errors
from telethon.tl.types import Channel, Chat

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import database
import web_server
from models import Config, SourceConfig
from forwarder_core import UltimateForwarder
from link_checker import LinkChecker
from bot_service import BotService

# --- 全局变量 ---
clients = []
bot_client = None
forwarder = None
link_checker = None
bot_service_instance = None 
DOCKER_CONTAINER_NAME = "tgf"
CONFIG_PATH = "/app/config.yaml"
START_TIME = datetime.now(timezone.utc)

class InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

def setup_logging(app_level: str = "INFO", telethon_level: str = "WARNING"):
    logging.root.handlers = [InterceptHandler()]
    logging.root.setLevel(app_level)
    for _log in ['uvicorn', 'uvicorn.error', 'uvicorn.access', 'fastapi']:
        _logger = logging.getLogger(_log)
        _logger.handlers = [InterceptHandler()]
        _logger.propagate = False
    config = {
        "handlers": [
            {
                "sink": sys.stdout,
                "level": app_level,
                "format": "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
                "enqueue": True 
            }
        ]
    }
    logger.configure(**config)
    logging.getLogger('telethon').setLevel(telethon_level)
    logging.getLogger('hpack').setLevel(logging.WARNING) 
    logger.success(f"日志系统初始化完成 (App: {app_level}, Telethon: {telethon_level})")

def load_config(path):
    global DOCKER_CONTAINER_NAME
    logger.info(f"正在加载配置: {path}")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        if 'docker_container_name' in config_data:
            DOCKER_CONTAINER_NAME = config_data['docker_container_name']
        config_obj = Config(**config_data)
        logger.success("配置文件验证通过。")
        return config_obj
    except FileNotFoundError:
        logger.critical(f"配置文件 '{path}' 未找到。")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"配置文件解析失败: {e}")
        sys.exit(1)

async def initialize_clients(config: Config):
    global clients
    clients.clear()
    logger.info(f"正在初始化 {len(config.accounts)} 个用户账号...")
    for i, acc in enumerate(config.accounts):
        if not acc.enabled: continue
        try:
            session_path = f"/app/data/{acc.session_name}"
            session_exists = os.path.exists(f"{session_path}.session")
            client = TelegramClient(session_path, acc.api_id, acc.api_hash, proxy=config.proxy.get_telethon_proxy() if config.proxy else None)
            client.session_name_for_forwarder = acc.session_name
            if not session_exists: logger.warning(f"⚠️ 账号 {acc.session_name} 未登录。请在控制台交互式登录。")
            await client.start()
            if not await client.is_user_authorized():
                 logger.error(f"❌ 账号 {acc.session_name} 未授权。跳过。")
                 await client.disconnect()
                 continue
            me = await client.get_me()
            logger.success(f"✅ 账号 {i+1} 登录成功: {me.first_name} (@{me.username})")
            clients.append(client)
        except Exception as e:
            logger.error(f"❌ 账号 {acc.session_name} 启动失败: {e}。跳过。")
    if not clients: logger.warning("⚠️ 没有任何可用的用户账号！")

async def initialize_bot(config: Config):
    global bot_client, forwarder, link_checker, bot_service_instance
    if not config.bot_service or not config.bot_service.enabled: return
    if not config.bot_service.bot_token or config.bot_service.bot_token == "YOUR_BOT_TOKEN_HERE": return

    logger.info("正在启动 Bot 服务...")
    try:
        api_id = config.accounts[0].api_id
        api_hash = config.accounts[0].api_hash
        bot_client = TelegramClient(None, api_id, api_hash, proxy=config.proxy.get_telethon_proxy() if config.proxy else None)
        await bot_client.start(bot_token=config.bot_service.bot_token)
        me = await bot_client.get_me()
        logger.success(f"✅ Bot 登录成功: @{me.username}")

        if not link_checker and config.link_checker.enabled and clients:
             link_checker = LinkChecker(config, clients[0]) 

        # 传入 lambda: clients 以获取最新列表
        bot_service_instance = BotService(config, bot_client, forwarder, link_checker, reload_config_func, lambda: clients)
        await bot_service_instance.register_commands()
        
        # 将 bot service 注册到 web server，用于推送通知
        web_server.set_bot_notifier(bot_service_instance.notify_admin)

    except Exception as e:
        logger.error(f"❌ Bot 启动失败: {e}")
        bot_client = None

async def resolve_identifiers(client: TelegramClient, source_list: List[SourceConfig], config_desc: str) -> List[int]:
    resolved_ids = []
    if not client: return []
    logger.info(f"正在解析 {config_desc} 中的 {len(source_list)} 个源...")
    for s_config in source_list:
        identifier = s_config.identifier
        try:
            entity = await client.get_entity(identifier)
            resolved_id = entity.id
            
            # 获取标题
            title = getattr(entity, 'title', None)
            if not title and hasattr(entity, 'username'):
                title = entity.username

            if isinstance(entity, Channel) and not str(resolved_id).startswith("-100"): resolved_id = int(f"-100{resolved_id}")
            elif isinstance(entity, Chat) and not str(resolved_id).startswith("-"): resolved_id = int(f"-{resolved_id}")
            
            logger.debug(f"解析源: {identifier} -> {resolved_id}")
            s_config.resolved_id = resolved_id 
            
            # 缓存标题到 Web 数据库
            if title:
                 s_config.cached_title = title
                 
            resolved_ids.append(resolved_id)
        except Exception as e:
            logger.error(f"无法解析源 '{identifier}': {e}")
            
    # 保存解析结果（包含标题）
    await web_server.save_rules_to_db()
    return list(set(resolved_ids))

# --- 状态回调函数 (您提到的292行附近) ---
async def get_runtime_stats_func():
    global bot_client, clients, START_TIME
    
    # 计算中文运行时间
    uptime_delta = datetime.now(timezone.utc) - START_TIME
    days = uptime_delta.days
    seconds = uptime_delta.seconds
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    uptime_parts = []
    if days > 0: uptime_parts.append(f"{days}天")
    if hours > 0: uptime_parts.append(f"{hours}时")
    if minutes > 0: uptime_parts.append(f"{minutes}分")
    uptime_parts.append(f"{secs}秒")
    uptime_str = "".join(uptime_parts) if uptime_parts else "0秒"
    
    bot_status_text = "未启用"
    bot_connected = False
    if bot_client:
        try:
            if bot_client.is_connected():
                bot_connected = True
                bot_status_text = "已连接"
            else: bot_status_text = "断开连接"
        except: bot_status_text = "异常"

    return {
        "uptime": uptime_str,
        "bot_status": bot_status_text,
        "bot_connected": bot_connected, 
        "user_account_count": len(clients)
    }

async def run_forwarder(config: Config):
    global forwarder, link_checker
    
    await initialize_clients(config)
    await initialize_bot(config)
    
    if clients:
        main_client = clients[0]
        # 解析 config.yaml 中的源
        await resolve_identifiers(main_client, config.sources, "config.yaml") 
        
        # 加载并解析 rules_db.json 中的源
        await web_server.load_rules_from_db(config)
        await resolve_identifiers(main_client, web_server.rules_db.sources, "rules_db.json")

        forwarder = UltimateForwarder(config, clients)
        await forwarder.resolve_targets()
        
        if bot_service_instance:
            bot_service_instance.forwarder = forwarder

        @main_client.on(events.NewMessage())
        async def handle_new_message(event):
            if event.message.grouped_id: return 
            await forwarder.process_message(event)
            if forwarder.config.forwarding.mark_as_read: await event.mark_read()

        @main_client.on(events.Album())
        async def handle_album(event):
            main_message = next((m for m in event.messages if m.text), event.messages[0])
            main_event = events.NewMessage.Event(message=main_message)
            main_event.chat_id = main_message.chat_id
            main_event.chat = await event.get_chat()
            await forwarder.process_message(main_event, all_messages_in_group=event.messages)
            if forwarder.config.forwarding.mark_as_read: await main_event.mark_read()

        logger.success("转发核心就绪。")
        if not config.forwarding.forward_new_only: logger.info("开始历史扫描...") 
    else:
        await web_server.load_rules_from_db(config)
        logger.warning("无可用用户账号。")

    scheduler = AsyncIOScheduler(timezone="UTC")
    if config.link_checker and config.link_checker.enabled and clients:
        if not link_checker: link_checker = LinkChecker(config, clients[0])
        try:
            scheduler.add_job(link_checker.run, CronTrigger.from_crontab(config.link_checker.schedule), name="link_checker")
        except Exception: pass
    scheduler.start()

    web_server.set_stats_provider(get_runtime_stats_func)
    
    # 启动 Web 服务
    server = uvicorn.Server(uvicorn.Config(web_server.app, host="0.0.0.0", port=8080, log_config=None, access_log=False))
    logger.success("🚀 Web UI: http://localhost:8080")
    
    tasks = [server.serve()]
    if clients: tasks.append(clients[0].run_until_disconnected())
    if bot_client and bot_client.is_connected(): tasks.append(bot_client.run_until_disconnected())
    
    await asyncio.gather(*tasks)

async def run_link_checker(config: Config):
    await database.init_db()
    await initialize_clients(config)
    if clients: LinkChecker(config, clients[0]).run()

async def export_dialogs(config: Config):
    await initialize_clients(config)
    if clients:
        dialogs = await clients[0].get_dialogs()
        for d in dialogs:
            if d.is_channel or d.is_group: print(f"{d.id:<20} | {d.title}")

async def reload_config_func():
    global forwarder, link_checker
    try:
        new_config = load_config(CONFIG_PATH)
        await web_server.load_rules_from_db(new_config)
        if clients:
             await resolve_identifiers(clients[0], web_server.rules_db.sources, "rules_db.json")
             if forwarder: await forwarder.reload(new_config)
             if link_checker: link_checker.reload(new_config)
        return "配置热重载成功。"
    except Exception as e: return f"热重载失败: {e}"

async def main():
    global CONFIG_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['run', 'checklinks', 'export'], default='run', nargs='?')
    parser.add_argument('-c', '--config', default='/app/config.yaml')
    args = parser.parse_args()
    CONFIG_PATH = args.config
    config = load_config(CONFIG_PATH)
    setup_logging(config.logging_level.app, config.logging_level.telethon)
    if config.web_ui: web_server.set_web_ui_password(config.web_ui.password)

    try:
        if args.mode != 'export': await database.init_db()
        if args.mode == 'run': await run_forwarder(config)
        elif args.mode == 'checklinks': await run_link_checker(config)
        elif args.mode == 'export': await export_dialogs(config)
    except (KeyboardInterrupt, asyncio.CancelledError): pass
    finally:
        if database._db_conn: await database._db_conn.close()
        if bot_client and bot_client.is_connected(): await bot_client.disconnect()
        for c in clients:
            if c.is_connected(): await c.disconnect()

if __name__ == "__main__":
    if not os.path.exists("/app/data"): os.makedirs("/app/data", exist_ok=True)
    try: asyncio.run(main())
    except KeyboardInterrupt: pass