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

import database
import web_server

from typing import List, Optional, Tuple, Dict, Set, Any, Union 
from models import Config, SourceConfig

from loguru import logger

# --- 核心转发器类 ---

class UltimateForwarder:
    docker_container_name: str = "tg-forwarder"

    def __init__(self, config: Config, clients: List[TelegramClient]):
        self.config = config
        self.clients = clients
        self.current_client_index = 0
        self.client_flood_wait: Dict[str, float] = {} 
        
        # 初始化正则
        ad_filter = web_server.rules_db.ad_filter
        self.ad_patterns = self._compile_patterns(ad_filter.patterns if ad_filter and ad_filter.patterns else [])
        self.ad_keyword_word_patterns = self._compile_word_patterns(ad_filter.keywords_word if ad_filter and ad_filter.keywords_word else [])
        
        # 打印初始配置
        settings = web_server.rules_db.settings
        logger.info(f"终极转发器核心已初始化。")
        logger.info(f"转发模式: {settings.forwarding_mode}")
    
    async def reload(self, new_config: Config):
        self.config = new_config
        
        new_ad_filter = web_server.rules_db.ad_filter
        self.ad_patterns = self._compile_patterns(new_ad_filter.patterns if new_ad_filter and new_ad_filter.patterns else [])
        self.ad_keyword_word_patterns = self._compile_word_patterns(new_ad_filter.keywords_word if new_ad_filter and new_ad_filter.keywords_word else [])
        
        await self.resolve_targets() 
        
        if new_config.logging_level:
            from ultimate_forwarder import setup_logging
            setup_logging(new_config.logging_level.app, new_config.logging_level.telethon)
        
        logger.info("转发器规则已热重载。")

    async def resolve_targets(self):
        if not self.clients: return
        client = self.clients[0]
        
        async def normalize_target(identifier: Union[str, int]) -> Optional[int]:
            try:
                if not identifier: return None
                entity = await client.get_entity(identifier)
                resolved_id = entity.id
                if isinstance(entity, Channel) and not str(resolved_id).startswith("-100"):
                    resolved_id = int(f"-100{resolved_id}")
                elif isinstance(entity, Chat) and not str(resolved_id).startswith("-"):
                    resolved_id = int(f"-{resolved_id}")
                logger.info(f"目标 '{identifier}' -> 解析为 ID: {resolved_id}")
                return resolved_id
            except Exception as e:
                logger.error(f"❌ 无法解析目标: {identifier} - {e}")
                return None
        
        settings = web_server.rules_db.settings
        if settings.default_target:
            self.config.targets.resolved_default_target_id = await normalize_target(settings.default_target)
        
        for rule in web_server.rules_db.distribution_rules:
            rule.resolved_target_id = await normalize_target(rule.target_identifier)

    async def _get_channel_progress(self, channel_id: int) -> int:
        return await database.get_progress(channel_id)

    async def _set_channel_progress(self, channel_id: int, message_id: int):
        await database.set_progress(channel_id, message_id)

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
                time.sleep(5) 
                continue

    async def _handle_send_error(self, e: Exception, client: TelegramClient):
        client_key = client.session_name_for_forwarder
        if isinstance(e, errors.FloodWaitError):
            wait_time = e.seconds + 5 
            logger.warning(f"客户端 {client_key} 触发 FloodWait: {wait_time} 秒。")
            self.client_flood_wait[client_key] = time.time() + wait_time
        else:
            logger.error(f"客户端 {client_key} 错误: {e}")

    # --- 消息处理流水线 ---

    async def process_message(self, event: events.NewMessage.Event, all_messages_in_group: Optional[List[Message]] = None):
        message = event.message
        
        if isinstance(event.chat_id, (int)):
            numeric_chat_id = event.chat_id
        else:
            try:
                 numeric_chat_id = events.utils.get_peer_id(event.chat_id)
            except Exception:
                 return
        
        if numeric_chat_id > 1000000000 and not str(numeric_chat_id).startswith("-100"):
            numeric_chat_id = int(f"-100{numeric_chat_id}")
        
        source_config = None
        for s in web_server.rules_db.sources:
            if s.resolved_id == numeric_chat_id:
                source_config = s
                break
        
        if not source_config: return

        try:
            msg_data = {
                "text": message.text or "",
                "media": message.media,
                "hash_source": message.id
            }

            if self._should_filter(msg_data['text'], msg_data['media']):
                logger.info(f"消息 {message.id} 被过滤。")
                return 

            if await self._is_duplicate(msg_data, f"{numeric_chat_id}/{message.id}"):
                logger.info(f"消息 {message.id} 重复。")
                return 

            target_id, topic_id = self._find_target(msg_data['text'], msg_data['media'])
            
            if not target_id:
                logger.error(f"消息 {message.id} 无有效目标。")
                return 

            msg_data['text'] = self._apply_replacements(msg_data['text']) 
            messages_to_send = all_messages_in_group if all_messages_in_group else message

            await self._send_message(
                original_message=messages_to_send, 
                message_data=msg_data,
                target_id=target_id,
                topic_id=topic_id
            )
            
            await self._mark_as_processed(msg_data)
            
        except Exception as e:
            logger.error(f"处理消息失败: {e}", exc_info=True)
        finally:
            await self._set_channel_progress(numeric_chat_id, message.id)

    async def process_history(self, resolved_source_ids: List[int]):
        settings = web_server.rules_db.settings
        if settings.forward_new_only:
            logger.info("根据系统设置，跳过历史消息扫描。")
            return
        # (历史处理逻辑，略)

    # --- 辅助方法 ---

    def _apply_replacements(self, text: str) -> str:
        # (修改) 读取动态规则
        replacements = web_server.rules_db.replacements
        if not text or not replacements: return text
        for find, replace_with in replacements.items():
            text = text.replace(find, replace_with) 
        return text

    def _compile_patterns(self, patterns: List[str]) -> List[re.Pattern]:
        return [re.compile(p, re.IGNORECASE) for p in patterns]

    def _compile_word_patterns(self, keywords: List[str]) -> List[re.Pattern]:
        return [re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE) for kw in keywords]

    def _should_filter(self, text: str, media: Any) -> Optional[str]: 
        text = text or ""
        text_lower = text.lower()
        
        # (修改) 从 rules_db 读取动态内容过滤器
        whitelist = web_server.rules_db.whitelist
        ad_filter = web_server.rules_db.ad_filter
        content_filter = web_server.rules_db.content_filter
        
        if whitelist and whitelist.enable:
            if any(kw.lower() in text_lower for kw in (whitelist.keywords or [])):
                return None 

        if ad_filter and ad_filter.enable:
            if any(kw.lower() in text_lower for kw in (ad_filter.keywords_substring or [])):
                return "Blacklist (Substring)"
            for p in self.ad_keyword_word_patterns:
                if p.search(text): return "Blacklist (Word)"
            for p in self.ad_patterns:
                if p.search(text): return "Blacklist (Regex)"
            
            if ad_filter.file_name_keywords and media and isinstance(media, MessageMediaDocument):
                 doc = media.document
                 if doc:
                    file_name = next((attr.file_name for attr in doc.attributes if hasattr(attr, 'file_name')), None)
                    if file_name:
                        if any(kw.lower() in file_name.lower() for kw in ad_filter.file_name_keywords):
                            return "Blacklist (Filename)"

        if content_filter and content_filter.enable:
            if not text and not media: return "Empty"
            if text_lower in ([w.lower() for w in content_filter.meaningless_words] or []):
                return "Meaningless"
            if not media and len(text.strip()) < content_filter.min_meaningful_length:
                return "Too Short"

        return None 

    def _get_message_hash(self, message_data: Dict[str, Any]) -> Optional[str]:
        if not self.config.deduplication.enable: return None
        media = message_data.get('media')
        if media:
            if hasattr(media, 'photo'): return f"photo:{media.photo.id}"
            if hasattr(media, 'document'): return f"doc:{media.document.id}:{getattr(media.document, 'size', '0')}"
        text = message_data.get('text', "")
        if len(text) > 50: return f"text:{hash(text)}"
        return f"id:{message_data.get('hash_source')}"

    async def _is_duplicate(self, message_data: Dict[str, Any], log_id: str) -> bool:
        if not self.config.deduplication.enable: return False
        msg_hash = self._get_message_hash(message_data)
        if not msg_hash: return False
        return await database.check_hash(msg_hash)

    async def _mark_as_processed(self, message_data: Dict[str, Any]):
        if not self.config.deduplication.enable: return
        msg_hash = self._get_message_hash(message_data)
        if msg_hash: await database.add_hash(msg_hash)

    def _find_target(self, text: str, media: Any) -> Tuple[Optional[int], Optional[int]]:
        for rule in web_server.rules_db.distribution_rules:
            if rule.check(text, media): 
                logger.debug(f"命中分发规则: '{rule.name}'")
                return rule.resolved_target_id, rule.topic_id
        
        settings = web_server.rules_db.settings
        return self.config.targets.resolved_default_target_id, settings.default_topic_id

    async def _send_message(self, original_message: Union[Message, List[Message]], message_data: Dict[str, Any], target_id: int, topic_id: Optional[int]):
        text = message_data['text']
        settings = web_server.rules_db.settings
        mode = settings.forwarding_mode
        
        send_kwargs = {}
        if topic_id: send_kwargs["reply_to"] = topic_id
            
        client = self._get_next_client()
        try:
            sent_message = None
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
                    sent_message = await client.send_message(target_id, message=text, file=media_to_send, **send_kwargs)
                else:
                    sent_message = await client.send_message(target_id, message=text, file=None, parse_mode='md', **send_kwargs)
            else:
                sent_message = await client.forward_messages(target_id, messages=original_message, **send_kwargs)
            
            if settings.mark_target_as_read and sent_message:
                try:
                    last_id = sent_message[-1].id if isinstance(sent_message, list) else sent_message.id
                    await client.mark_read(target_id, max_id=last_id, top_msg_id=topic_id)
                except: pass

        except Exception as e:
             await self._handle_send_error(e, client)