# forwarder_core.py
import logging
import random
import re
import asyncio
import httpx
import time
import json
import os 
from datetime import datetime, timezone
from telethon import TelegramClient, events, errors
# (新) 修复：导入 Message 对象
from telethon.tl.types import Message, MessageEntityTextUrl, MessageMediaDocument, PeerUser, PeerChat, PeerChannel
# (新) 修复问题4：导入 Channel 和 Chat
from telethon.tl.types import Channel, Chat 

# --- 类型提示 ---
from typing import List, Optional, Tuple, Dict, Set, Any, Union 
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

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
    session_name: str
    enabled: bool = True

    @model_validator(mode='before')
    @classmethod
    def check_session_auth(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get('session_name'): 
                raise ValueError("必须提供 session_name (会话文件)。")
            
            if data.get('session_name'):
                name = data['session_name']
                if '/' in name or '\\' in name:
                    raise ValueError("session_name 不能包含路径分隔符。")
        return data

class SourceConfig(BaseModel):
    identifier: Union[int, str]
    check_replies: bool = False
    replies_limit: int = 10
    forward_new_only: Optional[bool] = None
    resolved_id: Optional[int] = Field(None, exclude=True) # (新) 用于存储解析后的 ID

class TargetDistributionRule(BaseModel):
    name: str 
    # (新) 增加 all_keywords 字段，用于 AND 逻辑
    all_keywords: List[str] = Field(default_factory=list, description="[AND] 必须 *同时* 包含列表中的所有关键词")
    any_keywords: List[str] = Field(default_factory=list, description="[OR] 包含列表中的 *任意一个* 关键词即可")
    file_types: List[str] = Field(default_factory=list, description="[OR] 匹配任意一个MIME Type") 
    file_name_patterns: List[str] = Field(default_factory=list, description="[OR] 匹配任意一个文件名通配符") 

    target_identifier: Union[int, str]
    topic_id: Optional[int] = None 
    
    resolved_target_id: Optional[int] = Field(None, exclude=True)
    
    def check(self, text: str, media: Any) -> bool:
        """
        (已修改) 检查消息是否匹配此规则。
        逻辑: (all_keywords) AND (any_keywords OR file_types OR file_name_patterns)
        
        如果 all_keywords, any_keywords, file_types, file_name_patterns 都为空，则规则不匹配。
        如果 all_keywords 不为空，但 [OR] 组 (any_keywords, file_types, file_name_patterns) 为空，
        则仅当 all_keywords 匹配时，规则才匹配。
        """
        text_lower = text.lower() if text else ""
        
        # 1. 检查 [AND] all_keywords
        if self.all_keywords:
            if not all(kw.lower() in text_lower for kw in self.all_keywords):
                return False # [AND] 检查失败，此规则不匹配
        
        # 2. 检查 [OR] 条件组
        or_group_matched = False
        
        # 检查 [OR] any_keywords
        if self.any_keywords:
            if any(keyword.lower() in text_lower for keyword in self.any_keywords):
                or_group_matched = True
        
        # 检查 [OR] media (file_types / file_name_patterns)
        if not or_group_matched and media and isinstance(media, MessageMediaDocument):
            doc = media.document
            if doc:
                # 检查 [OR] file_types
                if self.file_types and doc.mime_type:
                    if any(ft.lower() in doc.mime_type.lower() for ft in self.file_types):
                        or_group_matched = True

                # 检查 [OR] file_name_patterns
                if not or_group_matched and self.file_name_patterns:
                    file_name = next((attr.file_name for attr in doc.attributes if hasattr(attr, 'file_name')), None)
                    if file_name:
                        for pattern_str in self.file_name_patterns:
                            try:
                                pattern = re.compile(pattern_str.replace('.', r'\.').replace('*', r'.*') + '$', re.IGNORECASE)
                                if re.search(pattern, file_name):
                                    or_group_matched = True
                                    break # 找到一个匹配就够了
                            except re.error:
                                logger.warning(f"规则 '{self.name}' 中的文件名模式 '{pattern_str}' 无效")
        
        # 3. 最终逻辑判断
        has_all_keywords = bool(self.all_keywords)
        has_or_group = bool(self.any_keywords or self.file_types or self.file_name_patterns)

        if has_all_keywords and not has_or_group:
            # 只有 [AND] 规则：all_keywords 必须匹配 (在步骤1中已检查)
            return True
        elif not has_all_keywords and has_or_group:
            # 只有 [OR] 规则：or_group 必须匹配
            return or_group_matched
        elif has_all_keywords and has_or_group:
            # [AND] + [OR] 规则：两者都必须匹配
            # all_keywords 已在步骤1中检查通过
            return or_group_matched
        else:
            # 所有列表都为空，规则无效
            return False

class TargetConfig(BaseModel):
    default_target: Union[int, str]
    # (新) 修复问题4：添加 default_topic_id
    default_topic_id: Optional[int] = None 
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    
    resolved_default_target_id: Optional[int] = Field(None, exclude=True)


class ForwardingConfig(BaseModel):
    mode: str = "forward" 
    forward_new_only: bool = True 
    # (新) 自动已读功能
    mark_as_read: bool = False
    
    @field_validator('mode')
    def check_mode(cls, v):
        if v not in ['forward', 'copy']:
            raise ValueError("forwarding.mode 必须是 'forward' 或 'copy'")
        return v

class AdFilterConfig(BaseModel):
    enable: bool = True
    # (新) 修复问题1：允许列表为 None (当 yaml 中注释掉所有条目时)
    keywords: Optional[List[str]] = Field(default_factory=list)
    patterns: Optional[List[str]] = Field(default_factory=list)

class ContentFilterConfig(BaseModel):
    enable: bool = True
    # (新) 修复问题1：允许列表为 None
    meaningless_words: Optional[List[str]] = Field(default_factory=list)
    min_meaningful_length: int = 5

class WhitelistConfig(BaseModel):
    enable: bool = False
    # (新) 修复问题1：允许列表为 None
    keywords: Optional[List[str]] = Field(default_factory=list)

class DeduplicationConfig(BaseModel):
    enable: bool = True
    db_path: str = "/app/data/dedup_db.json" 

class LinkExtractionConfig(BaseModel):
    check_hyperlinks: bool = True
    check_bots: bool = True

class LinkCheckerConfig(BaseModel):
    enabled: bool = False
    mode: str = "log" 
    schedule: str = "0 3 * * *" 
    
    @field_validator('mode')
    def check_mode(cls, v):
        if v not in ['log', 'edit', 'delete']:
            raise ValueError("link_checker.mode 必须是 'log', 'edit', 或 'delete'")
        return v

class BotServiceConfig(BaseModel):
    enabled: bool = False
    bot_token: str = "YOUR_BOT_TOKEN_HERE" 
    admin_user_ids: List[int] 
    
    @field_validator('bot_token', mode='before')
    def check_bot_token(cls, v, info: Any):
        values = info.data
        if values.get('enabled') and (not v or v == "YOUR_BOT_TOKEN_HERE"):
            raise ValueError("Bot 服务已启用，但 bot_token 未设置。")
        return v
    
    @field_validator('admin_user_ids', mode='before')
    def check_admin_ids(cls, v, info: Any):
        values = info.data
        if values.get('enabled') and (not v):
            raise ValueError("Bot 服务已启用，但 admin_user_ids 列表为空。")
        return v

class Config(BaseModel):
    docker_container_name: Optional[str] = "tg-forwarder"
    proxy: Optional[ProxyConfig] = Field(default_factory=ProxyConfig)
    accounts: List[AccountConfig]
    sources: List[SourceConfig]
    targets: TargetConfig
    forwarding: ForwardingConfig = Field(default_factory=ForwardingConfig)
    ad_filter: AdFilterConfig = Field(default_factory=AdFilterConfig)
    content_filter: ContentFilterConfig = Field(default_factory=ContentFilterConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)
    deduplication: DeduplicationConfig = Field(default_factory=DeduplicationConfig)
    link_extraction: LinkExtractionConfig = Field(default_factory=LinkExtractionConfig)
    # (新) 修复问题1：允许 replacements 为 None
    replacements: Optional[Dict[str, str]] = Field(default_factory=dict)
    link_checker: Optional[LinkCheckerConfig] = Field(default_factory=LinkCheckerConfig)
    bot_service: Optional[BotServiceConfig] = Field(default_factory=BotServiceConfig) 
    
# --- 核心转发器类 ---

class UltimateForwarder:
    docker_container_name: str = "tg-forwarder"

    def __init__(self, config: Config, clients: List[TelegramClient]):
        self.config = config
        self.clients = clients
        self.current_client_index = 0
        self.client_flood_wait: Dict[str, float] = {} 
        
        self.dedup_db: Set[str] = self._load_dedup_db()
        self.progress_db: Dict[str, int] = self._load_progress_db()

        # (新) 修复问题1：在使用列表前检查 None
        ad_filter = config.ad_filter
        self.ad_patterns = self._compile_patterns(ad_filter.patterns if ad_filter and ad_filter.patterns else [])
        # (新) 修复问题3：全词匹配
        self.ad_keyword_patterns = self._compile_word_patterns(ad_filter.keywords if ad_filter and ad_filter.keywords else [])
        
        logger.info(f"终极转发器核心已初始化。")
        logger.info(f"转发模式: {config.forwarding.mode}")
        logger.info(f"处理新消息: {config.forwarding.forward_new_only}")
        logger.info(f"去重数据库: {len(self.dedup_db)} 条记录")
        logger.info(f"进度数据库: {len(self.progress_db)} 个频道")
    
    async def reload(self, new_config: Config):
        """(新) 热重载配置"""
        self.config = new_config
        # (新) 修复问题1：在使用列表前检查 None
        new_ad_filter = new_config.ad_filter
        self.ad_patterns = self._compile_patterns(new_ad_filter.patterns if new_ad_filter and new_ad_filter.patterns else [])
        # (新) 修复问题3：全词匹配
        self.ad_keyword_patterns = self._compile_word_patterns(new_ad_filter.keywords if new_ad_filter and new_ad_filter.keywords else [])
        await self.resolve_targets() # 确保目标被重新解析
        logger.info("转发器配置已热重载。")

    async def resolve_targets(self):
        """(新) 解析所有目标标识符"""
        if not self.clients:
            logger.error("无可用客户端，无法解析目标。")
            return
            
        client = self.clients[0]
        
        # (新) 修复问题4：为所有目标应用 ID 规范化
        async def normalize_target(identifier: Union[str, int]) -> Optional[int]:
            try:
                entity = await client.get_entity(identifier)
                resolved_id = entity.id
                
                if isinstance(entity, Channel):
                    if not str(resolved_id).startswith("-100"):
                        resolved_id = int(f"-100{resolved_id}")
                elif isinstance(entity, Chat):
                    if not str(resolved_id).startswith("-"):
                        resolved_id = int(f"-{resolved_id}")
                
                logger.info(f"目标 '{identifier}' -> 解析为 ID: {resolved_id}")
                return resolved_id
            except Exception as e:
                logger.error(f"❌ 无法解析目标: {identifier} - {e}")
                return None
        
        # 规范化默认目标
        self.config.targets.resolved_default_target_id = await normalize_target(self.config.targets.default_target)

        # 规范化规则目标
        for rule in self.config.targets.distribution_rules:
            rule.resolved_target_id = await normalize_target(rule.target_identifier)


    # --- 数据库/状态管理 ---
    
    def _get_progress_db_path(self) -> str:
        return "/app/data/forwarder_progress.json"

    # (新) 修复问题1：立即创建文件
    def _save_db_data(self, path: str, data: Any):
        """(新) 封装保存逻辑"""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"保存数据库 {path} 失败: {e}")

    def _load_progress_db(self) -> Dict[str, int]:
        path = self._get_progress_db_path()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning(f"未找到进度文件 {path}，将创建新的。")
            db = {}
            self._save_db_data(path, db) # (新) 立即创建
            return db

    def _save_progress_db(self):
        path = self._get_progress_db_path()
        self._save_db_data(path, self.progress_db)

    def _get_channel_progress(self, channel_id: int) -> int:
        return self.progress_db.get(str(channel_id), 0)

    def _set_channel_progress(self, channel_id: int, message_id: int):
        current_progress = self.progress_db.get(str(channel_id), 0)
        if message_id > current_progress:
            self.progress_db[str(channel_id)] = message_id
            self._save_progress_db()

    def _load_dedup_db(self) -> Set[str]:
        path = self.config.deduplication.db_path
        try:
            with open(path, 'r', encoding='utf-8') as f:
                hashes = json.load(f)
                return set(hashes)
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning(f"未找到去重文件 {path}，将创建新的。")
            db = set()
            self._save_db_data(path, list(db)) # (新) 立即创建
            return db

    def _save_dedup_db(self):
        path = self.config.deduplication.db_path
        self._save_db_data(path, list(self.dedup_db))

    # --- 客户端管理 ---
    
    def _get_next_client(self) -> TelegramClient:
        start_index = self.current_client_index
        
        while True:
            client = self.clients[self.current_client_index]
            # --- (新) 核心修复 ---
            # 使用我们附加的 session_name 作为唯一键
            client_key = client.session_name_for_forwarder
            
            wait_until = self.client_flood_wait.get(client_key, 0)
            
            if time.time() > wait_until:
                self.current_client_index = (self.current_client_index + 1) % len(self.clients)
                return client
            
            self.current_client_index = (self.current_client_index + 1) % len(self.clients)
            
            if self.current_client_index == start_index:
                # (新) 修复: 使用 session_name_for_forwarder
                all_wait_times = [self.client_flood_wait.get(c.session_name_for_forwarder, 0) for c in self.clients]
                min_wait_time = min(all_wait_times) if all_wait_times else time.time()
                sleep_duration = max(1.0, (min_wait_time - time.time()) + 1.0) 
                
                logger.warning(f"所有 {len(self.clients)} 个客户端都在 FloodWait。等待 {sleep_duration:.1f} 秒...")
                time.sleep(sleep_duration) 
                
                start_index = self.current_client_index
                continue

    async def _handle_send_error(self, e: Exception, client: TelegramClient):
        # --- (新) 核心修复 ---
        client_key = client.session_name_for_forwarder
        client_name = client_key # 它本身就是 session_name，很适合日志

        if isinstance(e, errors.FloodWaitError):
            wait_time = e.seconds + 5 
            logger.warning(f"客户端 {client_name} 触发 FloodWait: {wait_time} 秒。")
            # (新) 修复: 使用 client_key 作为字典键
            self.client_flood_wait[client_key] = time.time() + wait_time
        elif isinstance(e, errors.ChatWriteForbiddenError):
            logger.error(f"客户端 {client_name} 无法写入目标频道 (权限不足)。")
        elif isinstance(e, errors.UserBannedInChannelError):
            logger.error(f"客户端 {client_name} 已被目标频道封禁。")
        else:
            logger.error(f"客户端 {client_name} 转发时遇到未知错误: {e}")

    # --- 消息处理流水线 (Process Pipeline) ---

    async def process_message(self, event: events.NewMessage.Event, all_messages_in_group: Optional[List[Message]] = None):
        """
        (新) 修复问题2：添加 all_messages_in_group
        """
        message = event.message
        
        # (新) 修复：重写 chat_id 解析逻辑以防止崩溃
        if isinstance(event.chat_id, (int)):
            # 如果 event.chat_id 已经是一个整数 (如 -1002400173595)，
            # 直接使用它，跳过 get_peer_id()，因为它可能在这种情况下崩溃
            numeric_chat_id = event.chat_id
        else:
            try:
                 # 否则 (如果它是一个 PeerChannel/PeerChat 对象), 
                 # 我们才使用 get_peer_id() 来解析
                 numeric_chat_id = events.utils.get_peer_id(event.chat_id)
            except Exception:
                 # 这个日志现在只会在 Peer 对象*真的*无法解析时触发
                 logger.warning(f"收到来自未知类型源 {event.chat_id} 的消息，已忽略。")
                 return
        
        # (新) 规范化保险：确保频道 ID 总是 -100...
        # 无论它是来自 get_peer_id 还是原始 int
        if numeric_chat_id > 1000000000 and not str(numeric_chat_id).startswith("-100"):
            numeric_chat_id = int(f"-100{numeric_chat_id}")
        
        # --- (修复结束) ---

        # --- (新) 修复问题3：源匹配逻辑 ---
        source_config = None
        
        for s in self.config.sources:
            # (新) 修复：直接比较解析后的数字 ID
            if s.resolved_id == numeric_chat_id:
                source_config = s
                break
        # --- 修复结束 ---
        
        if not source_config:
             logger.warning(f"收到来自未配置源 {numeric_chat_id} 的消息，已忽略。")
             return
            
        logger.debug(f"--- [START] 正在处理消息 {numeric_chat_id}/{message.id} ---")

        try:
            # (新) 修复问题2：提取逻辑现在只处理主消息
            # messages_to_process = await self._extract_links(message, source_config)
            # (旧)
            
            # (新) 我们只处理主消息 (event.message)
            # (新) 修复：移除 entities (Markdown 格式)
            msg_data = {
                "text": message.text or "",
                "media": message.media,
                # "entities": message.entities, # <-- (旧) 导致 'entities' 错误的根源
                "hash_source": message.id
            }

            # for msg_data in messages_to_process: # (旧)
            if True: # (新) 替换 for 循环
                
                # 1. (新) 先用 *原始* 文本进行过滤
                # (新) 修复问题3：获取详细的过滤原因
                filter_reason = self._should_filter(msg_data['text'], msg_data['media'])
                if filter_reason:
                    logger.info(f"消息 {numeric_chat_id}/{message.id} (Text: {msg_data['text'][:30]}...) [被过滤: {filter_reason}]")
                    return # (新) 修复问题2：如果是相册，我们只检查一次，直接返回

                # 2. (新) 再用 *原始* 消息进行去重
                if self._is_duplicate(msg_data, f"{numeric_chat_id}/{message.id}"):
                    logger.info(f"消息 {numeric_chat_id}/{message.id} (Text: {msg_data['text'][:30]}...) [重复]")
                    return # (新) 修复问题2：直接返回

                # 3. (新) 再用 *原始* 文本查找目标
                target_id, topic_id = self._find_target(msg_data['text'], msg_data['media'])
                
                if not target_id:
                    # (新) 修复问题4：添加日志以显示解析失败
                    logger.error(f"消息 {numeric_chat_id}/{message.id} 无法找到有效的目标 ID。请检查配置或启动日志中的解析错误。")
                    return # (新) 修复问题2：直接返回

                # 4. (新) 仅在消息确定要发送时，才应用替换
                msg_data['text'] = self._apply_replacements(msg_data['text']) # <-- (新位置)

                logger.info(f"消息 {numeric_chat_id}/{message.id} [将被发送] -> 目标 {target_id}/(Topic:{topic_id})")
                
                # (新) 修复问题2：决定是发送单个消息还是整个相册
                messages_to_send = all_messages_in_group if all_messages_in_group else message

                await self._send_message(
                    original_message=messages_to_send, # (新)
                    message_data=msg_data,
                    target_id=target_id,
                    topic_id=topic_id
                )
                
                self._mark_as_processed(msg_data)
                
        except Exception as e:
            logger.error(f"处理消息 {numeric_chat_id}/{message.id} 时发生严重错误: {e}", exc_info=True)
        finally:
            logger.debug(f"--- [END] 消息 {numeric_chat_id}/{message.id} 处理完毕 ---")
            self._set_channel_progress(numeric_chat_id, message.id)

    async def process_history(self, resolved_source_ids: List[int]):
        """处理历史消息 (仅在 `forward_new_only: false` 时调用)"""
        client = self._get_next_client() 
        
        for source_id in resolved_source_ids:
            source_config = None
            entity = None
            try:
                # (新) 修复：确保 source_id 是 Telethon 接受的 Peer
                peer = await client.get_input_entity(source_id)
                entity = await client.get_entity(peer)
                
                # (新) 修复问题3：使用解析后的 ID 查找配置
                source_config = next((s for s in self.config.sources if s.resolved_id == source_id), None)
                     
            except Exception as e:
                logger.error(f"历史记录：无法获取实体 {source_id}: {e}")
                continue

            if not source_config:
                logger.error(f"历史记录：无法找到 {source_id} 的配置，跳过。")
                continue
            
            process_history = not self.config.forwarding.forward_new_only
            if source_config.forward_new_only is not None:
                process_history = not source_config.forward_new_only
                
            if not process_history:
                logger.info(f"跳过源 {source_config.identifier} 的历史记录 (已在源或全局配置中禁用)。")
                continue

            last_id = self._get_channel_progress(source_id)
            logger.info(f"正在扫描源 {source_config.identifier} ({source_id}) 的历史记录 (从消息 ID {last_id} 开始)...")
            
            try:
                # (新) 使用 peer
                async for message in client.iter_messages(peer, offset_id=last_id, reverse=True, limit=None):
                    
                    # (新) 修复：peer_chat 和 chat_id 需要正确设置
                    event_chat_id = message.chat_id
                    
                    fake_event = events.NewMessage.Event(message=message) # (新) 修复：使用最小构造函数
                    
                    # (新) 模拟 event 对象的属性
                    fake_event.chat_id = event_chat_id
                    if not fake_event.chat:
                        fake_event.chat = entity
                        
                    # (新) 修复：历史记录不支持相册，直接处理
                    await self.process_message(fake_event)
                    
            except Exception as e:
                logger.error(f"扫描源 {source_config.identifier} 历史记录时失败: {e}")
                
            logger.info(f"源 {source_config.identifier} 历史记录扫描完成。")


    # --- 流水线步骤 (Pipeline Steps) ---

    async def _extract_links(self, message: Any, config: SourceConfig) -> List[Dict[str, Any]]:
        results = []
        main_text = message.text or ""
        
        results.append({
            "text": main_text,
            "media": message.media,
            "hash_source": message.id 
        })

        # TODO: 实现 tgforwarder 的高级链接提取 (check_hyperlinks, check_bots, check_replies)
        
        return results

    def _apply_replacements(self, text: str) -> str:
        # (新) 修复问题1：在函数开头检查 None
        if not text or not self.config.replacements:
            return text
        
        for find, replace_with in self.config.replacements.items():
            text = text.replace(find, replace_with) 
            
        return text

    def _compile_patterns(self, patterns: List[str]) -> List[re.Pattern]:
        compiled = []
        for p in patterns:
            try:
                compiled.append(re.compile(p, re.IGNORECASE))
            except re.error as e:
                logger.warning(f"无效的正则表达式: '{p}', 错误: {e}")
        return compiled

    # (新) 修复问题3：全词匹配
    def _compile_word_patterns(self, keywords: List[str]) -> List[re.Pattern]:
        """将关键词列表编译为全词匹配的正则表达式列表"""
        compiled = []
        for kw in keywords:
            try:
                # \b 确保是单词边界 (全词匹配)
                pattern = r'\b' + re.escape(kw) + r'\b'
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as e:
                logger.warning(f"无效的广告关键词: '{kw}', 错误: {e}")
        return compiled

    def _should_filter(self, text: str, media: Any) -> Optional[str]: # (新) 修复问题3：返回 Optional[str]
        text = text or ""
        text_lower = text.lower()
        
        # 1. 白名单 (最高优先级)
        if self.config.whitelist and self.config.whitelist.enable:
            # (新) 修复问题1：在使用列表前检查 None
            whitelist_keywords = self.config.whitelist.keywords if self.config.whitelist.keywords else []
            if not any(kw.lower() in text_lower for kw in whitelist_keywords):
                logger.debug(f"Filter [Whitelist]: 未命中白名单。")
                return "Whitelist (未命中)" # (新) 修复问题3
            else:
                logger.debug(f"Filter [Whitelist]: 命中白名单，通过。")
                return None # (新) 修复问题3

        # 2. 广告过滤 (黑名单)
        if self.config.ad_filter and self.config.ad_filter.enable:
            # (新) 修复问题3：使用全词匹配
            for p in self.ad_keyword_patterns:
                if p.search(text):
                    logger.debug(f"Filter [Ad Keyword]: 命中广告关键词 {p.pattern}。")
                    return f"Blacklist (关键词: {p.pattern})" # (新) 修复问题3
            for p in self.ad_patterns:
                if p.search(text):
                    logger.debug(f"Filter [Ad Pattern]: 命中广告正则 {p.pattern}。")
                    return f"Blacklist (正则: {p.pattern})" # (新) 修复问题3

        # 3. 内容质量过滤 (黑名单)
        if self.config.content_filter and self.config.content_filter.enable:
            if not text and not media:
                logger.debug(f"Filter [Content]: 既无文本也无媒体。")
                return "Content Filter (空消息)" # (新) 修复问题3
            
            # (新) 修复问题1：在使用列表前检查 None
            meaningless = [w.lower() for w in self.config.content_filter.meaningless_words] if self.config.content_filter.meaningless_words else []
            if text_lower in meaningless:
                logger.debug(f"Filter [Content]: 命中无意义词汇。")
                return "Content Filter (无意义词汇)" # (新) 修复问题3
                
            if not media and len(text.strip()) < self.config.content_filter.min_meaningful_length:
                logger.debug(f"Filter [Content]: 文本过短且无媒体。")
                return f"Content Filter (文本过短: {len(text.strip())} < {self.config.content_filter.min_meaningful_length})" # (新) 修复问题3

        return None # (新) 修复问题3：默认通过

    def _get_message_hash(self, message_data: Dict[str, Any]) -> Optional[str]:
        if not self.config.deduplication.enable:
            return None
            
        media = message_data.get('media')
        if media:
            if hasattr(media, 'photo'):
                return f"photo:{media.photo.id}"
            if hasattr(media, 'document'):
                doc_size = getattr(media.document, 'size', '0')
                return f"doc:{media.document.id}:{doc_size}"
        
        text = message_data.get('text', "")
        if len(text) > 50: 
            return f"text:{hash(text)}"
            
        hash_source = message_data.get('hash_source')
        if hash_source:
            return f"id:{hash_source}"
        
        return None

    def _is_duplicate(self, message_data: Dict[str, Any], log_id: str) -> bool:
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
        if not self.config.deduplication.enable:
            return
            
        # (新) 修复问题2：修复 'message_G_data' 拼写错误
        msg_hash = self._get_message_hash(message_data)
        if msg_hash:
            self.dedup_db.add(msg_hash)
            self._save_db_db()

    def _find_target(self, text: str, media: Any) -> Tuple[Optional[int], Optional[int]]:
        """
        (新) 查找目标。
        返回 (resolved_target_id, topic_id)
        """
        rules = self.config.targets.distribution_rules
        if rules:
            for rule in rules:
                if rule.check(text, media): 
                    logger.debug(f"命中分发规则: '{rule.name}'")
                    if not rule.resolved_target_id:
                        logger.warning(f"规则 '{rule.name}' 命中，但其目标 {rule.target_identifier} 无法解析或无效，跳过。")
                        continue
                    return rule.resolved_target_id, rule.topic_id
                    
        logger.debug("未命中分发规则，使用默认目标。")
        # (新) 修复问题4：返回 default_topic_id
        return self.config.targets.resolved_default_target_id, self.config.targets.default_topic_id

    async def _send_message(self, original_message: Union[Message, List[Message]], message_data: Dict[str, Any], target_id: int, topic_id: Optional[int]):
        """
        (新) 修复问题2：original_message 现在可以是单个 Message 或一个列表
        """
        text = message_data['text']
        # (新) 修复：移除 entities (Markdown)
        # entities = message_data.get('entities') # <-- (旧)
        mode = self.config.forwarding.mode
        
        send_kwargs = {}
        # (新) 修复问题1： 'reply_to' 是用于话题的正确参数
        if topic_id:
            # 两种模式都使用 reply_to
            send_kwargs["reply_to"] = topic_id

        while True:
            client = self._get_next_client()
            try:
                if mode == 'copy':
                    # (新) 修复：'copy' 模式的正确实现 (Markdown)
                    
                    files_to_send = None
                    if isinstance(original_message, list):
                        # 这是一个相册，发送媒体列表
                        files_to_send = [msg.media for msg in original_message if msg.media]
                    elif isinstance(original_message, Message):
                        # 这是一个单条消息
                        files_to_send = original_message.media # 使用它自己的媒体
                    
                    # (新) 修复 v5.1：智能 Markdown 渲染
                    if files_to_send:
                        # 1. 有媒体文件：发送时不使用 parse_mode，避免崩溃
                        await client.send_message(
                            target_id,
                            message=text,            # 替换后的文本
                            file=files_to_send,      # 媒体
                            # 不带 parse_mode 或 entities
                            **send_kwargs
                        )
                    else:
                        # 2. 纯文本：安全地使用 parse_mode='md' 来渲染链接
                        await client.send_message(
                            target_id,
                            message=text,            # 替换后的文本
                            file=None,               # 确保 file 为 None
                            parse_mode='md',         # (新) 渲染 Markdown
                            **send_kwargs
                        )
                else:
                    # (新) 修复问题2：转发模式天然支持列表
                    await client.forward_messages(
                        target_id,
                        messages=original_message, # (新)
                        **send_kwargs
                    )
                
                # (新) 修复问题1：使用 'session_name_for_forwarder' 修复刷屏 Bug
                logger.debug(f"客户端 {client.session_name_for_forwarder} 发送成功。")
                return 

            except Exception as e:
                # (新) 修复问题4：现在 target_id 应该是正确的 (-100...)，
                # (新) 修复问题1： 'reply_to' 是正确的参数
                if isinstance(e, TypeError) and "unexpected keyword argument" in str(e):
                    logger.error(f"客户端 {client.session_name_for_forwarder} 转发时遇到内部代码错误: {e}")
                    logger.error(f"这通常意味着目标 {target_id} (话题: {topic_id}) 与转发模式 {mode} 不兼容。")
                    logger.warning("将尝试不带 topic_id 转发...")
                    try:
                        # 尝试不带 topic_id 再次发送
                        if mode == 'copy':
                            # (新) 修复 v5.1：在重试时也应用智能 Markdown
                            if files_to_send:
                                # 1. 有媒体：不带 parse_mode
                                await client.send_message(
                                    target_id, 
                                    message=text, 
                                    file=files_to_send
                                )
                            else:
                                # 2. 纯文本：带 parse_mode
                                await client.send_message(
                                    target_id, 
                                    message=text, 
                                    file=None,
                                    parse_mode='md'
                                )
                        else:
                            await client.forward_messages(target_id, messages=original_message)
                        return # 重试成功
                    except Exception as e2:
                        logger.error(f"不带 topic_id 重试失败: {e2}")
                        await self._handle_send_error(e2, client) # 处理重试的错误
                        return # (新) 修复问题3：停止无限循环
                else:
                    await self._handle_send_error(e, client)
                    return # (新) 修复问题3：停止无限循环 (针对非 TypeError)