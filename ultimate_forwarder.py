import logging
import argparse
import yaml
import sys
import os
import asyncio 
from telethon import TelegramClient, events, errors
from telethon.tl.types import PeerUser, PeerChat, PeerChannel, Message
from telethon.tl.types import Channel, Chat 
from typing import List, Dict 

# (新) v8.0：导入 uvicorn
import uvicorn

# (新) v9.0：导入 database
import database

# (新) 导入定时任务
from apscheduler.schedulers.asyncio import AsyncIOScheduler 
from apscheduler.triggers.cron import CronTrigger

# 假设 forwarder_core 和 link_checker 在同一目录下
from forwarder_core import UltimateForwarder, Config, AccountConfig
from link_checker import LinkChecker
from bot_service import BotService 
# (新) v8.0：导入 web_server
import web_server

# --- (新) v5.9：日志配置现在由 main() 中的 config 驱动 ---
logging.basicConfig(
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    level="INFO", # 临时级别
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger('telethon').setLevel(logging.WARNING) 


# --- 全局变量 ---
clients = [] 
bot_client = None 
forwarder = None 
link_checker = None 
DOCKER_CONTAINER_NAME = "tgf" 
CONFIG_PATH = "/app/config.yaml" 

def setup_logging(app_level: str = "INFO", telethon_level: str = "WARNING"):
    """(新) v5.9：根据配置设置日志级别"""
    app_level = app_level.upper()
    telethon_level = telethon_level.upper()
    
    logging.basicConfig(
        format='%(asctime)s - [%(levelname)s] - %(message)s',
        level=app_level, 
        handlers=[
            logging.StreamHandler(sys.stdout)
        ],
        force=True 
    )
    
    logging.getLogger('telethon').setLevel(telethon_level)
    
    global logger
    logger = logging.getLogger(__name__)
    
    logger.info(f"程序日志级别已设置为: {app_level}")
    logger.info(f"Telethon 日志级别已设置为: {telethon_level}")
    if telethon_level == "INFO" or telethon_level == "DEBUG":
         logger.warning("Telethon 日志级别设置为 INFO/DEBUG，可能会导致大量刷屏。")

def load_config(path):
    """加载 YAML 配置文件"""
    global DOCKER_CONTAINER_NAME
    
    logger.info(f"正在从 {path} 加载配置...")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
            
        if 'docker_container_name' in config_data:
            DOCKER_CONTAINER_NAME = config_data['docker_container_name']
            
        config_obj = Config(**config_data)
        logger.info("✅ 配置文件加载并验证成功。")
        return config_obj
        
    except FileNotFoundError:
        logger.critical(f"❌ 致命错误: 配置文件 '{path}' 未找到。")
        logger.critical("---")
        logger.critical("如果你是第一次运行，请：")
        logger.critical("1. 将 'config_template.yaml' 复制为 'config.yaml'。")
        logger.critical("2. 填写 'config.yaml' 中的 API 密钥和频道 ID。")
        logger.critical("3. (如果你使用 Docker) 确保你使用了 '-v' 来挂载配置文件:")
        logger.critical(f"   docker run ... -v /path/to/your/config.yaml:{path} ...")
        logger.critical("---")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"❌ 致命错误: 加载或解析配置文件 {path} 失败: {e}")
        sys.exit(1)

async def initialize_clients(config: Config):
    """初始化所有 Telethon 用户客户端"""
    global clients
    clients.clear() 
    logger.info(f"正在初始化 {len(config.accounts)} 个用户账号...")
    
    for i, acc in enumerate(config.accounts):
        if not acc.enabled:
            logger.warning(f"账号 {i+1} (Session: {acc.session_name}) 已被禁用，跳过。")
            continue
        
        try:
            logger.info(f"账号 {i+1} 正在使用会话文件: {acc.session_name}...")
            session_path = f"/app/data/{acc.session_name}"
            
            session_file_exists = os.path.exists(f"{session_path}.session")

            client = TelegramClient(
                session_path, 
                acc.api_id,
                acc.api_hash,
                proxy=config.proxy.get_telethon_proxy() if config.proxy else None
            )
            
            client.session_name_for_forwarder = acc.session_name
            
            if not session_file_exists:
                logger.warning(f"账号 {acc.session_name} 未登录 (未找到 .session 文件)。")
                logger.warning("---")
                logger.warning("程序将等待你输入手机号、验证码和两步验证密码。")
                logger.warning("!!! (重要) 如果你使用 DOCKER, 你必须现在打开 *另一个* 终端并运行: !!!")
                logger.warning(f"    docker attach {DOCKER_CONTAINER_NAME}")
                logger.warning("---")
            else:
                logger.info(f"检测到账号 {acc.session_name} 的会话文件，尝试自动登录...")

            
            await client.start()
            
            me = await client.get_me()
            logger.info(f"✅ 账号 {i+1} ({me.first_name} / @{me.username}) 登录成功。")
            clients.append(client)
            
        except errors.SessionPasswordNeededError:
            logger.error(f"❌ 账号 {acc.session_name} 需要两步验证密码 (Two-Step Verification)。")
            logger.warning(f"请在控制台 (docker attach {DOCKER_CONTAINER_NAME}) 中输入你的密码。")
        except errors.AuthKeyUnregisteredError:
             logger.error(f"❌ 账号 {acc.session_name} 的 Session 已失效，请删除 data 目录下的 {acc.session_name}.session 文件后重试。")
        except Exception as e:
            logger.error(f"❌ 账号 {acc.session_name} 启动失败: {e}")
    
    if not clients:
        logger.critical("❌ 致命错误: 没有可用的账号。请检查配置或 Session 文件。")
        sys.exit(1)
    
    logger.info(f"✅ 成功启动 {len(clients)} 个用户客户端。")

async def initialize_bot(config: Config):
    """初始化 Bot 客户端"""
    global bot_client, forwarder, link_checker
    
    if not config.bot_service or not config.bot_service.enabled:
        logger.info("Bot 服务未在配置中启用，跳过。")
        return

    if not config.bot_service.bot_token:
        logger.error("Bot 服务已启用，但 bot_token 未提供，跳过。")
        return

    logger.info("正在初始化 Bot 客户端...")
    try:
        # Bot 使用内存会话
        bot_client = TelegramClient(
            None, 
            config.accounts[0].api_id, 
            config.accounts[0].api_hash,
            proxy=config.proxy.get_telethon_proxy() if config.proxy else None
        )
        
        await bot_client.start(bot_token=config.bot_service.bot_token)
        me = await bot_client.get_me()
        logger.info(f"✅ Bot (@{me.username}) 登录成功。")

        if not link_checker and config.link_checker.enabled:
             link_checker = LinkChecker(config, clients[0]) 

        bot_service = BotService(config, bot_client, forwarder, link_checker, reload_config_func)
        await bot_service.register_commands()
        logger.info("✅ Bot 命令已注册。")

    except Exception as e:
        logger.error(f"❌ Bot 客户端启动失败: {e}")
        bot_client = None


async def resolve_identifiers(client: TelegramClient, config: Config) -> List[int]:
    """将频道用户名/链接列表解析为数字 ID 列表"""
    resolved_ids = []
    
    logger.info("正在解析所有源频道/群组...")
    for s_config in config.sources:
        identifier = s_config.identifier
        try:
            entity = await client.get_entity(identifier)
            
            resolved_id = entity.id
            
            if isinstance(entity, Channel):
                if not str(resolved_id).startswith("-100"):
                    resolved_id = int(f"-100{resolved_id}")
            elif isinstance(entity, Chat):
                 if not str(resolved_id).startswith("-"):
                    resolved_id = int(f"-{resolved_id}")
            
            logger.info(f"源 '{identifier}' -> 解析为 ID: {resolved_id}")
            s_config.resolved_id = resolved_id 
            resolved_ids.append(resolved_id)
                
        except ValueError:
            logger.error(f"❌ 无法解析源: '{identifier}'。它似乎不是一个有效的频道/群组/用户。")
        except errors.ChannelPrivateError:
            logger.error(f"❌ 无法访问源: '{identifier}'。你的账号未加入该私有频道。")
        except Exception as e:
            logger.error(f"❌ 解析源 '{identifier}' 时出错: {e}")
    
    return list(set(resolved_ids))


async def run_forwarder(config: Config):
    """运行转发器主逻辑"""
    global forwarder, link_checker
    
    await initialize_clients(config)
    
    main_client = clients[0] 
    
    resolved_source_ids = await resolve_identifiers(main_client, config) 
    
    if not resolved_source_ids:
        logger.critical("❌ 无法解析任何源频道，请检查配置或确保账号已加入。")
        return
        
    logger.info(f"✅ 成功解析 {len(resolved_source_ids)} 个源。")
    
    forwarder = UltimateForwarder(config, clients)
    
    await forwarder.resolve_targets()
    
    # 1. 注册新消息处理器 (用于非相册消息)
    logger.info("注册新消息 (NewMessage) 事件处理器...")
    @main_client.on(events.NewMessage(chats=resolved_source_ids))
    async def handle_new_message(event):
        
        if event.message.grouped_id:
            return
            
        await forwarder.process_message(event)
        
        if forwarder.config.forwarding.mark_as_read:
            try:
                await event.mark_read() 
            except Exception as e:
                logger.debug(f"将 {event.chat_id} 标记为已读失败: {e}")
        
    logger.info("✅ NewMessage 事件处理器已注册。")

    # 2. 注册相册 (Album) 处理器
    logger.info("注册相册 (Album) 事件处理器...")
    @main_client.on(events.Album(chats=resolved_source_ids))
    async def handle_album(event):
        
        logger.info(f"处理相册 {event.grouped_id} (共 {len(event.messages)} 条消息)...")
        
        main_message = next((m for m in event.messages if m.text), event.messages[0])
        
        main_event = events.NewMessage.Event(message=main_message)
        main_event.chat_id = main_message.chat_id
        main_event.chat = await event.get_chat()

        all_messages = event.messages
        
        await forwarder.process_message(main_event, all_messages_in_group=all_messages)
        
        if forwarder.config.forwarding.mark_as_read:
            try:
                await main_event.mark_read()
            except Exception as e:
                logger.debug(f"将相册 {event.grouped_id} 标记为已读失败: {e}")

    logger.info("✅ Album 事件处理器已注册。")

    # 3. 启动 Bot 服务
    logger.info("正在启动 Bot 服务...")
    await initialize_bot(config)

    # 4. 启动定时任务 (Link Checker & v9.0 DB Prune)
    if config.link_checker and config.link_checker.enabled:
        if not link_checker: 
             link_checker = LinkChecker(config, main_client)
        
        try:
            scheduler = AsyncIOScheduler(timezone="UTC")
            # 任务 1: 链接检测
            trigger = CronTrigger.from_crontab(config.link_checker.schedule)
            scheduler.add_job(link_checker.run, trigger, name="run_link_checker_job")
            logger.info(f"✅ 链接检测器定时任务已启动 (Cron: {config.link_checker.schedule} UTC)。")

            # (新) v9.0：任务 2: 数据库清理
            # 每天凌晨 4:05 运行
            prune_trigger = CronTrigger.from_crontab("5 4 * * *")
            scheduler.add_job(database.prune_old_hashes, prune_trigger, name="prune_db_job", args=[30])
            logger.info(f"✅ 数据库清理定时任务已启动 (Cron: 5 4 * * *)。")
            
            scheduler.start()
            
        except ValueError as e:
            logger.warning(f"⚠️ 链接检测器 cron 表达式 '{config.link_checker.schedule}' 无效，定时任务未启动: {e}")
        except Exception as e_v4:
            logger.error(f"❌ 链接检测器启动失败: {e_v4}")


    # 5. (可选) 处理历史消息
    if not config.forwarding.forward_new_only:
        logger.info("配置了 `forward_new_only: false`，开始扫描历史消息 (这可能需要一些时间)...")
        await forwarder.process_history(resolved_source_ids)
        logger.info("✅ 历史消息扫描完成。")
    else:
        logger.info("`forward_new_only: true`，跳过历史消息扫描。")

    # (新) v8.0：准备 Web 服务器任务
    uvicorn_config = uvicorn.Config(web_server.app, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(uvicorn_config)
    
    # (新) v8.0：从 rules_db.json 加载规则
    await web_server.load_rules_from_db()

    # 6. 运行并等待
    logger.info(f"🚀 终极转发器已启动。正在监听 {len(resolved_source_ids)} 个源。")
    logger.info(f"🚀 Web UI (v8.0) 正在 http://0.0.0.0:8080 上启动。")
    
    tasks_to_run = [
        main_client.run_until_disconnected(),
        server.serve() # (新) v8.0：运行 Web 服务器
    ]
    
    if bot_client:
        tasks_to_run.append(bot_client.run_until_disconnected())

    await asyncio.gather(*tasks_to_run)

async def run_link_checker(config: Config):
    """运行失效链接检测器"""
    global link_checker
    
    if not config.link_checker or not config.link_checker.enabled:
        logger.warning("LinkChecker 未在 config.yaml 中启用，退出。")
        return
        
    # (新) v9.0：运行任务前必须初始化数据库
    await database.init_db()

    logger.info("启动失效链接检测器...")
    await initialize_clients(config) 
    
    link_checker = LinkChecker(config, clients[0])
    await link_checker.run()
    logger.info("✅ 失效链接检测完成。")

async def export_dialogs(config: Config):
    """导出频道和话题信息"""
    await initialize_clients(config)
    main_client = clients[0]

    logger.info("正在导出所有对话... (这可能需要一点时间)")
    
    try:
        dialogs = await main_client.get_dialogs()
        output = "--- 频道/群组/用户列表 (标识符 / 名称) ---\n"
        output += "--- (可直接复制 标识符 到 config.yaml) ---\n"
        topics_output = "\n--- 群组话题列表 (群组ID / 话题ID / 话题名称) ---\n"

        for dialog in dialogs:
            identifier = ""
            if dialog.is_channel or dialog.is_group:
                if dialog.entity.username:
                    identifier = f"@{dialog.entity.username}"
                else:
                    if dialog.is_channel:
                         identifier = str(dialog.id) if str(dialog.id).startswith("-100") else str(f"-100{dialog.id}")
                    else: # is_group
                         identifier = str(dialog.id) if str(dialog.id).startswith("-") else str(f"-{dialog.id}")

                output += f"{identifier}\t{dialog.title}\n"
                
                if dialog.is_group and getattr(dialog.entity, 'forum', False):
                    logger.info(f"正在获取群组 '{dialog.title}' ({identifier}) 的话题...")
                    try:
                        topics = await main_client.get_topics(dialog.id)
                        for topic in topics:
                            topics_output += f"{identifier}\t{topic.id}\t{topic.title}\n"
                    except Exception as e:
                        logger.warning(f"获取话题失败 for {dialog.title}: {e} (可能是权限不足)")

            elif dialog.is_user:
                if dialog.entity.username:
                    identifier = f"@{dialog.entity.username}"
                else:
                    identifier = str(dialog.id)
                output += f"{identifier}\t{dialog.title}\n"


        print("\n\n" + "="*30)
        print(output)
        print(topics_output)
        print("="*30 + "\n")
        
        logger.info("---")
        logger.info("如何使用:")
        logger.info("1. 在 'sources' 配置中，复制 '标识符' 列 (例如 @username 或 -100123456789)。")
        logger.info("2. 在 'targets' 配置中，也使用 '标识符'。")
        logger.info("3. 在 'targets.distribution_rules' 中，使用 '群组ID' 和 '话题ID'。")
        
    except Exception as e:
        logger.error(f"导出对话失败: {e}")

async def reload_config_func():
    """Bot 调用的热重载函数"""
    global forwarder, link_checker, bot_client, CONFIG_PATH, clients
    
    logger.warning("🔄 收到 /reload 命令，正在热重载配置...")
    
    try:
        new_config = load_config(CONFIG_PATH)
        
        if new_config.logging_level:
            setup_logging(new_config.logging_level.app, new_config.logging_level.telethon)
        
        # (新) v8.0：同时重载 Web UI 的规则
        await web_server.load_rules_from_db()
        
        # (旧)
        await resolve_identifiers(clients[0], new_config)

        if forwarder:
            await forwarder.reload(new_config) 

        if link_checker:
            link_checker.reload(new_config)
            logger.info("✅ 链接检测器配置已热重载。")
        
        return "✅ 配置热重载完毕。"
    except Exception as e:
        logger.error(f"❌ 热重载失败: {e}")
        return f"❌ 热重载失败: {e}"


async def main():
    global CONFIG_PATH
    parser = argparse.ArgumentParser(description="TG Ultimate Forwarder - 终极 Telegram 转发器")
    parser.add_argument(
        'mode',
        choices=['run', 'checklinks', 'export'],
        default='run',
        nargs='?', 
        help=(
            "运行模式: \n"
            "  'run' (默认): 启动转发器、Bot 和 Web UI。\n"
            "  'checklinks': 仅运行一次失效链接检测器。\n"
            "  'export': 导出频道和话题ID。"
        )
    )
    parser.add_argument(
        '-c', '--config',
        default='/app/config.yaml', 
        help="配置文件路径 (默认: /app/config.yaml)"
    )
    args = parser.parse_args()
    CONFIG_PATH = args.config 

    config = load_config(CONFIG_PATH)

    if config.logging_level:
        setup_logging(config.logging_level.app, config.logging_level.telethon)
    else:
        setup_logging() # 使用默认值 (INFO, WARNING)

    try:
        # (新) v9.0：在任何操作之前初始化数据库
        if args.mode in ['run', 'checklinks']:
            await database.init_db()
            
        if args.mode == 'run':
            await run_forwarder(config)
        elif args.mode == 'checklinks':
            await run_link_checker(config)
        elif args.mode == 'export':
            await export_dialogs(config)
            
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("程序被用户中断。")
    except Exception as e:
        logger.critical(f"❌ 出现未捕获的致命错误: {e}", exc_info=True)
    finally:
        # (新) v9.0：安全关闭数据库连接
        if database._db_conn:
             await database._db_conn.close()
             logger.info("数据库连接已关闭。")
             
        if bot_client and bot_client.is_connected():
            await bot_client.disconnect()
            logger.info("Bot 客户端已断开连接。")
        for client in clients:
            if client.is_connected():
                await client.disconnect()
        logger.info("所有用户客户端已断开连接。程序退出。")

if __name__ == "__main__":
    if not os.path.exists("/app/data"):
        logger.info("未检测到 /app/data 目录，正在创建...")
        try:
            os.makedirs("/app/data")
        except OSError as e:
            logger.critical(f"无法创建 /app/data 目录: {e}")
            logger.critical("请确保你已使用 -v /path/to/your/data:/app/data 挂载了数据卷。")
            sys.exit(1)
            
    asyncio.run(main())