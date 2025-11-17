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
from telethon.tl.types import Message, MessageEntityTextUrl, MessageMediaDocument, PeerUser, PeerChat, PeerChannel
from telethon.tl.types import Channel, Chat
from telethon.tl.types import MessageMediaWebPage

# (新) v9.0：导入 database
import database

# --- 类型提示 ---
from typing import List, Optional, Tuple, Dict, Set, Any, Union 
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

logger = logging.getLogger(__name__)

# --- 日志配置模型 ---
class LoggingLevelConfig(BaseModel):
    app: str = "INFO"
    telethon: str = "WARNING"

# (新) v8.1：Web UI 配置模型
class WebUIConfig(BaseModel):
    password: str = "password"

# --- 配置模型 ---

class ProxyConfig(BaseModel):
    enabled: bool = False
    proxy_type: str = "socks5"
    addr: str = "127.0.0.1"
    port: int = 1080
    username: Optional[str] = None
    password: Optional[str] = None
    
    def get_telethon_proxy(self):
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
    resolved_id: Optional[int] = Field(None, exclude=True) 

class TargetDistributionRule(BaseModel):
    name: str 
    all_keywords: List[str] = Field(default_factory=list)
    any_keywords: List[str] = Field(default_factory=list)
    file_types: List[str] = Field(default_factory=list) 
    file_name_patterns: List[str] = Field(default_factory=list) 

    target_identifier: Union[int, str]
    topic_id: Optional[int] = None 
    
    resolved_target_id: Optional[int] = Field(None, exclude=True)
    
    def check(self, text: str, media: Any) -> bool:
        text_lower = text.lower() if text else ""
        
        # 1. 检查 [AND] all_keywords
        if self.all_keywords:
            if not all(kw.lower() in text_lower for kw in self.all_keywords):
                return False 
        
        # 2. 检查 [OR] 条件组
        or_group_matched = False
        
        if self.any_keywords:
            if any(keyword.lower() in text_lower for keyword in self.any_keywords):
                or_group_matched = True
        
        if not or_group_matched and media and isinstance(media, MessageMediaDocument):
            doc = media.document
            if doc:
                if self.file_types and doc.mime_type:
                    if any(ft.lower() in doc.mime_type.lower() for ft in self.file_types):
                        or_group_matched = True

                if not or_group_matched and self.file_name_patterns:
                    file_name = next((attr.file_name for attr in doc.attributes if hasattr(attr, 'file_name')), None)
                    if file_name:
                        for pattern_str in self.file_name_patterns:
                            try:
                                pattern = re.compile(re.escape(pattern_str).replace(r'\*', r'.*'), re.IGNORECASE)
                                if re.search(pattern, file_name):
                                    or_group_matched = True
                                    break 
                            except re.error:
                                logger.warning(f"规则 '{self.name}' 中的文件名模式 '{pattern_str}' 无效")
        
        # 3. 最终逻辑判断
        has_all_keywords = bool(self.all_keywords)
        has_or_group = bool(self.any_keywords or self.file_types or self.file_name_patterns)

        if has_all_keywords and not has_or_group:
            return True
        elif not has_all_keywords and has_or_group:
            return or_group_matched
        elif has_all_keywords and has_or_group:
            return or_group_matched
        else:
            return False

class TargetConfig(BaseModel):
    default_target: Union[int, str]
    default_topic_id: Optional[int] = None 
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    
    resolved_default_target_id: Optional[int] = Field(None, exclude=True)


class ForwardingConfig(BaseModel):
    mode: str = "forward" 
    forward_new_only: bool = True 
    mark_as_read: bool = False
    mark_target_as_read: bool = False 
    
    @field_validator('mode')
    def check_mode(cls, v):
        if v not in ['forward', 'copy']:
            raise ValueError("forwarding.mode 必须是 'forward' 或 'copy'")
        return v

class AdFilterConfig(BaseModel):
    enable: bool = True
    keywords_substring: Optional[List[str]] = Field(default_factory=list)
    keywords_word: Optional[List[str]] = Field(default_factory=list)
    patterns: Optional[List[str]] = Field(default_factory=list)
    file_name_keywords: Optional[List[str]] = Field(default_factory=list)

class ContentFilterConfig(BaseModel):
    enable: bool = True
    meaningless_words: Optional[List[str]] = Field(default_factory=list)
    min_meaningful_length: int = 5

class WhitelistConfig(BaseModel):
    enable: bool = False
    keywords: Optional[List[str]] = Field(default_factory=list)

class DeduplicationConfig(BaseModel):
    enable: bool = True
    # (新) v9.0：db_path 已被 database.py 取代，但我们保留它以兼容旧配置
    db_path: Optional[str] = "/app/data/dedup_db.json" 

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
    logging_level: Optional[LoggingLevelConfig] = Field(default_factory=LoggingLevelConfig)
    web_ui: Optional[WebUIConfig] = Field(default_factory=WebUIConfig) # (新) v8.1
    
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
        
        # (新) v9.0：移除 dedup_db 和 progress_db 的内存加载
        # self.dedup_db: Set[str] = self._load_dedup_db()
        # self.progress_db: Dict[str, int] = self._load_progress_db()

        ad_filter = config.ad_filter
        self.ad_patterns = self._compile_patterns(ad_filter.patterns if ad_filter and ad_filter.patterns else [])
        self.ad_keyword_word_patterns = self._compile_word_patterns(ad_filter.keywords_word if ad_filter and ad_filter.keywords_word else [])
        
        logger.info(f"终极转发器核心已初始化。")
        logger.info(f"转发模式: {config.forwarding.mode}")
        logger.info(f"处理新消息: {config.forwarding.forward_new_only}")
    
    async def reload(self, new_config: Config):
        """热重载配置"""
        self.config = new_config
        new_ad_filter = new_config.ad_filter
        self.ad_patterns = self._compile_patterns(new_ad_filter.patterns if new_ad_filter and new_ad_filter.patterns else [])
        self.ad_keyword_word_patterns = self._compile_word_patterns(new_ad_filter.keywords_word if new_ad_filter and new_ad_filter.keywords_word else [])
        await self.resolve_targets() 
        
        # (新) v9.0：修复循环导入。日志重载由 ultimate_forwarder.py 的 reload_config_func 处理
        # if new_config.logging_level:
        #     from ultimate_forwarder import setup_logging
        #     setup_logging(new_config.logging_level.app, new_config.logging_level.telethon)
        
        logger.info("转发器规则已热重载。")

    async def resolve_targets(self):
        """解析所有目标标识符"""
        if not self.clients:
            logger.error("无可用客户端，无法解析目标。")
            return
            
        client = self.clients[0]
        
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
        
        self.config.targets.resolved_default_target_id = await normalize_target(self.config.targets.default_target)

        for rule in self.config.targets.distribution_rules:
            rule.resolved_target_id = await normalize_target(rule.target_identifier)


    # --- (新) v9.0：移除数据库/状态管理函数 ---
    # 移除 _get_progress_db_path, _save_db_data, _load_progress_db, 
    # _save_progress_db, _load_dedup_db, _save_dedup_db
    
    # (新) v9.0：修改 _get_channel_progress
    async def _get_channel_progress(self, channel_id: int) -> int:
        return await database.get_progress(channel_id)

    # (新) v9.0：修改 _set_channel_progress
    async def _set_channel_progress(self, channel_id: int, message_id: int):
        await database.set_progress(channel_id, message_id)


    # --- 客户端管理 ---
    
    def _get_next_client(self) -> TelegramClient:
        start_index = self.current_client_index
        
        while True:
            client = self.clients[self.current_client_index]
            client_key = client.session_name_for_forwarder
            
            wait_until = self.client_flood_wait.get(client_key, 0)
            
            if time.time() > wait_until:
                self.current_client_index = (self.current_client_index + 1) % len(self.clients)
                return client
            
            self.current_client_index = (self.current_client_index + 1) % len(self.clients)
            
            if self.current_client_index == start_index:
                all_wait_times = [self.client_flood_wait.get(c.session_name_for_forwarder, 0) for c in self.clients]
                min_wait_time = min(all_wait_times) if all_wait_times else time.time()
                sleep_duration = max(1.0, (min_wait_time - time.time()) + 1.0) 
                
                logger.warning(f"所有 {len(self.clients)} 个客户端都在 FloodWait。等待 {sleep_duration:.1f} 秒...")
                time.sleep(sleep_duration) 
                
                start_index = self.current_client_index
                continue

    async def _handle_send_error(self, e: Exception, client: TelegramClient):
        client_key = client.session_name_for_forwarder
        client_name = client_key 

        if isinstance(e, errors.FloodWaitError):
            wait_time = e.seconds + 5 
            logger.warning(f"客户端 {client_name} 触发 FloodWait: {wait_time} 秒。")
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
        处理单条消息或相册的主消息
        """
        message = event.message
        
        if isinstance(event.chat_id, (int)):
            numeric_chat_id = event.chat_id
        else:
            try:
                 numeric_chat_id = events.utils.get_peer_id(event.chat_id)
            except Exception:
                 logger.warning(f"收到来自未知类型源 {event.chat_id} 的消息，已忽略。")
                 return
        
        if numeric_chat_id > 1000000000 and not str(numeric_chat_id).startswith("-100"):
            numeric_chat_id = int(f"-100{numeric_chat_id}")
        
        source_config = None
        for s in self.config.sources:
            if s.resolved_id == numeric_chat_id:
                source_config = s
                break
        
        if not source_config:
             logger.warning(f"收到来自未配置源 {numeric_chat_id} 的消息，已忽略。")
             return
            
        logger.debug(f"--- [START] 正在处理消息 {numeric_chat_id}/{message.id} ---")

        try:
            msg_data = {
                "text": message.text or "",
                "media": message.media,
                "hash_source": message.id
            }

            if True: 
                filter_reason = self._should_filter(msg_data['text'], msg_data['media'])
                if filter_reason:
                    logger.info(f"消息 {numeric_chat_id}/{message.id} (Text: {msg_data['text'][:30]}...) [被过滤: {filter_reason}]")
                    return 

                # (新) v9.0：修改 _is_duplicate
                if await self._is_duplicate(msg_data, f"{numeric_chat_id}/{message.id}"):
                    logger.info(f"消息 {numeric_chat_id}/{message.id} (Text: {msg_data['text'][:30]}...) [重复]")
                    return 

                target_id, topic_id = self._find_target(msg_data['text'], msg_data['media'])
                
                if not target_id:
                    logger.error(f"消息 {numeric_chat_id}/{message.id} 无法找到有效的目标 ID。请检查配置或启动日志中的解析错误。")
                    return 

                msg_data['text'] = self._apply_replacements(msg_data['text']) 

                logger.info(f"消息 {numeric_chat_id}/{message.id} [将被发送] -> 目标 {target_id}/(Topic:{topic_id})")
                
                messages_to_send = all_messages_in_group if all_messages_in_group else message

                await self._send_message(
                    original_message=messages_to_send, 
                    message_data=msg_data,
                    target_id=target_id,
                    topic_id=topic_id
                )
                
                # (新) v9.0：修改 _mark_as_processed
                await self._mark_as_processed(msg_data)
                
        except Exception as e:
            logger.error(f"处理消息 {numeric_chat_id}/{message.id} 时发生严重错误: {e}", exc_info=True)
        finally:
            logger.debug(f"--- [END] 消息 {numeric_chat_id}/{message.id} 处理完毕 ---")
            # (新) v9.0：修改 _set_channel_progress
            await self._set_channel_progress(numeric_chat_id, message.id)

    async def process_history(self, resolved_source_ids: List[int]):
        """处理历史消息 (仅在 `forward_new_only: false` 时调用)"""
        client = self._get_next_client() 
        
        for source_id in resolved_source_ids:
            source_config = None
            entity = None
            try:
                peer = await client.get_input_entity(source_id)
                entity = await client.get_entity(peer)
                
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

            # (新) v9.0：修改 _get_channel_progress
            last_id = await self._get_channel_progress(source_id)
            logger.info(f"正在扫描源 {source_config.identifier} ({source_id}) 的历史记录 (从消息 ID {last_id} 开始)...")
            
            try:
                async for message in client.iter_messages(peer, offset_id=last_id, reverse=True, limit=None):
                    
                    event_chat_id = message.chat_id
                    
                    fake_event = events.NewMessage.Event(message=message) 
                    
                    fake_event.chat_id = event_chat_id
                    if not fake_event.chat:
                        fake_event.chat = entity
                        
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
        
        return results

    def _apply_replacements(self, text: str) -> str:
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

    def _compile_word_patterns(self, keywords: List[str]) -> List[re.Pattern]:
        """将关键词列表编译为全词匹配的正则表达式列表"""
        compiled = []
        for kw in keywords:
            try:
                pattern = r'\b' + re.escape(kw) + r'\b'
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as e:
                logger.warning(f"无效的广告关键词: '{kw}', 错误: {e}")
        return compiled

    def _should_filter(self, text: str, media: Any) -> Optional[str]: 
        text = text or ""
        text_lower = text.lower()
        
        # 1. 白名单 (最高优先级)
        if self.config.whitelist and self.config.whitelist.enable:
            whitelist_keywords = self.config.whitelist.keywords if self.config.whitelist.keywords else []
            if not any(kw.lower() in text_lower for kw in whitelist_keywords):
                logger.debug(f"Filter [Whitelist]: 未命中白名单。")
                return "Whitelist (未命中)" 
            else:
                logger.debug(f"Filter [Whitelist]: 命中白名单，通过。")
                return None 

        # 2. 广告过滤 (黑名单)
        if self.config.ad_filter and self.config.ad_filter.enable:
            ad_keywords_sub = self.config.ad_filter.keywords_substring if self.config.ad_filter.keywords_substring else []
            for kw in ad_keywords_sub:
                if kw.lower() in text_lower:
                    logger.debug(f"Filter [Ad Substring]: 命中广告关键词 {kw}。")
                    return f"Blacklist (关键词: {kw})"
            
            for p in self.ad_keyword_word_patterns:
                if p.search(text):
                    logger.debug(f"Filter [Ad Word]: 命中广告全词 {p.pattern}。")
                    return f"Blacklist (全词: {p.pattern})"
            
            file_keywords = self.config.ad_filter.file_name_keywords if self.config.ad_filter.file_name_keywords else []
            if file_keywords and media and isinstance(media, MessageMediaDocument):
                doc = media.document
                if doc:
                    file_name = next((attr.file_name for attr in doc.attributes if hasattr(attr, 'file_name')), None)
                    if file_name:
                        file_name_lower = file_name.lower()
                        for kw in file_keywords:
                            if kw.lower() in file_name_lower:
                                logger.debug(f"Filter [Ad Filename]: 命中文件名关键词 {kw}。")
                                return f"Blacklist (文件名: {kw})"

            for p in self.ad_patterns:
                if p.search(text):
                    logger.debug(f"Filter [Ad Pattern]: 命中广告正则 {p.pattern}。")
                    return f"Blacklist (正则: {p.pattern})" 

        # 3. 内容质量过滤 (黑名单)
        if self.config.content_filter and self.config.content_filter.enable:
            if not text and not media:
                logger.debug(f"Filter [Content]: 既无文本也无媒体。")
                return "Content Filter (空消息)" 
            
            meaningless = [w.lower() for w in self.config.content_filter.meaningless_words] if self.config.content_filter.meaningless_words else []
            if text_lower in meaningless:
                logger.debug(f"Filter [Content]: 命中无意义词汇。")
                return "Content Filter (无意义词汇)" 
                
            if not media and len(text.strip()) < self.config.content_filter.min_meaningful_length:
                logger.debug(f"Filter [Content]: 文本过短且无媒体。")
                return f"Content Filter (文本过短: {len(text.strip())} < {self.config.content_filter.min_meaningful_length})" 

        return None 

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

    # (新) v9.0：修改 _is_duplicate
    async def _is_duplicate(self, message_data: Dict[str, Any], log_id: str) -> bool:
        if not self.config.deduplication.enable:
            return False
            
        msg_hash = self._get_message_hash(message_data)
        if not msg_hash:
            logger.debug(f"无法为 {log_id} 生成哈希，跳过。")
            return False
            
        return await database.check_hash(msg_hash)

    # (新) v9.0：修改 _mark_as_processed
    async def _mark_as_processed(self, message_data: Dict[str, Any]):
        if not self.config.deduplication.enable:
            return
            
        msg_hash = self._get_message_hash(message_data)
        if msg_hash:
            await database.add_hash(msg_hash)

    def _find_target(self, text: str, media: Any) -> Tuple[Optional[int], Optional[int]]:
        """
        查找目标。
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
        return self.config.targets.resolved_default_target_id, self.config.targets.default_topic_id

    async def _send_message(self, original_message: Union[Message, List[Message]], message_data: Dict[str, Any], target_id: int, topic_id: Optional[int]):
        """
        发送消息，original_message 可以是单个 Message 或一个列表
        """
        text = message_data['text']
        mode = self.config.forwarding.mode
        
        send_kwargs = {}
        if topic_id:
            send_kwargs["reply_to"] = topic_id
            
        sent_message = None 

        while True:
            client = self._get_next_client()
            try:
                if mode == 'copy':
                    
                    media_to_send = None
                    is_real_file = False
                    
                    if isinstance(original_message, list):
                        media_to_send = [msg.media for msg in original_message if msg.media]
                        is_real_file = True 
                    elif isinstance(original_message, Message):
                        media = original_message.media
                        if media and not isinstance(media, MessageMediaWebPage):
                            is_real_file = True
                            media_to_send = media
                    
                    
                    if is_real_file:
                        sent_message = await client.send_message( 
                            target_id,
                            message=text,            
                            file=media_to_send,      
                            **send_kwargs
                        )
                    else:
                        sent_message = await client.send_message( 
                            target_id,
                            message=text,            
                            file=None,               
                            parse_mode='md',         
                            **send_kwargs
                        )
                else:
                    sent_message = await client.forward_messages( 
                        target_id,
                        messages=original_message, 
                        **send_kwargs
                    )
                
                if self.config.forwarding.mark_target_as_read and sent_message:
                    try:
                        last_message_id = 0
                        if isinstance(sent_message, list):
                            last_message_id = sent_message[-1].id
                        else:
                            last_message_id = sent_message.id
                        
                        await client.mark_read(
                            target_id, 
                            max_id=last_message_id,
                            top_msg_id=topic_id 
                        )
                    except Exception as e:
                        logger.debug(f"将目标 {target_id} (话题: {topic_id}) 标记为已读失败: {e}")

                logger.debug(f"客户端 {client.session_name_for_forwarder} 发送成功。")
                return 

            except Exception as e:
                if isinstance(e, TypeError) and "unexpected keyword argument" in str(e):
                    logger.error(f"客户端 {client.session_name_for_forwarder} 转发时遇到内部代码错误: {e}")
                    logger.error(f"这通常意味着目标 {target_id} (话题: {topic_id}) 与转发模式 {mode} 不兼容。")
                    logger.warning("将尝试不带 topic_id 转发...")
                    try:
                        # 尝试不带 topic_id 再次发送
                        sent_message_retry = None 
                        if mode == 'copy':
                            if is_real_file:
                                sent_message_retry = await client.send_message( 
                                    target_id, 
                                    message=text, 
                                    file=media_to_send
                                )
                            else:
                                sent_message_retry = await client.send_message( 
                                    target_id, 
                                    message=text, 
                                    file=None,
                                    parse_mode='md'
                                )
                        else:
                            sent_message_retry = await client.forward_messages(target_id, messages=original_message) 
                        
                        if self.config.forwarding.mark_target_as_read and sent_message_retry:
                            try:
                                last_message_id = 0
                                if isinstance(sent_message_retry, list):
                                    last_message_id = sent_message_retry[-1].id
                                else:
                                    last_message_id = sent_message_retry.id
                                
                                await client.mark_read(
                                    target_id, 
                                    max_id=last_message_id,
                                    top_msg_id=None 
                                )
                            except Exception as e:
                                logger.debug(f"将目标 {target_id} 标记为已读失败: {e}")

                        return # 重试成功
                    except Exception as e2:
                        logger.error(f"不带 topic_id 重试失败: {e2}")
                        await self._handle_send_error(e2, client) 
                        return # 停止无限循环
                else:
                    await self._handle_send_error(e, client)
                    return # 停止无限循环 (针对非 TypeError)