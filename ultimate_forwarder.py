import asyncio
import logging
import argparse
import yaml
import sys
import os
# import base64 (Removed)
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession

# 假设 forwarder_core 和 link_checker 在同一目录下
from forwarder_core import UltimateForwarder, Config
from link_checker import LinkChecker

# --- 日志配置 ---
# CRITICAL 50, ERROR 40, WARNING 30, INFO 20, DEBUG 10
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    level=LOG_LEVEL,
    handlers=[
        logging.StreamHandler(sys.stdout) # 输出到控制台
    ]
)
logging.getLogger('telethon').setLevel(logging.WARNING) # 屏蔽Telethon的DEBUG日志
logger = logging.getLogger(__name__)

# --- 全局客户端列表 ---
clients = []

def load_config(path):
    """加载 YAML 配置文件"""
    logger.info(f"正在从 {path} 加载配置...")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        
        # 将 Pydantic 模型用于验证和构建
        config_obj = Config(**config_data)
        logger.info("✅ 配置文件加载并验证成功。")
        return config_obj
    except FileNotFoundError:
        logger.critical(f"❌ 致命错误: 配置文件 {path} 未找到。")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"❌ 致命错误: 加载或解析配置文件 {path} 失败: {e}")
        sys.exit(1)

async def initialize_clients(config: Config):
    """初始化所有 Telethon 客户端 (支持混合登录)"""
    global clients
    logger.info(f"正在初始化 {len(config.accounts)} 个账号...")
    
    for i, acc in enumerate(config.accounts):
        if not acc.enabled:
            logger.warning(f"账号 {i+1} (Session: {acc.session_name}) 已被禁用，跳过。") # (Modified)
            continue
        
        try:
            # (Modified) 简化为只支持 session_name
            logger.info(f"账号 {i+1} 正在使用会话文件: {acc.session_name}...")
            # 确保会话文件保存在持久化目录 /app/data 中
            session_path = f"/app/data/{acc.session_name}"
            session_data = session_path
            session_identifier = f"SessionFile ({acc.session_name})"

            
            client = TelegramClient(
                session_data, # (已修改)
                acc.api_id,
                acc.api_hash,
                proxy=config.proxy.get_telethon_proxy() if config.proxy else None
            )
            
            # (已修改) 仅在 方式A (Session File) 且未登录时才提示
            if acc.session_name and not await client.is_user_authorized():
                logger.warning(f"账号 {acc.session_name} 未登录。")
                logger.warning("请在控制台输入手机号 (例如 +861234567890) 和验证码。")
                container_name = config.docker_container_name or "YOUR_CONTAINER_NAME"
                logger.warning(f"如果使用 Docker, 请运行: docker attach {container_name}")
            
            await client.start()
            me = await client.get_me()
            logger.info(f"✅ 账号 {i+1} ({me.first_name if me.first_name else me.username}) 登录成功。")
            clients.append(client)
            
        except errors.SessionPasswordNeededError:
            logger.error(f"❌ 账号 {session_identifier} 需要两步验证密码 (Two-Step Verification)。") # (Modified)
            logger.warning("请在控制台 (docker attach) 中输入你的密码。")
        except errors.AuthKeyUnregisteredError:
             logger.error(f"❌ 账号 {session_identifier} 的 Session 已失效，请删除 data 目录下的 {acc.session_name}.session 文件后重试。") # (Modified)
        except Exception as e:
            logger.error(f"❌ 账号 {session_identifier} 启动失败: {e}") # (Modified)
    
    if not clients:
        logger.critical("❌ 致命错误: 没有可用的账号。请检查配置或 Session。")
        sys.exit(1)
    
    logger.info(f"✅ 成功启动 {len(clients)} 个客户端。")

async def run_forwarder(config: Config):
    """运行转发器主逻辑"""
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

async def run_link_checker(config: Config):
    """运行失效链接检测器"""
    if not config.link_checker or not config.link_checker.enabled:
        logger.warning("LinkChecker 未在 config.yaml 中启用，退出。")
        return

    logger.info("启动失效链接检测器...")
    await initialize_clients(config) # 只需要一个客户端
    
    checker = LinkChecker(config, clients[0])
    await checker.run()
    logger.info("✅ 失效链接检测完成。")

async def export_dialogs(config: Config):
    """导出频道和话题信息"""
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

        print("\n\n" + "="*30)
        print(output)
        print(topics_output)
        print("="*30 + "\n")
        
        logger.info("---")
        logger.info("如何使用:")
        logger.info("1. 在 'sources' 配置中，使用 'ID' 列的 ID (例如 -100123456789)。")
        logger.info("2. 在 'targets.distribution_rules' 中，使用 '群组ID' 和 '话题ID'。")
        
    except Exception as e:
        logger.error(f"导出对话失败: {e}")

# ... (run_forwarder, run_link_checker, export_dialogs remain the same) ...
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
    
    # (Modified) 移除 CONFIG_BASE64 逻辑
    config_path = args.config

    
    # 将配置加载移到 main() 中，以便 Docker 提示可以读取 container_name
    config = load_config(config_path)
    # 将容器名存入类变量，以便日志提示
    Config.docker_container_name = config.docker_container_name if config.docker_container_name else "YOUR_CONTAINER_NAME"


    try:
        if args.mode == 'run':
            await run_forwarder(config) # (已修改) 传递 config 对象
        elif args.mode == 'checklinks':
            await run_link_checker(config) # (已修改) 传递 config 对象
        elif args.mode == 'export':
            await export_dialogs(config) # (已修改) 传递 config 对象
            
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