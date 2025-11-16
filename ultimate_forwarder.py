import logging
import argparse
import yaml
import sys
import os
import asyncio # <--- 添加这一行
from telethon import TelegramClient, events, errors
# from telethon.sessions import Session # <--- 移除这个导入
from telethon.tl.types import PeerUser, PeerChat, PeerChannel
from typing import List # <--- 添加了这一行来修复错误

# (新) 导入定时任务
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# 假设 forwarder_core 和 link_checker 在同一目录下
from forwarder_core import UltimateForwarder, Config, AccountConfig
from link_checker import LinkChecker
from bot_service import BotService # (新) 导入 Bot 服务

# --- 日志配置 ---
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    level=LOG_LEVEL,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logging.getLogger('telethon').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- 全局变量 ---
clients = [] # (新) 用户客户端
bot_client = None # (新) Bot 客户端
forwarder = None # (新) 转发器实例
link_checker = None # (新) 链接检测器实例
DOCKER_CONTAINER_NAME = "tgf" # 默认值
CONFIG_PATH = "/app/config.yaml" # (新) 配置文件路径

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
    clients.clear() # (新) 清空旧客户端
    logger.info(f"正在初始化 {len(config.accounts)} 个用户账号...")
    
    for i, acc in enumerate(config.accounts):
        if not acc.enabled:
            logger.warning(f"账号 {i+1} (Session: {acc.session_name}) 已被禁用，跳过。")
            continue
        
        try:
            logger.info(f"账号 {i+1} 正在使用会话文件: {acc.session_name}...")
            session_path = f"/app/data/{acc.session_name}"
            session_identifier = f"SessionFile ({acc.session_name})"
            
            client = TelegramClient(
                session_path, # <--- 修复: 直接传递路径字符串，而不是 Session(session_path)
                acc.api_id,
                acc.api_hash,
                proxy=config.proxy.get_telethon_proxy() if config.proxy else None
            )
            
            # --- (新) 核心修复 ---
            # 将 session_name 附加到 client 对象上，以便全局访问
            client.session_name_for_forwarder = acc.session_name
            # --- 修复结束 ---
            
            logger.info(f"正在连接账号: {acc.session_name}...")

            if not await client.connect() or not await client.is_user_authorized():
                logger.warning(f"账号 {acc.session_name} 未登录。")
                logger.warning("---")
                logger.warning("程序将等待你输入手机号、验证码和两步验证密码。")
                logger.warning("!!! (重要) 如果你使用 DOCKER, 你必须现在打开 *另一个* 终端并运行: !!!")
                logger.warning(f"    docker attach {DOCKER_CONTAINER_NAME}")
                logger.warning("---")
            
            await client.start()
            
            me = await client.get_me()
            logger.info(f"✅ 账号 {i+1} ({me.first_name} / @{me.username}) 登录成功。")
            clients.append(client)
            
        except errors.SessionPasswordNeededError:
            logger.error(f"❌ 账号 {session_identifier} 需要两步验证密码 (Two-Step Verification)。")
            logger.warning(f"请在控制台 (docker attach {DOCKER_CONTAINER_NAME}) 中输入你的密码。")
        except errors.AuthKeyUnregisteredError:
             logger.error(f"❌ 账号 {session_identifier} 的 Session 已失效，请删除 data 目录下的 {acc.session_name}.session 文件后重试。")
        except Exception as e:
            logger.error(f"❌ 账号 {session_identifier} 启动失败: {e}")
    
    if not clients:
        logger.critical("❌ 致命错误: 没有可用的账号。请检查配置或 Session 文件。")
        sys.exit(1)
    
    logger.info(f"✅ 成功启动 {len(clients)} 个用户客户端。")

async def initialize_bot(config: Config):
    """(新) 初始化 Bot 客户端"""
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
            None, # <--- 修复: 传递 None 来使用内存会话，而不是 Session(None)
            config.accounts[0].api_id, # (新) Bot 也需要 API ID/Hash
            config.accounts[0].api_hash,
            proxy=config.proxy.get_telethon_proxy() if config.proxy else None
        )
        
        await bot_client.start(bot_token=config.bot_service.bot_token)
        me = await bot_client.get_me()
        logger.info(f"✅ Bot (@{me.username}) 登录成功。")

        # (新) 将服务实例传递给 Bot
        # 确保 link_checker 已经初始化
        if not link_checker and config.link_checker.enabled:
             link_checker = LinkChecker(config, clients[0]) # Bot 使用第一个用户客户端来检测

        bot_service = BotService(config, bot_client, forwarder, link_checker, reload_config_func)
        await bot_service.register_commands()
        logger.info("✅ Bot 命令已注册。")

    except Exception as e:
        logger.error(f"❌ Bot 客户端启动失败: {e}")
        bot_client = None


async def resolve_identifiers(client: TelegramClient, identifiers: List[str | int]) -> List[int]:
    """(新) 将频道用户名/链接列表解析为数字 ID 列表"""
    resolved_ids = []
    for identifier in identifiers:
        try:
            # Telethon 可以自动处理 int, @username, 和 https://t.me/link
            entity = await client.get_entity(identifier)
            
            # (新) 确保我们只获取频道的数字 ID
            if isinstance(entity, (PeerUser, PeerChat)):
                resolved_ids.append(entity.id)
            elif isinstance(entity, PeerChannel):
                resolved_ids.append(entity.channel_id)
            else:
                 # (新) 适配 User, Chat, Channel 对象
                resolved_ids.append(entity.id)
                
        except ValueError:
            logger.error(f"❌ 无法解析源: '{identifier}'。它似乎不是一个有效的频道/群组/用户。")
        except errors.ChannelPrivateError:
            logger.error(f"❌ 无法访问源: '{identifier}'。你的账号未加入该私有频道。")
        except Exception as e:
            logger.error(f"❌ 解析源 '{identifier}' 时出错: {e}")
            
    # (新) Telethon 需要的格式是 -100...，它会自动处理
    # 我们只需要确保 get_entity 成功即可
    
    # (新) 修复：Telethon 的 NewMessage(chats=...) 需要的是 Peer* 对象
    # 我们将在 Forwarder 核心中处理 ID 到 Peer 的转换
    
    # (新) 直接返回 get_entity 可以接受的原始标识符
    # return [i for i in identifiers if i]
    
    # (新) 返回解析后的数字 ID
    return list(set(resolved_ids))


async def run_forwarder(config: Config):
    """运行转发器主逻辑"""
    global forwarder, link_checker
    
    await initialize_clients(config)
    
    main_client = clients[0] # 第一个客户端用于监听和解析
    
    # (新) 解析所有源标识符
    logger.info("正在解析所有源频道/群组...")
    source_identifiers = [s.identifier for s in config.sources]
    resolved_source_ids = await resolve_identifiers(main_client, source_identifiers)
    
    if not resolved_source_ids:
        logger.critical("❌ 无法解析任何源频道，请检查配置或确保账号已加入。")
        return
        
    logger.info(f"✅ 成功解析 {len(resolved_source_ids)} 个源。")
    
    # 实例化核心转发器
    forwarder = UltimateForwarder(config, clients)
    
    # 1. 注册新消息处理器
    logger.info("注册新消息事件处理器...")
    # (新) 监听已解析的 ID
    @main_client.on(events.NewMessage(chats=resolved_source_ids))
    async def handle_new_message(event):
        await forwarder.process_message(event)
        
    logger.info("✅ 事件处理器已注册。")

    # (新) 步骤 2: 启动 Bot 服务 (!!! 必须在 process_history 之前!!!)
    logger.info("正在启动 Bot 服务...")
    await initialize_bot(config)

    # (新) 步骤 3: 启动定时任务 (Link Checker)
    if config.link_checker and config.link_checker.enabled:
        if not link_checker: # 如果 Bot 没启动，单独初始化
             link_checker = LinkChecker(config, main_client)
        
        try:
            # (新) 使用 apscheduler 实现 cron 定时任务
            trigger = CronTrigger.from_crontab(config.link_checker.schedule)
            scheduler = AsyncIOScheduler(timezone="UTC")
            scheduler.add_job(link_checker.run, trigger, name="run_link_checker_job")
            scheduler.start()
            logger.info(f"✅ 链接检测器定时任务已启动 (Cron: {config.link_checker.schedule} UTC)。")
        except ValueError as e:
            logger.warning(f"⚠️ 链接检测器 cron 表达式 '{config.link_checker.schedule}' 无效，定时任务未启动: {e}")

    # (新) 步骤 4: (可选) 处理历史消息
    if not config.forwarding.forward_new_only:
        logger.info("配置了 `forward_new_only: false`，开始扫描历史消息 (这可能需要一些时间)...")
        # (新) 传入已解析的 ID
        await forwarder.process_history(resolved_source_ids)
        logger.info("✅ 历史消息扫描完成。")
    else:
        logger.info("`forward_new_only: true`，跳过历史消息扫描。")

    # (新) 步骤 5: 运行并等待
    logger.info(f"🚀 终极转发器已启动。正在监听 {len(resolved_source_ids)} 个源。")
    
    # (新) 如果 Bot 也在运行，使用 asyncio.gather
    if bot_client:
        await asyncio.gather(
            main_client.run_until_disconnected(),
            bot_client.run_until_disconnected()
        )
    else:
        await main_client.run_until_disconnected()

async def run_link_checker(config: Config):
    """运行失效链接检测器"""
    global link_checker
    
    if not config.link_checker or not config.link_checker.enabled:
        logger.warning("LinkChecker 未在 config.yaml 中启用，退出。")
        return

    logger.info("启动失效链接检测器...")
    await initialize_clients(config) # 只需要一个客户端
    
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
                # (新) 优先使用 username，否则使用 ID
                if dialog.entity.username:
                    identifier = f"@{dialog.entity.username}"
                else:
                    identifier = str(dialog.id)
                output += f"{identifier}\t{dialog.title}\n"
                
                # 检查是否是开启了话题的群组
                if dialog.is_group and getattr(dialog.entity, 'forum', False):
                    logger.info(f"正在获取群组 '{dialog.title}' ({dialog.id}) 的话题...")
                    try:
                        # (新) 修复了获取话题的逻辑
                        topics = await main_client.get_topics(dialog.id)
                        for topic in topics:
                            topics_output += f"{dialog.id}\t{topic.id}\t{topic.title}\n"
                    except Exception as e:
                        logger.warning(f"获取话题失败 for {dialog.title}: {e} (可能是权限不足)")

            elif dialog.is_user:
                # (新) 同样支持用户
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
    """(新) Bot 调用的热重载函数"""
    global forwarder, link_checker, bot_client, CONFIG_PATH
    
    logger.warning("🔄 收到 /reload 命令，正在热重载配置...")
    
    try:
        # 1. 重新加载配置文件
        new_config = load_config(CONFIG_PATH)
        
        # 2. 重新初始化需要重载的部分
        # (注意: 客户端和监听器不能完全重启，否则会断开连接)
        
        # 2a. 重载转发器 (它持有所有过滤/分发规则)
        if forwarder:
            await forwarder.reload(new_config)
            logger.info("✅ 转发器规则已热重载。")

        # 2b. 重载链接检测器
        if link_checker:
            link_checker.reload(new_config)
            logger.info("✅ 链接检测器配置已热重载。")

        # 2c. 重载 Bot (主要是 admin_user_ids)
        if bot_client and bot_client.is_connected():
             # 简单起见，BotService 内部会重新加载
             # 我们只需要确保 BotService 实例能拿到新 config
             pass
        
        logger.warning("✅ 配置热重载完毕。")
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
            "  'run' (默认): 启动转发器 (和 Bot)。\n"
            "  'checklinks': 仅运行一次失效链接检测器。\n"
            "  'export': 导出频道和话题ID。"
        )
    )
    parser.add_argument(
        '-c', '--config',
        default='/app/config.yaml', # Docker 内部的绝对路径
        help="配置文件路径 (默认: /app/config.yaml)"
    )
    args = parser.parse_args()
    CONFIG_PATH = args.config # (新) 保存配置路径以供热重载

    # 将配置加载移到 main() 中
    config = load_config(CONFIG_PATH)

    try:
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