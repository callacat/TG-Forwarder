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

# (新) v8.4：导入 web_server 以访问内存中的规则
import web_server

from typing import List, Optional, Tuple, Dict, Set, Any, Union 

# (新) v8.5：从 models.py 导入
from models import Config

logger = logging.getLogger(__name__)

# --- 核心转发器类 ---

class UltimateForwarder:
    docker_container_name: str = "tg-forwarder"

    def __init__(self, config: Config, clients: List[TelegramClient]):
        # (新) v8.4：self.config 只保留静态配置
        self.config = config
        self.clients = clients
        self.current_client_index = 0
        self.client_flood_wait: Dict[str, float] = {} 
        
        # (新) v8.4：从 web_server.rules_db (内存) 初始化规则
        ad_filter = web_server.rules_db.ad_filter
        self.ad_patterns = self._compile_patterns(ad_filter.patterns if ad_filter and ad_filter.patterns else [])
        self.ad_keyword_word_patterns = self._compile_word_patterns(ad_filter.keywords_word if ad_filter and ad_filter.keywords_word else [])
        
        logger.info(f"终极转发器核心已初始化。")
        logger.info(f"转发模式: {config.forwarding.mode}")
        logger.info(f"处理新消息: {config.forwarding.forward_new_only}")
    
    async def reload(self, new_config: Config):
        """热重载配置"""
        # 1. 重载静态配置
        self.config = new_config
        
        # 2. (新) v8.4：从 web_server.rules_db (已重载) 重新加载规则
        new_ad_filter = web_server.rules_db.ad_filter
        self.ad_patterns = self._compile_patterns(new_ad_filter.patterns if new_ad_filter and new_ad_filter.patterns else [])
        self.ad_keyword_word_patterns = self._compile_word_patterns(new_ad_filter.keywords_word if new_ad_filter and new_ad_filter.keywords_word else [])
        
        # 3. (新) v8.4：重新解析目标
        await self.resolve_targets() 
        
        # 4. (新) v8.4：重载日志级别
        if new_config.logging_level:
            # (新) v8.5：修复循环导入
            from ultimate_forwarder import setup_logging
            setup_logging(new_config.logging_level.app, new_config.logging_level.telethon)
        
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
        
        # (新) v8.4：同时解析 config.yaml (旧) 和 rules_db (新) 中的目标
        
        # 1. 解析 config.yaml 中的目标 (用于迁移和向后兼容)
        self.config.targets.resolved_default_target_id = await normalize_target(self.config.targets.default_target)
        for rule in self.config.targets.distribution_rules:
            rule.resolved_target_id = await normalize_target(rule.target_identifier)
            
        # 2. (新) 解析 rules_db.json 中的目标
        for rule in web_server.rules_db.distribution_rules:
            rule.resolved_target_id = await normalize_target(rule.target_identifier)


    # (新) v9.0：重构数据库函数
    
    async def _get_channel_progress(self, channel_id: int) -> int:
        return await database.get_progress(channel_id)

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
        
        # (新) v8.4：从 rules_db (内存) 检查源
        source_config = None
        for s in web_server.rules_db.sources:
            if s.resolved_id == numeric_chat_id:
                source_config = s
                break
        
        if not source_config:
             logger.debug(f"收到来自未配置源 {numeric_chat_id} 的消息，已忽略。")
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

                if await self._is_duplicate(msg_data, f"{numeric_chat_id}/{message.id}"):
                    logger.info(f"消息 {numeric_chat_id}/{message.id} (Text: {msg_data['text'][:30]}...) [重复]")
                    return 

                target_id, topic_id = self._find_target(msg_data['text'], msg_data['media'])
                
                if not target_id:
                    logger.error(f"消息 {numeric_chat_id}/{message.id} 无法找到有效的目标 ID。请检查 Web UI 中的'转发规则'或 config.yaml 中的'default_target'。")
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
                
                await self._mark_as_processed(msg_data)
                
        except Exception as e:
            logger.error(f"处理消息 {numeric_chat_id}/{message.id} 时发生严重错误: {e}", exc_info=True)
        finally:
            logger.debug(f"--- [END] 消息 {numeric_chat_id}/{message.id} 处理完毕 ---")
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
                
                # (新) v8.4：从 config.yaml 查找
                source_config = next((s for s in self.config.sources if s.resolved_id == source_id), None)
                     
            except Exception as e:
                logger.error(f"历史记录：无法获取实体 {source_id}: {e}")
                continue

            if not source_config:
                logger.error(f"历史记录：无法找到 {source_id} 的配置 (config.yaml)，跳过。")
                continue
            
            process_history = not self.config.forwarding.forward_new_only
            if source_config.forward_new_only is not None:
                process_history = not source_config.forward_new_only
                
            if not process_history:
                logger.info(f"跳过源 {source_config.identifier} 的历史记录 (已在源或全局配置中禁用)。")
                continue

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
        # (新) v8.4：从 config.yaml 读取 replacements
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
        
        # (新) v8.4：从 web_server.rules_db (内存) 读取规则
        whitelist = web_server.rules_db.whitelist
        ad_filter = web_server.rules_db.ad_filter
        content_filter = self.config.content_filter # (旧) content_filter 仍在 config.yaml
        
        # 1. 白名单 (最高优先级)
        if whitelist and whitelist.enable:
            whitelist_keywords = whitelist.keywords if whitelist.keywords else []
            if not any(kw.lower() in text_lower for kw in whitelist_keywords):
                logger.debug(f"Filter [Whitelist]: 未命中白名单。")
                return "Whitelist (未命中)" 
            else:
                logger.debug(f"Filter [Whitelist]: 命中白名单，通过。")
                return None 

        # 2. 广告过滤 (黑名单)
        if ad_filter and ad_filter.enable:
            ad_keywords_sub = ad_filter.keywords_substring if ad_filter.keywords_substring else []
            for kw in ad_keywords_sub:
                if kw.lower() in text_lower:
                    logger.debug(f"Filter [Ad Substring]: 命中广告关键词 {kw}。")
                    return f"Blacklist (关键词: {kw})"
            
            for p in self.ad_keyword_word_patterns:
                if p.search(text):
                    logger.debug(f"Filter [Ad Word]: 命中广告全词 {p.pattern}。")
                    return f"Blacklist (全词: {p.pattern})"
            
            file_keywords = ad_filter.file_name_keywords if ad_filter.file_name_keywords else []
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
        if content_filter and content_filter.enable:
            if not text and not media:
                logger.debug(f"Filter [Content]: 既无文本也无媒体。")
                return "Content Filter (空消息)" 
            
            meaningless = [w.lower() for w in content_filter.meaningless_words] if content_filter.meaningless_words else []
            if text_lower in meaningless:
                logger.debug(f"Filter [Content]: 命中无意义词汇。")
                return "Content Filter (无意义词汇)" 
                
            if not media and len(text.strip()) < content_filter.min_meaningful_length:
                logger.debug(f"Filter [Content]: 文本过短且无媒体。")
                return f"Content Filter (文本过短: {len(text.strip())} < {content_filter.min_meaningful_length})" 

        return None 

    def _get_message_hash(self, message_data: Dict[str, Any]) -> Optional[str]:
        # (新) v9.0：从 config.yaml 读取
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

    async def _is_duplicate(self, message_data: Dict[str, Any], log_id: str) -> bool:
        if not self.config.deduplication.enable:
            return False
            
        msg_hash = self._get_message_hash(message_data)
        if not msg_hash:
            logger.debug(f"无法为 {log_id} 生成哈希，跳过。")
            return False
            
        return await database.check_hash(msg_hash)

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
        # (新) v8.4：从 web_server.rules_db (内存) 读取规则
        rules = web_server.rules_db.distribution_rules
        if rules:
            for rule in rules:
                if rule.check(text, media): 
                    logger.debug(f"命中分发规则: '{rule.name}'")
                    if not rule.resolved_target_id:
                        logger.warning(f"规则 '{rule.name}' 命中，但其目标 {rule.target_identifier} 无法解析或无效，跳过。")
                        continue
                    return rule.resolved_target_id, rule.topic_id
                    
        logger.debug("未命中分发规则，使用默认目标。")
        # (新) v8.4：从 config.yaml 读取默认目标
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