# forwarder_core.py
import logging
import random
import re
import asyncio
import httpx
import time
from datetime import datetime, timezone
from telethon import TelegramClient, events, errors
from telethon.tl.types import MessageEntityTextUrl

# --- 类型提示 ---
from typing import List, Optional, Tuple, Dict, Set, Any
from pydantic import BaseModel, Field, HttpUrl, field_validator

logger = logging.getLogger(__name__)

# --- 配置模型 (使用 Pydantic 进行验证) ---

class ProxyConfig(BaseModel):
    enabled: bool = False
    proxy_type: str = "socks5"
    addr: str = "127.0.0.1"
    port: int = 1080
    username: Optional[str] = None
    password: Optional[str] = None
    
    def get_telethon_proxy(self):
        """返回 Telethon 接受的代理元组"""
        if not self.enabled:
            return None
        return (self.proxy_type, self.addr, self.port, True, self.username, self.password)

class AccountConfig(BaseModel):
    api_id: int
    api_hash: str
    session_string: str # 使用 StringSession，而不是 session_name
    session_name: Optional[str] = "default" # 兼容旧配置，但提示
    enabled: bool = True

    @field_validator('session_string', mode='before')
    def check_session_string(cls, v):
        if not v:
            raise ValueError("session_string 不能为空。请生成 StringSession。")
        return v

class SourceConfig(BaseModel):
    id: int # 频道ID (必须是数字ID)
    check_replies: bool = False
    replies_limit: int = 10
    forward_new_only: Optional[bool] = None # 可选：覆盖全局设置

class TargetDistributionRule(BaseModel):
    name: str # 规则名称
    keywords: List[str] = []
    target_id: int # 目标频道/群组 ID
    topic_id: Optional[int] = None # (新功能) 目标话题 ID
    
    def check(self, text: str) -> bool:
        """检查文本是否匹配此规则的任何一个关键词"""
        if not self.keywords:
            return False
        text_lower = text.lower()
        for keyword in self.keywords:
            if keyword.lower() in text_lower:
                return True
        return False

class TargetConfig(BaseModel):
    default_target: int # 默认目标频道/群组 ID
    distribution_rules: List[TargetDistributionRule] = []

class ForwardingConfig(BaseModel):
    mode: str = "forward" # 'forward' 或 'copy'
    forward_new_only: bool = True # 'true' = 只处理新消息, 'false' = 处理历史消息
    
    @field_validator('mode')
    def check_mode(cls, v):
        if v not in ['forward', 'copy']:
            raise ValueError("forwarding.mode 必须是 'forward' 或 'copy'")
        return v

class AdFilterConfig(BaseModel):
    enable: bool = True
    keywords: List[str] = []
    patterns: List[str] = []

class ContentFilterConfig(BaseModel):
    enable: bool = True
    meaningless_words: List[str] = []
    min_meaningful_length: int = 5

class WhitelistConfig(BaseModel):
    enable: bool = False
    keywords: List[str] = []

class DeduplicationConfig(BaseModel):
    enable: bool = True
    db_path: str = "forwarder_dedup.json"

class LinkExtractionConfig(BaseModel):
    check_hyperlinks: bool = True
    check_bots: bool = True

class LinkCheckerConfig(BaseModel):
    enabled: bool = False
    mode: str = "log" # "log", "edit", "delete"
    schedule: str = "0 3 * * *" # Cron 表达式, 默认每天凌晨3点
    
    @field_validator('mode')
    def check_mode(cls, v):
        if v not in ['log', 'edit', 'delete']:
            raise ValueError("link_checker.mode 必须是 'log', 'edit', 或 'delete'")
        return v

class Config(BaseModel):
    proxy: Optional[ProxyConfig] = None
    accounts: List[AccountConfig]
    sources: List[SourceConfig]
    targets: TargetConfig
    forwarding: ForwardingConfig = Field(default_factory=ForwardingConfig)
    ad_filter: Optional[AdFilterConfig] = None
    content_filter: Optional[ContentFilterConfig] = None
    whitelist: Optional[WhitelistConfig] = None
    deduplication: DeduplicationConfig = Field(default_factory=DeduplicationConfig)
    link_extraction: LinkExtractionConfig = Field(default_factory=LinkExtractionConfig)
    replacements: Dict[str, str] = {}
    link_checker: Optional[LinkCheckerConfig] = None
    
    def get_source_chat_ids(self) -> List[int]:
        """获取所有源ID的列表"""
        return [source.id for source in self.sources]

# --- 核心转发器类 ---

class UltimateForwarder:
    def __init__(self, config: Config, clients: List[TelegramClient]):
        self.config = config
        self.clients = clients
        self.current_client_index = 0
        self.client_flood_wait = {} # 存储客户端的FloodWait截止时间
        
        # 加载去重数据库
        self.dedup_db: Set[str] = self._load_dedup_db()
        # 加载进度数据库
        self.progress_db: Dict[str, int] = self._load_progress_db()

        # 编译正则表达式
        self.ad_patterns = self._compile_patterns(config.ad_filter.patterns if config.ad_filter else [])
        
        logger.info(f"终极转发器核心已初始化。")
        logger.info(f"转发模式: {config.forwarding.mode}")
        logger.info(f"处理新消息: {config.forwarding.forward_new_only}")
        logger.info(f"去重数据库: {len(self.dedup_db)} 条记录")
        logger.info(f"进度数据库: {len(self.progress_db)} 个频道")
    
    # --- 数据库/状态管理 ---
    
    def _get_progress_db_path(self) -> str:
        # 简单地使用一个固定的JSON文件
        return "forwarder_progress.json"

    def _load_progress_db(self) -> Dict[str, int]:
        """加载频道转发进度"""
        path = self._get_progress_db_path()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning(f"未找到进度文件 {path}，将创建新的。")
            return {}

    def _save_progress_db(self):
        """保存频道转发进度"""
        path = self._get_progress_db_path()
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.progress_db, f, indent=2)
        except Exception as e:
            logger.error(f"保存进度文件 {path} 失败: {e}")

    def _get_channel_progress(self, channel_id: int) -> int:
        """获取单个频道的最后消息ID"""
        return self.progress_db.get(str(channel_id), 0)

    def _set_channel_progress(self, channel_id: int, message_id: int):
        """设置单个频道的最后消息ID"""
        self.progress_db[str(channel_id)] = message_id
        # TODO: 优化为批量保存，而不是每次都写
        self._save_progress_db()

    def _load_dedup_db(self) -> Set[str]:
        """加载去重数据库"""
        path = self.config.deduplication.db_path
        try:
            with open(path, 'r', encoding='utf-8') as f:
                hashes = json.load(f)
                return set(hashes)
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning(f"未找到去重文件 {path}，将创建新的。")
            return set()

    def _save_dedup_db(self):
        """保存去重数据库"""
        path = self.config.deduplication.db_path
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(list(self.dedup_db), f) # 转换为列表再保存
        except Exception as e:
            logger.error(f"保存去重文件 {path} 失败: {e}")

    # --- 客户端管理 ---
    
    def _get_next_client(self) -> TelegramClient:
        """获取下一个可用的客户端 (轮换 & FloodWait 检查)"""
        start_index = self.current_client_index
        
        while True:
            client = self.clients[self.current_client_index]
            client_id = client.session.session_id
            
            wait_until = self.client_flood_wait.get(client_id, 0)
            
            if time.time() > wait_until:
                # 客户端可用
                # 轮换索引以便下次使用下一个
                self.current_client_index = (self.current_client_index + 1) % len(self.clients)
                return client
            
            # 客户端正在 FloodWait，检查下一个
            self.current_client_index = (self.current_client_index + 1) % len(self.clients)
            
            if self.current_client_index == start_index:
                # 所有客户端都在 FloodWait
                all_wait_times = [self.client_flood_wait.get(c.session.session_id, 0) for c in self.clients]
                min_wait_time = min(all_wait_times)
                sleep_duration = max(1.0, (min_wait_time - time.time()) + 1.0) # 等待最短时间的客户端 + 1秒
                
                logger.warning(f"所有 {len(self.clients)} 个客户端都在 FloodWait。等待 {sleep_duration:.1f} 秒...")
                time.sleep(sleep_duration) # 同步等待
                
                # 重新开始循环检查
                start_index = self.current_client_index
                continue

    async def _handle_send_error(self, e: Exception, client: TelegramClient):
        """统一处理发送错误"""
        client_id = client.session.session_id
        if isinstance(e, errors.FloodWaitError):
            logger.warning(f"客户端 {client_id[:5]}... 触发 FloodWait: {e.seconds} 秒。")
            self.client_flood_wait[client_id] = time.time() + e.seconds + 5 # 增加5秒缓冲
        elif isinstance(e, errors.ChatWriteForbiddenError):
            logger.error(f"客户端 {client_id[:5]}... 无法写入目标频道 (权限不足)。")
            # 这种情况通常是永久性的，不需要重试
        elif isinstance(e, errors.UserBannedInChannelError):
            logger.error(f"客户端 {client_id[:5]}... 已被目标频道封禁。")
        else:
            logger.error(f"客户端 {client_id[:5]}... 转发时遇到未知错误: {e}")

    # --- 消息处理流水线 (Process Pipeline) ---

    async def process_message(self, event: events.NewMessage.Event):
        """处理单条消息的主流水线"""
        message = event.message
        chat_id = event.chat_id
        
        # 0. 获取源配置
        source_config = next((s for s in self.config.sources if s.id == chat_id), None)
        if not source_config:
            logger.warning(f"收到来自未知源 {chat_id} 的消息，已忽略。")
            return
            
        logger.debug(f"--- [START] 正在处理消息 {chat_id}/{message.id} ---")

        try:
            # 1. 提取链接 (包括评论区和机器人)
            # 这一步可能会产生多条 "pseudo" 消息
            messages_to_process = await self._extract_links(message, source_config)

            # 2. 依次处理提取到的每条消息
            for msg_data in messages_to_process:
                # 3. 内容替换
                msg_data['text'] = self._apply_replacements(msg_data['text'])
                
                # 4. 内容过滤
                if self._should_filter(msg_data['text'], msg_data['media']):
                    logger.info(f"消息 {chat_id}/{message.id} (Text: {msg_data['text'][:30]}...) [被过滤]")
                    continue

                # 5. 内容去重
                if self._is_duplicate(msg_data, f"{chat_id}/{message.id}"):
                    logger.info(f"消息 {chat_id}/{message.id} (Text: {msg_data['text'][:30]}...) [重复]")
                    continue
                
                # 6. 查找目标
                target_id, topic_id = self._find_target(msg_data['text'])
                
                # 7. 执行发送
                logger.info(f"消息 {chat_id}/{message.id} [将被发送] -> 目标 {target_id}/(Topic:{topic_id})")
                await self._send_message(
                    original_message=message,
                    message_data=msg_data,
                    target_id=target_id,
                    topic_id=topic_id
                )
                
                # 8. 标记为已处理 (去重)
                self._mark_as_processed(msg_data)
                
        except Exception as e:
            logger.error(f"处理消息 {chat_id}/{message.id} 时发生严重错误: {e}", exc_info=True)
        finally:
            logger.debug(f"--- [END] 消息 {chat_id}/{message.id} 处理完毕 ---")
            # 无论如何都更新进度，避免卡住
            self._set_channel_progress(chat_id, message.id)

    async def process_history(self):
        """处理历史消息 (仅在 `forward_new_only: false` 时调用)"""
        client = self._get_next_client() # 获取一个客户端用于处理
        
        for source in self.config.sources:
            # 检查是否覆盖了全局设置
            process_history = not self.config.forwarding.forward_new_only
            if source.forward_new_only is not None:
                process_history = not source.forward_new_only
                
            if not process_history:
                logger.info(f"跳过源 {source.id} 的历史记录 (已在源配置中禁用)。")
                continue

            last_id = self._get_channel_progress(source.id)
            logger.info(f"正在扫描源 {source.id} 的历史记录 (从消息 ID {last_id} 开始)...")
            
            try:
                # 反向迭代消息 (从旧到新)
                async for message in client.iter_messages(source.id, offset_id=last_id, reverse=True, limit=None):
                    # 伪造一个 event 对象
                    fake_event = events.NewMessage.Event(message=message)
                    fake_event.chat_id = source.id
                    await self.process_message(fake_event)
                    
            except Exception as e:
                logger.error(f"扫描源 {source.id} 历史记录时失败: {e}")
                
            logger.info(f"源 {source.id} 历史记录扫描完成。")


    # --- 流水线步骤 (Pipeline Steps) ---

    async def _extract_links(self, message: Any, config: SourceConfig) -> List[Dict[str, Any]]:
        """
        步骤 1: 提取链接。
        将 `tgforwarder` 的高级提取 (超链接, 评论, 机器人) 整合到这里。
        返回一个列表，每个元素是一个 "pseudo-message" 字典。
        """
        results = []
        main_text = message.text or ""
        
        # 基础消息 (原始消息)
        # 我们假设原始消息总是要被处理的，除非它只包含广告
        results.append({
            "text": main_text,
            "media": message.media,
            "hash_source": message.id # 用于去重
        })

        # TODO: 实现 tgforwarder 的高级链接提取
        # 1. 提取 MessageEntityTextUrl (超链接)
        if self.config.link_extraction.check_hyperlinks and message.entities:
            for entity in message.entities:
                if isinstance(entity, MessageEntityTextUrl):
                    # TODO: 检查URL是否为telegra.ph或机器人
                    pass

        # 2. 检查评论区
        if config.check_replies and message.replies:
             # TODO: 抓取评论
             pass
        
        # 3. 检查机器人按钮
        if self.config.link_extraction.check_bots and message.buttons:
            # TODO: 模拟点击机器人
            pass
            
        # 注意：为了简化，这里暂时只返回原始消息。
        # 完整的实现需要异步抓取上述链接，并为每个提取到的链接/资源
        # 创建一个新的 "pseudo-message" 添加到 results 列表中。
        
        return results

    def _apply_replacements(self, text: str) -> str:
        """步骤 2: 内容替换"""
        if not text or not self.config.replacements:
            return text
        
        for find, replace_with in self.config.replacements.items():
            text = text.replace(find, replace_with) # 简单的替换
            # TODO: 支持正则表达式替换
            
        return text

    def _compile_patterns(self, patterns: List[str]) -> List[re.Pattern]:
        """编译正则表达式"""
        compiled = []
        for p in patterns:
            try:
                compiled.append(re.compile(p, re.IGNORECASE))
            except re.error as e:
                logger.warning(f"无效的正则表达式: '{p}', 错误: {e}")
        return compiled

    def _should_filter(self, text: str, media: Any) -> bool:
        """步骤 3: 内容过滤"""
        text = text or ""
        text_lower = text.lower()
        
        # 1. 白名单 (最高优先级)
        if self.config.whitelist and self.config.whitelist.enable:
            if not any(kw.lower() in text_lower for kw in self.config.whitelist.keywords):
                logger.debug(f"Filter [Whitelist]: 未命中白名单。")
                return True # 不在白名单中，过滤掉
            else:
                logger.debug(f"Filter [Whitelist]: 命中白名单，通过。")
                return False # 在白名单中，不再进行后续过滤

        # 2. 广告过滤
        if self.config.ad_filter and self.config.ad_filter.enable:
            # 关键词
            if any(kw.lower() in text_lower for kw in self.config.ad_filter.keywords):
                logger.debug(f"Filter [Ad Keyword]: 命中广告关键词。")
                return True
            # 正则
            for p in self.ad_patterns:
                if p.search(text):
                    logger.debug(f"Filter [Ad Pattern]: 命中广告正则 {p.pattern}。")
                    return True

        # 3. 内容质量过滤
        if self.config.content_filter and self.config.content_filter.enable:
            if not text and not media:
                logger.debug(f"Filter [Content]: 既无文本也无媒体。")
                return True # 过滤空消息
            
            # 无意义词汇
            if text_lower in [w.lower() for w in self.config.content_filter.meaningless_words]:
                logger.debug(f"Filter [Content]: 命中无意义词汇。")
                return True
                
            # 最小长度 (仅在没有媒体时检查)
            if not media and len(text.strip()) < self.config.content_filter.min_meaningful_length:
                logger.debug(f"Filter [Content]: 文本过短且无媒体。")
                return True

        return False # 通过所有过滤

    def _get_message_hash(self, message_data: Dict[str, Any]) -> Optional[str]:
        """为消息生成一个用于去重的哈希"""
        if not self.config.deduplication.enable:
            return None
            
        # 优先使用媒体文件ID
        media = message_data.get('media')
        if media:
            # TODO: 更复杂的Hash，例如 tg_zf 中的文件大小+ID
            if hasattr(media, 'photo'):
                return f"photo:{media.photo.id}"
            if hasattr(media, 'document'):
                return f"doc:{media.document.id}:{media.document.size}"
        
        # 其次使用文本 (如果文本很短，可能误判)
        text = message_data.get('text', "")
        if len(text) > 50: # 只对较长的文本进行哈希
            return f"text:{hash(text)}"
            
        # 最后使用原始ID (这只在提取链接时有用)
        return f"id:{message_data.get('hash_source')}"

    def _is_duplicate(self, message_data: Dict[str, Any], log_id: str) -> bool:
        """步骤 4: 内容去重"""
        if not self.config.deduplication.enable:
            return False
            
        msg_hash = self._get_message_hash(message_data)
        if not msg_hash:
            logger.debug(f"无法为 {log_id} 生成哈希，跳过。")
            return False
            
        if msg_hash in self.dedup_db:
            return True
        return False

    def _mark_as_processed(self, message_data: Dict[str, Any]):
        """标记消息为已处理 (用于去重)"""
        if not self.config.deduplication.enable:
            return
            
        msg_hash = self._get_message_hash(message_data)
        if msg_hash:
            self.dedup_db.add(msg_hash)
            # TODO: 优化为批量保存
            self._save_dedup_db()

    def _find_target(self, text: str) -> Tuple[int, Optional[int]]:
        """
        步骤 5: 查找目标。
        根据分发规则，返回 (target_id, topic_id)
        """
        rules = self.config.targets.distribution_rules
        if rules:
            for rule in rules:
                if rule.check(text):
                    logger.debug(f"命中分发规则: '{rule.name}'")
                    return rule.target_id, rule.topic_id
                    
        # 未命中任何规则，返回默认目标
        logger.debug("未命中分发规则，使用默认目标。")
        return self.config.targets.default_target, None

    async def _send_message(self, original_message: Any, message_data: Dict[str, Any], target_id: int, topic_id: Optional[int]):
        """步骤 7: 执行发送 (包含重试和多账号逻辑)"""
        
        text = message_data['text']
        media = message_data['media']
        mode = self.config.forwarding.mode
        
        # 准备发送参数
        send_kwargs = {
            "reply_to": topic_id
        }

        while True:
            client = self._get_next_client()
            try:
                if mode == 'copy':
                    # 复制模式 (来自 tgforwarder)
                    await client.send_message(
                        target_id,
                        message=text,
                        file=media,
                        **send_kwargs
                    )
                else:
                    # 转发模式 (来自 tg_zf)
                    await client.forward_messages(
                        target_id,
                        messages=original_message,
                        **send_kwargs
                    )
                
                logger.debug(f"客户端 {client.session.session_id[:5]}... 发送成功。")
                return # 发送成功，退出循环

            except Exception as e:
                await self._handle_send_error(e, client)
                # 发生错误 (例如 FloodWait), 循环将继续，
                # _get_next_client 会获取下一个可用的客户端或等待。