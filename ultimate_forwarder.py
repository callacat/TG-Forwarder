# ultimate_forwarder.py
import asyncio
import logging
import argparse
import yaml
import sys
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession

from forwarder_core import UltimateForwarder, Config

# --- 日志配置 ---
# CRITICAL 50, ERROR 40, WARNING 30, INFO 20, DEBUG 10
LOG_LEVEL = logging.INFO
logging.basicConfig(
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    level=LOG_LEVEL,
    handlers=[
        logging.StreamHandler(sys.stdout) # 输出到控制台
        # logging.FileHandler("forwarder.log") # 输出到文件
    ]
)
logging.getLogger('telethon').setLevel(logging.WARNING) # 屏蔽Telethon的DEBUG日志
logger = logging.getLogger(__name__)

# --- 全局客户端列表 ---
clients = []
current_client_index = 0

def load_config(path):
    """加载 YAML 配置文件"""
    logger.info(f"正在从 {path} 加载配置...")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        logger.info("✅ 配置文件加载成功。")
        return Config(config_data) # 使用Pydantic模型验证和构建配置
    except FileNotFoundError:
        logger.critical(f"❌ 致命错误: 配置文件 {path} 未找到。")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"❌ 致命错误: 加载或解析配置文件 {path} 失败: {e}")
        sys.exit(1)

async def initialize_clients(config: Config):
    """初始化所有 Telethon 客户端"""
    global clients
    logger.info(f"正在初始化 {len(config.accounts)} 个账号...")
    
    for i, acc in enumerate(config.accounts):
        if not acc.enabled:
            logger.warning(f"账号 {i+1} (Session: {acc.session_name[:5]}...) 已被禁用，跳过。")
            continue
        
        try:
            client = TelegramClient(
                StringSession(acc.session_string),
                acc.api_id,
                acc.api_hash,
                proxy=config.proxy.get_telethon_proxy() if config.proxy else None
            )
            
            await client.start()
            me = await client.get_me()
            logger.info(f"✅ 账号 {i+1} ({me.first_name}) 登录成功。")
            clients.append(client)
            
        except errors.SessionPasswordNeededError:
            logger.error(f"❌ 账号 {i+1} (Session: {acc.session_name[:5]}...) 需要两步验证密码，请在本地运行一次以授权。")
        except errors.AuthKeyUnregisteredError:
             logger.error(f"❌ 账号 {i+1} (Session: {acc.session_string}) Session已失效，请重新生成。")
        except Exception as e:
            logger.error(f"❌ 账号 {i+1} (Session: {acc.session_name[:5]}...) 启动失败: {e}")
    
    if not clients:
        logger.critical("❌ 致命错误: 没有可用的账号。请检查配置或 Session 字符串。")
        sys.exit(1)
    
    logger.info(f"✅ 成功启动 {len(clients)} 个客户端。")

async def run_forwarder(config_path: str):
    """运行转发器主逻辑"""
    config = load_config(config_path)
    await initialize_clients(config)
    
    # 获取第一个客户端作为主客户端（用于监听）
    main_client = clients[0]
    
    # 实例化核心转发器
    forwarder = UltimateForwarder(config, clients)
    
    # 1. 注册新消息处理器
    logger.info("注册新消息事件处理器...")
    @main_client.on(events.NewMessage(chats=config.get_source_chat_ids()))
    async def handle_new_message(event):
        await forwarder.process_message(event)
        
    logger.info("✅ 事件处理器已注册。")

    # 2. (可选) 处理历史消息
    if not config.forwarding.forward_new_only:
        logger.info("配置了 `forward_new_only: false`，开始扫描历史消息...")
        await forwarder.process_history()
        logger.info("✅ 历史消息扫描完成。")
    else:
        logger.info("`forward_new_only: true`，跳过历史消息扫描。")

    # 3. 运行并等待
    logger.info(f"🚀 终极转发器已启动。正在监听 {len(config.sources)} 个源。")
    await main_client.run_until_disconnected()

async def run_link_checker(config_path: str):
    """运行失效链接检测器"""
    from link_checker import LinkChecker
    
    config = load_config(config_path)
    if not config.link_checker or not config.link_checker.enabled:
        logger.warning("LinkChecker 未在 config.yaml 中启用，退出。")
        return

    logger.info("启动失效链接检测器...")
    await initialize_clients(config) # 只需要一个客户端
    
    checker = LinkChecker(config, clients[0])
    await checker.run()
    logger.info("✅ 失效链接检测完成。")

async def export_dialogs(config_path: str):
    """导出频道和话题信息"""
    config = load_config(config_path)
    await initialize_clients(config)
    main_client = clients[0]

    logger.info("正在导出所有对话... (这可能需要一点时间)")
    
    try:
        dialogs = await main_client.get_dialogs()
        output = "--- 频道/群组列表 (ID / 名称) ---\n"
        topics_output = "\n--- 群组话题列表 (群组ID / 话题ID / 话题名称) ---\n"

        for dialog in dialogs:
            if dialog.is_channel or dialog.is_group:
                output += f"{dialog.id}\t{dialog.title}\n"
                
                # 检查是否是开启了话题的群组
                if dialog.is_group and getattr(dialog.entity, 'forum', False):
                    logger.info(f"正在获取群组 '{dialog.title}' ({dialog.id}) 的话题...")
                    try:
                        # 获取话题
                        async for topic in main_client.iter_messages(dialog.entity, 0, search=""):
                            # 话题的 "message" 是一个特殊的 MessageService
                            if topic.action and hasattr(topic.action, 'title'):
                                topics_output += f"{dialog.id}\t{topic.id}\t{topic.action.title}\n"
                    except Exception as e:
                        logger.warning(f"获取话题失败 for {dialog.title}: {e}")

        print(output)
        print(topics_output)
        
        logger.info("---")
        logger.info("如何使用:")
        logger.info("1. 在 'sources' 配置中，使用 'ID' 列的 ID (例如 -100123456789)。")
        logger.info("2. 在 'targets.distribution_rules' 中，使用 '群组ID' 和 '话题ID'。")
        
    except Exception as e:
        logger.error(f"导出对话失败: {e}")


async def main():
    parser = argparse.ArgumentParser(description="TG Ultimate Forwarder - 终极 Telegram 转发器")
    parser.add_argument(
        'mode',
        choices=['run', 'checklinks', 'export'],
        default='run',
        nargs='?', # '?' 表示 0 或 1 个参数
        help=(
            "运行模式: \n"
            "  'run' (默认): 启动转发器。\n"
            "  'checklinks': 运行失效链接检测器。\n"
            "  'export': 导出频道和话题ID。"
        )
    )
    parser.add_argument(
        '-c', '--config',
        default='config.yaml',
        help="配置文件路径 (默认: config.yaml)"
    )
    args = parser.parse_args()

    try:
        if args.mode == 'run':
            await run_forwarder(args.config)
        elif args.mode == 'checklinks':
            await run_link_checker(args.config)
        elif args.mode == 'export':
            await export_dialogs(args.config)
            
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("程序被用户中断。")
    except Exception as e:
        logger.critical(f"❌ 出现未捕获的致命错误: {e}", exc_info=True)
    finally:
        for client in clients:
            if client.is_connected():
                await client.disconnect()
        logger.info("所有客户端已断开连接。程序退出。")

if __name__ == "__main__":
    asyncio.run(main())