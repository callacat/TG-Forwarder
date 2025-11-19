import sys
import os
import asyncio
import argparse
import yaml
import logging  # 仅用于拦截标准库日志
from typing import List, Dict
from datetime import datetime, timezone

# (新) 现代化日志库
from loguru import logger

from telethon import TelegramClient, events, errors
from telethon.tl.types import Channel, Chat

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# 导入项目模块
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
DOCKER_CONTAINER_NAME = "tgf"
CONFIG_PATH = "/app/config.yaml"
START_TIME = datetime.now(timezone.utc) # 记录启动时间

# --- 1. 现代化日志系统 (Loguru Integration) ---

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

    for _log in ['uvicorn', 'uvicorn.error', 'fastapi']:
        _logger = logging.getLogger(_log)
        _logger.handlers = [InterceptHandler()]

    config = {
        "handlers": [
            {
                "sink": sys.stdout,
                "level": app_level,
                "format": "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                          "<level>{level: <8}</level> | "
                          "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                          "<level>{message}</level>",
                "enqueue": True 
            }
        ]
    }
    logger.configure(**config)
    logging.getLogger('telethon').setLevel(telethon_level)
    logging.getLogger('hpack').setLevel(logging.WARNING) 
    logger.success(f"日志系统初始化完成 (App: {app_level}, Telethon: {telethon_level})")


# --- 2. 核心逻辑 ---

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
        if not acc.enabled:
            logger.warning(f"账号 {i+1} ({acc.session_name}) 已禁用，跳过。")
            continue
        
        try:
            session_path = f"/app/data/{acc.session_name}"
            session_exists = os.path.exists(f"{session_path}.session")

            client = TelegramClient(
                session_path, 
                acc.api_id,
                acc.api_hash,
                proxy=config.proxy.get_telethon_proxy() if config.proxy else None
            )
            client.session_name_for_forwarder = acc.session_name
            
            if not session_exists:
                logger.warning(f"⚠️ 账号 {acc.session_name} 未登录。")
                logger.warning(">>> 请在终端 (docker attach) 输入手机号和验证码 <<<")
            
            await client.start()
            me = await client.get_me()
            logger.success(f"账号 {i+1} 登录成功: {me.first_name} (@{me.username})")
            clients.append(client)
        except errors.SessionPasswordNeededError:
            logger.error(f"账号 {acc.session_name} 需要两步验证密码。请在控制台输入。")
        except Exception as e:
            logger.error(f"账号 {acc.session_name} 启动失败: {e}")
    
    if not clients:
        logger.critical("没有可用的用户账号，程序退出。")
        sys.exit(1)

async def initialize_bot(config: Config):
    global bot_client, forwarder, link_checker
    
    if not config.bot_service or not config.bot_service.enabled:
        return

    if not config.bot_service.bot_token or config.bot_service.bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.error("Bot 服务已启用但 Token 未配置，跳过。")
        return

    logger.info("正在启动 Bot 服务...")
    try:
        bot_client = TelegramClient(
            None, 
            config.accounts[0].api_id, 
            config.accounts[0].api_hash,
            proxy=config.proxy.get_telethon_proxy() if config.proxy else None
        )
        await bot_client.start(bot_token=config.bot_service.bot_token)
        me = await bot_client.get_me()
        logger.success(f"Bot 登录成功: @{me.username}")

        if not link_checker and config.link_checker.enabled:
             link_checker = LinkChecker(config, clients[0]) 

        bot_service = BotService(config, bot_client, forwarder, link_checker, reload_config_func)
        await bot_service.register_commands()

    except Exception as e:
        logger.error(f"Bot 启动失败: {e}")
        bot_client = None

async def resolve_identifiers(client: TelegramClient, source_list: List[SourceConfig], config_desc: str) -> List[int]:
    resolved_ids = []
    logger.info(f"正在解析 {config_desc} 中的 {len(source_list)} 个源...")
    for s_config in source_list:
        identifier = s_config.identifier
        try:
            entity = await client.get_entity(identifier)
            resolved_id = entity.id
            if isinstance(entity, Channel) and not str(resolved_id).startswith("-100"):
                resolved_id = int(f"-100{resolved_id}")
            elif isinstance(entity, Chat) and not str(resolved_id).startswith("-"):
                resolved_id = int(f"-{resolved_id}")
            
            logger.debug(f"解析源: {identifier} -> {resolved_id}")
            s_config.resolved_id = resolved_id 
            resolved_ids.append(resolved_id)
        except Exception as e:
            logger.error(f"无法解析源 '{identifier}' ({config_desc}): {e}")
    return list(set(resolved_ids))

# --- 3. 业务逻辑 ---

# (新) 状态提供函数 - 优化时间格式
async def get_runtime_stats_func():
    """提供给 web_server 的回调，用于获取实时状态"""
    global bot_client, clients, START_TIME
    
    # 计算运行时间
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
    
    bot_connected = False
    bot_status_text = "未启用"
    if bot_client:
        try:
            if bot_client.is_connected():
                bot_connected = True
                bot_status_text = "已连接"
            else:
                bot_status_text = "断开连接"
        except:
            bot_status_text = "异常"

    return {
        "uptime": uptime_str,
        "bot_status": bot_status_text,
        "bot_connected": bot_connected, 
        "user_account_count": len(clients)
    }

async def run_forwarder(config: Config):
    global forwarder, link_checker
    
    await initialize_clients(config)
    main_client = clients[0] 
    
    await resolve_identifiers(main_client, config.sources, "config.yaml") 
    await web_server.load_rules_from_db(config)
    await resolve_identifiers(main_client, web_server.rules_db.sources, "rules_db.json")

    forwarder = UltimateForwarder(config, clients)
    await forwarder.resolve_targets()
    
    @main_client.on(events.NewMessage())
    async def handle_new_message(event):
        if event.message.grouped_id: return 
        await forwarder.process_message(event)
        if forwarder.config.forwarding.mark_as_read:
            await event.mark_read()

    @main_client.on(events.Album())
    async def handle_album(event):
        main_message = next((m for m in event.messages if m.text), event.messages[0])
        main_event = events.NewMessage.Event(message=main_message)
        main_event.chat_id = main_message.chat_id
        main_event.chat = await event.get_chat()
        await forwarder.process_message(main_event, all_messages_in_group=event.messages)
        if forwarder.config.forwarding.mark_as_read:
            await main_event.mark_read()

    logger.success("事件监听器注册完毕。")
    await initialize_bot(config)

    scheduler = AsyncIOScheduler(timezone="UTC")
    if config.link_checker and config.link_checker.enabled:
        if not link_checker: link_checker = LinkChecker(config, main_client)
        try:
            trigger = CronTrigger.from_crontab(config.link_checker.schedule)
            scheduler.add_job(link_checker.run, trigger, name="link_checker")
            logger.info(f"LinkChecker 定时任务: {config.link_checker.schedule} UTC")
        except ValueError as e:
            logger.error(f"LinkChecker Cron 错误: {e}")
    scheduler.start()

    if not config.forwarding.forward_new_only:
        logger.info("开始扫描历史消息...")
        # await forwarder.process_history(resolved_source_ids) 
        pass
    else:
        logger.info("跳过历史扫描。")

    # 注册状态提供者
    web_server.set_stats_provider(get_runtime_stats_func)

    uvicorn_config = uvicorn.Config(web_server.app, host="0.0.0.0", port=8080, log_config=None)
    server = uvicorn.Server(uvicorn_config)
    
    logger.success("🚀 系统启动完成，Web UI: http://localhost:8080")
    
    tasks = [main_client.run_until_disconnected(), server.serve()]
    if bot_client: tasks.append(bot_client.run_until_disconnected())
    await asyncio.gather(*tasks)

async def run_link_checker(config: Config):
    global link_checker
    if not config.link_checker or not config.link_checker.enabled: return
    await database.init_db()
    await initialize_clients(config)
    link_checker = LinkChecker(config, clients[0])
    await link_checker.run()

async def export_dialogs(config: Config):
    await initialize_clients(config)
    client = clients[0]
    dialogs = await client.get_dialogs()
    print("\n" + "="*40)
    print(f"{'ID':<20} | {'Name'}")
    print("-" * 40)
    for d in dialogs:
        if d.is_channel or d.is_group: print(f"{d.id:<20} | {d.title}")
    print("="*40 + "\n")

async def reload_config_func():
    global forwarder, link_checker
    logger.warning("🔄 正在执行热重载...")
    try:
        new_config = load_config(CONFIG_PATH)
        setup_logging(new_config.logging_level.app, new_config.logging_level.telethon)
        await web_server.load_rules_from_db(new_config)
        await resolve_identifiers(clients[0], web_server.rules_db.sources, "rules_db.json")
        if forwarder: await forwarder.reload(new_config)
        if link_checker: link_checker.reload(new_config)
        logger.success("✅ 热重载成功！")
        return "配置热重载成功。"
    except Exception as e:
        logger.exception("热重载失败")
        return f"热重载失败: {e}"

async def main():
    global CONFIG_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['run', 'checklinks', 'export'], default='run', nargs='?')
    parser.add_argument('-c', '--config', default='/app/config.yaml')
    args = parser.parse_args()
    CONFIG_PATH = args.config

    config = load_config(CONFIG_PATH)
    setup_logging(config.logging_level.app, config.logging_level.telethon)

    if config.web_ui and config.web_ui.password != "default_password_please_change":
        web_server.set_web_ui_password(config.web_ui.password)
    else:
        web_server.set_web_ui_password("default_password_please_change")

    try:
        if args.mode in ['run', 'checklinks']: await database.init_db()
        if args.mode == 'run': await run_forwarder(config)
        elif args.mode == 'checklinks': await run_link_checker(config)
        elif args.mode == 'export': await export_dialogs(config)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("程序被用户停止。")
    except Exception as e:
        logger.exception("发生未捕获的致命错误")
    finally:
        if database._db_conn: await database._db_conn.close()
        if bot_client and bot_client.is_connected(): await bot_client.disconnect()
        for c in clients:
            if c.is_connected(): await c.disconnect()

if __name__ == "__main__":
    if not os.path.exists("/app/data"): os.makedirs("/app/data", exist_ok=True)
    try: asyncio.run(main())
    except KeyboardInterrupt: pass