import sys
import os
import asyncio
import argparse
import yaml
import logging  # 仅用于拦截标准库日志
from typing import List, Dict

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

# --- 1. 现代化日志系统 (Loguru Integration) ---

class InterceptHandler(logging.Handler):
    """
    将标准库 logging 模块的日志拦截并重定向到 Loguru。
    这样 Telethon 和 Uvicorn 的日志也能统一格式。
    """
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # 查找调用者的栈帧，以便 Loguru 能正确显示日志来源
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

def setup_logging(app_level: str = "INFO", telethon_level: str = "WARNING"):
    """配置 Loguru 接管所有日志"""
    
    # 1. 移除标准库 root logger 的所有 handler (防止重复打印)
    logging.root.handlers = [InterceptHandler()]
    logging.root.setLevel(app_level)

    # 2. 移除 Uvicorn 和 FastAPI 默认的 handler
    for _log in ['uvicorn', 'uvicorn.error', 'fastapi']:
        _logger = logging.getLogger(_log)
        _logger.handlers = [InterceptHandler()]

    # 3. 配置 Loguru
    # format: 定义日志的颜色和结构
    # sink: 输出目标 (sys.stdout)
    # enqueue: 线程安全 (异步环境必需)
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
            },
            # (可选) 如果你想同时保存到文件，可以取消注释以下内容：
            # {
            #     "sink": "/app/data/app.log",
            #     "rotation": "10 MB",
            #     "retention": "7 days",
            #     "level": "INFO",
            #     "encoding": "utf-8"
            # }
        ]
    }
    logger.configure(**config)

    # 4. 单独设置第三方库的日志级别
    logging.getLogger('telethon').setLevel(telethon_level)
    # 屏蔽一些嘈杂的库
    logging.getLogger('hpack').setLevel(logging.WARNING) 
    
    logger.success(f"日志系统初始化完成 (App: {app_level}, Telethon: {telethon_level})")


# --- 2. 核心逻辑 ---

def load_config(path):
    """加载 YAML 配置文件"""
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
    """初始化所有 Telethon 用户客户端"""
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
            
            # 绑定一个自定义属性，方便后续识别
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
    """初始化 Bot 客户端"""
    global bot_client, forwarder, link_checker
    
    if not config.bot_service or not config.bot_service.enabled:
        return

    if not config.bot_service.bot_token or config.bot_service.bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.error("Bot 服务已启用但 Token 未配置，跳过。")
        return

    logger.info("正在启动 Bot 服务...")
    try:
        bot_client = TelegramClient(
            None, # Bot 使用内存会话
            config.accounts[0].api_id, 
            config.accounts[0].api_hash,
            proxy=config.proxy.get_telethon_proxy() if config.proxy else None
        )
        
        await bot_client.start(bot_token=config.bot_service.bot_token)
        me = await bot_client.get_me()
        logger.success(f"Bot 登录成功: @{me.username}")

        # 确保 LinkChecker 存在 (Bot 命令可能需要它)
        if not link_checker and config.link_checker.enabled:
             link_checker = LinkChecker(config, clients[0]) 

        bot_service = BotService(config, bot_client, forwarder, link_checker, reload_config_func)
        await bot_service.register_commands()

    except Exception as e:
        logger.error(f"Bot 启动失败: {e}")
        bot_client = None

async def resolve_identifiers(client: TelegramClient, source_list: List[SourceConfig], config_desc: str) -> List[int]:
    """将频道用户名/链接列表解析为数字 ID"""
    resolved_ids = []
    
    logger.info(f"正在解析 {config_desc} 中的 {len(source_list)} 个源...")
    for s_config in source_list:
        identifier = s_config.identifier
        try:
            entity = await client.get_entity(identifier)
            resolved_id = entity.id
            
            # 标准化 ID 格式
            if isinstance(entity, Channel) and not str(resolved_id).startswith("-100"):
                resolved_id = int(f"-100{resolved_id}")
            elif isinstance(entity, Chat) and not str(resolved_id).startswith("-"):
                resolved_id = int(f"-{resolved_id}")
            
            # Loguru 不需要 f-string 拼接太多，直接传参也可以，这里保持 f-string
            logger.debug(f"解析源: {identifier} -> {resolved_id}")
            s_config.resolved_id = resolved_id 
            resolved_ids.append(resolved_id)
                
        except Exception as e:
            logger.error(f"无法解析源 '{identifier}' ({config_desc}): {e}")
    
    return list(set(resolved_ids))

# --- 3. 业务逻辑 ---

async def run_forwarder(config: Config):
    """主运行逻辑"""
    global forwarder, link_checker
    
    # 1. 登录客户端
    await initialize_clients(config)
    main_client = clients[0] 
    
    # 2. 加载并解析规则
    # 从 config.yaml 解析 (兼容旧版)
    resolved_source_ids = await resolve_identifiers(main_client, config.sources, "config.yaml") 
    
    # 从 Web UI 数据库加载并解析
    await web_server.load_rules_from_db(config)
    await resolve_identifiers(main_client, web_server.rules_db.sources, "rules_db.json")

    # 3. 初始化转发核心
    forwarder = UltimateForwarder(config, clients)
    await forwarder.resolve_targets()
    
    # 4. 注册事件监听
    @main_client.on(events.NewMessage())
    async def handle_new_message(event):
        if event.message.grouped_id: return # 相册消息交给 Album 处理
        await forwarder.process_message(event)
        if forwarder.config.forwarding.mark_as_read:
            await event.mark_read()

    @main_client.on(events.Album())
    async def handle_album(event):
        # 获取相册中第一条带文字的消息作为主消息，或者默认第一条
        main_message = next((m for m in event.messages if m.text), event.messages[0])
        # 构建一个伪造的 NewMessage 事件
        main_event = events.NewMessage.Event(message=main_message)
        main_event.chat_id = main_message.chat_id
        main_event.chat = await event.get_chat()
        
        await forwarder.process_message(main_event, all_messages_in_group=event.messages)
        
        if forwarder.config.forwarding.mark_as_read:
            await main_event.mark_read()

    logger.success("事件监听器注册完毕。")

    # 5. 启动 Bot
    await initialize_bot(config)

    # 6. 启动定时任务 (Scheduler)
    scheduler = AsyncIOScheduler(timezone="UTC")
    
    # 链接检测任务
    if config.link_checker and config.link_checker.enabled:
        if not link_checker: 
             link_checker = LinkChecker(config, main_client)
        try:
            trigger = CronTrigger.from_crontab(config.link_checker.schedule)
            scheduler.add_job(link_checker.run, trigger, name="link_checker")
            logger.info(f"LinkChecker 定时任务已添加: {config.link_checker.schedule} UTC")
        except ValueError as e:
            logger.error(f"LinkChecker Cron 表达式错误: {e}")
            
    # 数据库清理任务 (每天 4:05 UTC)
    # 注意: 需要在 database.py 中实现 prune_old_hashes 
    # scheduler.add_job(database.prune_old_hashes, CronTrigger.from_crontab("5 4 * * *")) 
        
    scheduler.start()

    # 7. 历史消息处理
    if not config.forwarding.forward_new_only:
        logger.info("开始扫描历史消息 (forward_new_only=False)...")
        await forwarder.process_history(resolved_source_ids)
        logger.success("历史消息扫描完成。")
    else:
        logger.info("仅处理新消息，跳过历史扫描。")

    # 8. 启动 Web Server
    # 禁用 Uvicorn 默认日志配置，让 InterceptHandler 接管
    uvicorn_config = uvicorn.Config(web_server.app, host="0.0.0.0", port=8080, log_config=None)
    server = uvicorn.Server(uvicorn_config)
    
    logger.success("🚀 系统启动完成，正在运行...")
    logger.info(f"Web 面板地址: http://localhost:8080")
    
    # 9. 保持运行
    tasks = [
        main_client.run_until_disconnected(),
        server.serve()
    ]
    if bot_client:
        tasks.append(bot_client.run_until_disconnected())

    await asyncio.gather(*tasks)

async def run_link_checker(config: Config):
    """独立运行链接检测模式"""
    global link_checker
    if not config.link_checker or not config.link_checker.enabled:
        logger.error("LinkChecker 未启用。")
        return
        
    await database.init_db()
    await initialize_clients(config)
    link_checker = LinkChecker(config, clients[0])
    await link_checker.run()

async def export_dialogs(config: Config):
    """工具：导出频道 ID"""
    await initialize_clients(config)
    client = clients[0]

    logger.info("正在获取对话列表...")
    dialogs = await client.get_dialogs()
    
    print("\n" + "="*40)
    print(f"{'ID':<20} | {'Name'}")
    print("-" * 40)
    for d in dialogs:
        if d.is_channel or d.is_group:
            print(f"{d.id:<20} | {d.title}")
    print("="*40 + "\n")

async def reload_config_func():
    """热重载回调函数"""
    global forwarder, link_checker
    logger.warning("🔄 正在执行热重载...")
    
    try:
        new_config = load_config(CONFIG_PATH)
        
        # 重新配置日志
        setup_logging(new_config.logging_level.app, new_config.logging_level.telethon)
        
        # 重载 Web 规则
        await web_server.load_rules_from_db(new_config)
        await resolve_identifiers(clients[0], web_server.rules_db.sources, "rules_db.json")
        
        # 重载核心组件
        if forwarder:
            await forwarder.reload(new_config)
        if link_checker:
            link_checker.reload(new_config)
            
        logger.success("✅ 热重载成功！")
        return "配置热重载成功。"
    except Exception as e:
        logger.exception("热重载失败")
        return f"热重载失败: {e}"

async def main():
    global CONFIG_PATH
    parser = argparse.ArgumentParser(description="TG Ultimate Forwarder Pro")
    parser.add_argument('mode', choices=['run', 'checklinks', 'export'], default='run', nargs='?')
    parser.add_argument('-c', '--config', default='/app/config.yaml')
    args = parser.parse_args()
    CONFIG_PATH = args.config

    # 初始加载配置
    config = load_config(CONFIG_PATH)
    
    # 初始化日志系统
    setup_logging(config.logging_level.app, config.logging_level.telethon)

    # Web UI 密码检查
    if config.web_ui and config.web_ui.password != "default_password_please_change":
        web_server.set_web_ui_password(config.web_ui.password)
    else:
        logger.warning("⚠️ Web UI 使用了默认密码！请立即在 config.yaml 中修改。")
        web_server.set_web_ui_password("default_password_please_change")

    try:
        # 运行模式选择
        if args.mode in ['run', 'checklinks']:
            await database.init_db()
            
        if args.mode == 'run':
            await run_forwarder(config)
        elif args.mode == 'checklinks':
            await run_link_checker(config)
        elif args.mode == 'export':
            await export_dialogs(config)
            
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("程序被用户停止。")
    except Exception as e:
        logger.exception("发生未捕获的致命错误")
    finally:
        # 清理资源
        if database._db_conn:
             await database._db_conn.close()
        if bot_client and bot_client.is_connected():
            await bot_client.disconnect()
        for c in clients:
            if c.is_connected():
                await c.disconnect()

if __name__ == "__main__":
    # 确保数据目录存在
    if not os.path.exists("/app/data"):
        os.makedirs("/app/data", exist_ok=True)
            
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass # 避免在最后退出时打印 Traceback