# link_checker.py
import logging
import asyncio
import httpx
import json
from telethon import TelegramClient
from telethon.errors import RPCError
from bs4 import BeautifulSoup
import re
from typing import List, Dict, Any
from forwarder_core import Config # 复用配置模型

logger = logging.getLogger(__name__)

# 基于 TGNetDiskLinkChecker.py 优化

class LinkChecker:
    def __init__(self, config: Config, client: TelegramClient):
        self.config = config
        self.client = client
        self.checker_config = config.link_checker
        self.target_channel = config.targets.default_target # 默认检查主目标频道
        
        # TODO: 让用户在 config.yaml 中配置要检查的网盘域名
        self.net_disk_domains = [
            'pan.quark.cn', 'aliyundrive.com', 'alipan.com',
            '115.com', 'pan.baidu.com', 'cloud.189.cn', 'drive.uc.cn'
        ]
        
        # 链接检测状态数据库
        self.db_path = "link_checker_db.json"
        self.link_db: Dict[str, Dict[str, Any]] = self._load_db()

    def _load_db(self) -> Dict[str, Dict[str, Any]]:
        """加载链接状态数据库"""
        try:
            with open(self.db_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logger.info(f"未找到链接检测数据库 {self.db_path}，将创建新的。")
            return {}

    def _save_db(self):
        """保存链接状态数据库"""
        try:
            with open(self.db_path, 'w', encoding='utf-8') as f:
                json.dump(self.link_db, f, indent=2)
        except Exception as e:
            logger.error(f"保存链接检测数据库 {self.db_path} 失败: {e}")

    def _extract_links(self, message_text: str) -> List[str]:
        """从消息文本中提取网盘链接"""
        if not message_text:
            return []
        url_pattern = r'https?://[^\s]+'
        urls = re.findall(url_pattern, message_text)
        links = [url for url in urls if any(domain in url for domain in self.net_disk_domains)]
        return list(set(links)) # 去重

    async def _check_link_validity(self, url: str) -> bool:
        """
        检查单个链接的有效性 (简化版)。
        一个真正的实现需要像 TGNetDiskLinkChecker.py 那样为每个网盘编写单独的检测逻辑。
        """
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.head(url, headers={"User-Agent": "Mozilla/5.0"})
                
                if response.status_code == 404:
                    logger.debug(f"Link check (HEAD) {url} -> 404 Not Found")
                    return False
                if response.status_code >= 400:
                     logger.debug(f"Link check (HEAD) {url} -> {response.status_code}")
                     # 其他客户端错误 (403, 410) 也可能意味着失效
                     return False
                
                # TODO: 替换为 TGNetDiskLinkChecker.py 中更复杂的HTML内容检查
                
                return True
        except httpx.RequestError as e:
            logger.warning(f"检测链接 {url} 时发生网络错误: {e}")
            return True # 网络错误，暂时认为有效
        except Exception as e:
            logger.error(f"检测链接 {url} 时发生未知错误: {e}")
            return True # 未知错误，暂时认为有效

    async def run(self):
        """运行检测器的主逻辑"""
        logger.info("--- 启动失效链接检测器 ---")
        if not self.checker_config:
            logger.error("Link checker config is missing.")
            return

        logger.info(f"检测模式: {self.checker_config.mode}")
        logger.info(f"目标频道: {self.target_channel}")
        
        # 1. 扫描目标频道，收集所有链接
        # 为了演示，我们只扫描数据库中没有的
        
        last_processed_id = self.link_db.get("_meta", {}).get("last_processed_id", 0)
        logger.info(f"从消息 ID {last_processed_id} 开始扫描频道...")
        
        new_links_found = 0
        try:
            async for message in self.client.iter_messages(self.target_channel, min_id=last_processed_id):
                links = self._extract_links(message.text)
                if links:
                    for link in links:
                        if link not in self.link_db:
                            self.link_db[link] = {
                                "message_id": message.id,
                                "status": "pending", # 'pending', 'valid', 'invalid'
                                "last_checked": None
                            }
                            new_links_found += 1
                
                # 更新最后处理的ID
                last_processed_id = max(last_processed_id, message.id)

            self.link_db["_meta"] = {"last_processed_id": last_processed_id}
            logger.info(f"频道扫描完成，发现 {new_links_found} 个新链接。")

        except Exception as e:
            logger.error(f"扫描频道 {self.target_channel} 失败: {e}")

        # 2. 批量检测 'pending' 或 'invalid' (用于重试) 的链接
        links_to_check = [link for link, data in self.link_db.items() if link != "_meta" and data['status'] != 'valid']
        logger.info(f"总共有 {len(links_to_check)} 个链接需要检测...")

        # TODO: 使用 asyncio.Semaphore 实现并发限制
        
        invalid_messages = {} # 存储失效链接对应的 message_id

        for link in links_to_check:
            is_valid = await self._check_link_validity(link)
            data = self.link_db[link]
            data['last_checked'] = datetime.now(timezone.utc).isoformat()
            
            if is_valid:
                data['status'] = 'valid'
            else:
                data['status'] = 'invalid'
                logger.warning(f"检测到失效链接: {link} (Message ID: {data['message_id']})")
                
                if data['message_id'] not in invalid_messages:
                    invalid_messages[data['message_id']] = []
                invalid_messages[data['message_id']].append(link)

        # 3. 根据模式 (mode) 执行操作
        if self.checker_config.mode == "log":
            logger.info("检测完成 (日志模式)。")
            
        elif self.checker_config.mode == "edit":
            logger.info("正在编辑包含失效链接的消息...")
            for msg_id, links in invalid_messages.items():
                try:
                    message = await self.client.get_messages(self.target_channel, ids=msg_id)
                    if not message or not message.text:
                        continue
                    
                    new_text = message.text
                    for link in links:
                        # 简单的在链接后添加失效标记
                        new_text = new_text.replace(link, f"{link} [链接已失效]")
                        
                    await self.client.edit_message(self.target_channel, msg_id, new_text)
                    logger.info(f"已编辑消息 {msg_id}")
                except Exception as e:
                    logger.error(f"编辑消息 {msg_id} 失败: {e}")

        elif self.checker_config.mode == "delete":
            logger.info("正在删除包含失效链接的消息...")
            # TODO: 实现更复杂的逻辑，例如 "只有当 *所有* 链接都失效时" 才删除
            msg_ids_to_delete = list(invalid_messages.keys())
            try:
                await self.client.delete_messages(self.target_channel, msg_ids_to_delete)
                logger.info(f"已删除 {len(msg_ids_to_delete)} 条消息。")
            except RPCError as e:
                logger.error(f"批量删除消息失败: {e}")

        # 4. 保存数据库
        self._save_db()
        logger.info("--- 失效链接检测器运行完毕 ---")