# -*- coding: utf-8 -*-
"""转发引擎：纯函数过滤/替换/去重 + 快照原子替换（R8/P7）+ 事件驱动 + catchup 兜底。

- P7 根治：update_snapshot 整体替换引用，处理中持有旧快照，无半新半旧窗口；
- catchup 重复打日志修复：chat/message LRU 去重，同消息双路径（事件+扫描）只处理一次；
- P8 修复：dedup TTL 后台清理（v2 dedup_hashes 只增）。
"""
import asyncio
import hashlib
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from telethon import events
from telethon.errors import MediaCaptionTooLongError
from telethon.tl.types import (
    Channel,
    Chat,
    Message,
    MessageEntityTextUrl,
    MessageMediaDocument,
    MessageMediaWebPage,
)

from tg_forwarder.storage.db import Database


# ---------------------------------------------------------------------------
# 纯函数（不依赖实例状态，便于单测）
# ---------------------------------------------------------------------------

_regex_cache: "OrderedDict[str, re.Pattern]" = OrderedDict()


def _cached_compile(pattern: str, flags: int = re.IGNORECASE) -> re.Pattern:
    """正则编译缓存（LRU 512），避免每次 reload 重复编译。"""
    key = f"{flags}:{pattern}"
    hit = _regex_cache.get(key)
    if hit is not None:
        _regex_cache.move_to_end(key)
        return hit
    compiled = re.compile(pattern, flags)
    _regex_cache[key] = compiled
    if len(_regex_cache) > 512:
        _regex_cache.popitem(last=False)
    return compiled


def _compile_word_patterns(keywords: List[str]) -> List[re.Pattern]:
    return [_cached_compile(r"\b" + re.escape(kw) + r"\b") for kw in keywords]


def _compile_patterns(patterns: List[str]) -> List[re.Pattern]:
    compiled = []
    for p in patterns:
        try:
            compiled.append(_cached_compile(p))
        except re.error as e:
            logger.warning(f"无效正则已跳过: {p} ({e})")
    return compiled


# ---------------------------------------------------------------------------
# T4：caption 超限截断保媒体（v2 全历史 7 次 SendMediaRequest "caption too long"
# 整条图文永久丢失实锤；v3 原先同样无保护）。方案=保图截文：截断+省略号，溢出文本丢弃。
# ---------------------------------------------------------------------------

# Telegram caption 上限 1024，按 UTF-16 代码元计数（emoji 等增补平面字符占 2）
CAPTION_LIMIT_UNITS = 1024
_CAPTION_ELLIPSIS = "…"  # U+2026 占 1 个 UTF-16 代码元


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def clamp_caption(text: str, limit: int = CAPTION_LIMIT_UNITS) -> str:
    """caption 超限时截断到 limit（UTF-16 单位）并补省略号；未超限原样返回。

    逐码点累计宽度截断，绝不拆散 emoji 代理对。
    """
    if _utf16_len(text) <= limit:
        return text
    budget = limit - _utf16_len(_CAPTION_ELLIPSIS)
    out: List[str] = []
    used = 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out).rstrip() + _CAPTION_ELLIPSIS


def _doc_file_name(media: Any) -> Optional[str]:
    """从消息媒体提取文件名（document 兼容两层形状，与 message_hash 同逻辑）。"""
    if not media:
        return None
    doc = getattr(media, "document", None)
    if not doc:
        return None
    # 兼容 MessageMediaDocument（嵌套 .document）或裸 Document
    doc = getattr(doc, "document", doc)
    if not doc:
        return None
    return next(
        (attr.file_name for attr in doc.attributes if hasattr(attr, "file_name")),
        None,
    )


def should_filter(text: str, media: Any, snapshot) -> Tuple[Optional[str], Optional[str]]:
    """过滤判断（对齐 v2 _should_filter）：白名单最高优先 → 广告黑名单 → 内容质量。

    返回 (reason, keyword)；不过滤返回 (None, None)。
    """
    text = text or ""
    text_lower = text.lower()

    # 1. 白名单（最高优先级）
    whitelist = snapshot.whitelist
    if whitelist and whitelist.enable:
        if any(kw.lower() in text_lower for kw in (whitelist.keywords or [])):
            return None, None

    # 2. 广告黑名单
    ad_filter = snapshot.ad_filter
    if ad_filter and ad_filter.enable:
        if ad_filter.keywords_substring:
            for kw in ad_filter.keywords_substring:
                if kw.lower() in text_lower:
                    return "Blacklist (Substring)", kw

        for p in _compile_word_patterns(ad_filter.keywords_word or []):
            match = p.search(text)
            if match:
                return "Blacklist (Word)", match.group(0)

        for p in _compile_patterns(ad_filter.patterns or []):
            match = p.search(text)
            if match:
                return "Blacklist (Regex)", p.pattern

        if ad_filter.file_name_keywords:
            file_name = _doc_file_name(media)
            if file_name:
                for kw in ad_filter.file_name_keywords:
                    if kw.lower() in file_name.lower():
                        return "Blacklist (Filename)", kw

    # 3. 内容质量过滤
    content_filter = snapshot.content_filter
    if content_filter and content_filter.enable:
        if not text and not media:
            return "Empty", "No Content"
        if text_lower in [w.lower() for w in (content_filter.meaningless_words or [])]:
            return "Meaningless", text
        if not media and len(text.strip()) < content_filter.min_meaningful_length:
            return "Too Short", f"Len: {len(text.strip())}"

    return None, None


def apply_replacements(text: str, snapshot) -> str:
    """内容替换（对齐 v2：str.replace 逐条应用）。"""
    replacements = snapshot.replacements
    if not text or not replacements:
        return text
    for find, replace_with in replacements.items():
        text = text.replace(find, replace_with)
    return text


def message_hash(text: str, media: Any, msg_id: Any) -> Optional[str]:
    """去重哈希（对齐 v2 语义；长文本改 sha256 修复内置 hash 跨重启不稳定）。

    photo → photo:{id}；document → doc:{id}:{size}；文本>50 → text:{sha256[:16]}；
    其他 → id:{msg_id}。

    修复 v2 隐藏 bug：v2 用 ``media.document.id``，但真实 telethon 对象上
    MessageMediaDocument 的嵌套 Document 在 ``media.document.document``——
    v2 该路径 AttributeError 被 add_hash 的 try 静默吃掉，文档消息从未入 dedup。
    """
    if media:
        if hasattr(media, "photo") and media.photo:
            return f"photo:{media.photo.id}"
        if hasattr(media, "document") and media.document:
            doc = media.document
            # 兼容两层形状：MessageMediaDocument（嵌套 .document）或裸 Document
            doc = getattr(doc, "document", doc)
            return f"doc:{doc.id}:{getattr(doc, 'size', '0')}"
    text = text or ""
    if len(text) > 50:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"text:{digest[:16]}"
    return f"id:{msg_id}"


def find_target(text: str, media: Any, snapshot) -> Tuple[Optional[int], Optional[int]]:
    """目标路由（对齐 v2 _find_target）：分发规则命中 → 默认目标。"""
    for rule in snapshot.distribution_rules:
        try:
            if rule.check(text, media):
                logger.debug(f"命中分发规则: '{rule.name}'")
                return rule.resolved_target_id, rule.topic_id
        except Exception as e:
            logger.error(f"分发规则 '{rule.name}' 检查异常: {e}")

    settings = snapshot.settings
    return snapshot.targets_resolved_default, settings.default_topic_id


# ---------------------------------------------------------------------------
# 转发引擎
# ---------------------------------------------------------------------------


class Forwarder:
    """事件驱动转发 + catchup 兜底；快照原子替换（R8）。

    两条入口共用同一 process_message 管线：
    - 实时：register_handlers 挂 telethon NewMessage/Album 事件；
    - 兜底：catchup_once（main.py 以 IntervalTrigger 周期调用），
      增量扫描 progress 之后的消息，补齐断连窗口/update 间隙漏送的
      （09-13 事故 v2 靠它补回 35 条）。与事件路径不双发：
      progress 门控（min_id=last）为主、LRU + dedup hash 为兜底。
    """

    _CATCHUP_LRU_SIZE = 200      # 近期已处理消息 chat/msg 记录上限
    _CATCHUP_INTERVAL_SECONDS = 300  # 兜底扫描周期（对齐 v2 IntervalTrigger 300s）
    _CATCHUP_LIMIT = 50          # 每源每轮上限（对齐 v2 50 条/次）

    def __init__(self, db: Database, account_manager: Any):
        self.db = db
        self._am = account_manager
        self._snapshot = None
        self._client_flood_wait: Dict[str, float] = {}
        self._rr_index = 0
        self._processed_lru: "OrderedDict[str, None]" = OrderedDict()
        self._prune_task: Optional[asyncio.Task] = None

    # --- 快照（R8 原子化）---

    def update_snapshot(self, config) -> None:
        """整体替换配置快照（一次赋值，原子；P7 根治）。"""
        self._snapshot = config.snapshot()
        logger.info("转发器配置快照已整体替换（热重载）。")

    def get_snapshot(self):
        return self._snapshot

    @property
    def client_flood_wait(self) -> Dict[str, float]:
        """FloodWait 到期时间表（bot /status 与观测读取，R9）。

        旧属性名带下划线 _client_flood_wait，而 status_handler 读无下划线名，
        导致 /status 的 FloodWait 计数恒 0；此别名对齐消费端。
        """
        return self._client_flood_wait

    # --- FloodWait 感知轮询（对齐 v2 _get_next_client）---

    def _get_next_client(self) -> Any:
        clients = self._am.healthy_accounts()
        if not clients:
            return None
        now = time.time()
        start = self._rr_index
        for _ in range(len(clients)):
            client = clients[self._rr_index % len(clients)]
            key = getattr(client, "session_name_for_forwarder", f"c{self._rr_index}")
            self._rr_index = (self._rr_index + 1) % len(clients)
            if self._client_flood_wait.get(key, 0) <= now:
                return client
        # 全部在 FloodWait：取第一个（尽力而为）
        return clients[start % len(clients)]

    def _handle_send_error(self, client: Any, e: Exception) -> None:
        from telethon import errors

        key = getattr(client, "session_name_for_forwarder", "unknown")
        if isinstance(e, errors.FloodWaitError):
            wait_time = e.seconds + 5
            self._client_flood_wait[key] = time.time() + wait_time
            if self._am is not None and hasattr(self._am, "record_flood_wait"):
                self._am.record_flood_wait(key, e.seconds)
        else:
            logger.error(f"客户端 {key} 发送错误: {e}")

    # --- catchup LRU（重复打日志修复）---

    def _seen_recently(self, chat_id: int, message_id: int) -> bool:
        key = f"{chat_id}/{message_id}"
        if key in self._processed_lru:
            self._processed_lru.move_to_end(key)
            return True
        self._processed_lru[key] = None
        while len(self._processed_lru) > self._CATCHUP_LRU_SIZE:
            self._processed_lru.popitem(last=False)
        return False

    # --- 消息处理流水线（对齐 v2 process_message）---

    async def process_message(
        self, message: Message, all_messages_in_group: Optional[List[Message]] = None
    ) -> None:
        """处理一条新消息（事件或 catchup 进入）。"""
        snapshot = self._snapshot
        if snapshot is None:
            return

        numeric_chat_id = message.chat_id
        if numeric_chat_id > 1000000000 and not str(numeric_chat_id).startswith("-100"):
            numeric_chat_id = int(f"-100{numeric_chat_id}")

        # catchup LRU：同消息只处理一次
        if self._seen_recently(numeric_chat_id, message.id):
            logger.debug(f"消息 {numeric_chat_id}/{message.id} 近期已处理，跳过。")
            return

        # 源匹配
        source_config = None
        for s in snapshot.sources:
            if s.resolved_id == numeric_chat_id:
                source_config = s
                break
        if not source_config:
            return

        text = message.text or ""
        media = message.media

        try:
            # 过滤
            filter_reason, filter_keyword = should_filter(text, media, snapshot)
            if filter_reason:
                logger.info(
                    f"消息 {message.id} 被过滤。原因: {filter_reason} | "
                    f"关键词: {filter_keyword}"
                )
                return

            # 去重
            if snapshot.deduplication.enable:
                h = message_hash(text, media, message.id)
                if h and await self.db.check_hash(h):
                    logger.info(f"消息 {message.id} 重复。")
                    return

            # 目标路由
            target_id, topic_id = find_target(text, media, snapshot)
            if not target_id:
                logger.error(f"消息 {message.id} 无有效目标。")
                return

            # 替换
            new_text = apply_replacements(text, snapshot)

            # 发送（相册取整组）
            to_send = all_messages_in_group if all_messages_in_group else message
            await self._send_message(to_send, new_text, target_id, topic_id, snapshot)

            # 标记已见
            if snapshot.deduplication.enable:
                h = message_hash(text, media, message.id)
                if h:
                    await self.db.add_hash(h)

        except Exception as e:
            logger.error(f"处理消息失败: {e}", exc_info=True)
        finally:
            # 断点续传（v2 对齐）
            await self.db.set_progress(numeric_chat_id, message.id)

    # --- 发送（copy 无痕 / forward 模式，对齐 v2 _send_message）---

    async def _send_message(
        self, original_message, text: str, target_id: int,
        topic_id: Optional[int], snapshot,
    ) -> None:
        mode = snapshot.settings.forwarding_mode
        send_kwargs = {}
        if topic_id:
            send_kwargs["reply_to"] = topic_id

        client = self._get_next_client()
        if client is None:
            logger.error("无可用客户端发送，放弃本条消息。")
            return

        try:
            sent_message = None
            if mode == "copy":
                media_to_send = None
                is_real_file = False
                if isinstance(original_message, list):
                    media_to_send = [m.media for m in original_message if m.media]
                    is_real_file = True
                elif isinstance(original_message, Message):
                    media = original_message.media
                    if media and not isinstance(media, MessageMediaWebPage):
                        is_real_file = True
                        media_to_send = media

                if is_real_file:
                    # T4：caption 预截断保图；服务端仍拒则去 caption 单发图兜底
                    caption = clamp_caption(text)
                    if caption != text:
                        logger.warning(
                            f"T4: caption {_utf16_len(text)} UTF-16 单位超上限"
                            f"({CAPTION_LIMIT_UNITS})，截断保图，溢出文本丢弃"
                        )
                    try:
                        sent_message = await client.send_message(
                            target_id, message=caption, file=media_to_send,
                            **send_kwargs
                        )
                    except MediaCaptionTooLongError:
                        logger.warning(
                            "T4: 服务端仍拒绝 caption（MediaCaptionTooLongError），"
                            "去文本重发媒体兜底"
                        )
                        sent_message = await client.send_message(
                            target_id, message="", file=media_to_send,
                            **send_kwargs
                        )
                else:
                    sent_message = await client.send_message(
                        target_id, message=text, file=None, parse_mode="md",
                        **send_kwargs
                    )
            else:
                sent_message = await client.forward_messages(
                    target_id, messages=original_message, **send_kwargs
                )

            # 成功收发 → 刷新活性时间戳（黑洞式断连检测双保险，R1）
            key = getattr(client, "session_name_for_forwarder", None)
            if key and self._am is not None and hasattr(self._am, "touch"):
                self._am.touch(key)

            if snapshot.settings.mark_target_as_read and sent_message:
                try:
                    last_id = (
                        sent_message[-1].id
                        if isinstance(sent_message, list)
                        else sent_message.id
                    )
                    await client.mark_read(
                        target_id, max_id=last_id, top_msg_id=topic_id
                    )
                except Exception:
                    pass
        except Exception as e:
            self._handle_send_error(client, e)

    # --- 目标解析（对齐 v2 normalize_target，保持行为）---

    async def resolve_targets(self, client) -> None:
        """解析默认目标与分发规则目标 ID；同步源标题缓存。"""
        snapshot = self._snapshot
        if snapshot is None or client is None:
            return

        async def normalize_target(identifier) -> Optional[int]:
            try:
                if not identifier:
                    return None
                search_key = identifier
                if isinstance(identifier, str):
                    identifier = identifier.strip()
                    if identifier.lstrip("-").isdigit():
                        search_key = int(identifier)

                entity = None
                try:
                    entity = await client.get_entity(search_key)
                except Exception:
                    if (
                        isinstance(search_key, int)
                        and str(search_key).startswith("-100")
                    ):
                        try:
                            entity = await client.get_entity(
                                int(str(search_key)[4:])
                            )
                        except Exception:
                            pass

                if not entity:
                    try:
                        logger.warning(
                            f"未在缓存中找到 {search_key}，尝试刷新 Dialogs..."
                        )
                        async for _d in client.iter_dialogs(limit=50):
                            pass
                        entity = await client.get_entity(search_key)
                    except Exception as refresh_error:
                        logger.warning(f"刷新缓存后仍未找到: {refresh_error}")

                if not entity:
                    raise ValueError(f"Cannot find entity corresponding to {identifier}")

                resolved_id = entity.id
                title = getattr(entity, "title", None)
                if not title and hasattr(entity, "username"):
                    title = entity.username

                if isinstance(entity, Channel) and not str(resolved_id).startswith("-100"):
                    resolved_id = int(f"-100{resolved_id}")
                elif isinstance(entity, Chat) and not str(resolved_id).startswith("-"):
                    resolved_id = int(f"-{resolved_id}")

                logger.info(f"目标 '{identifier}' -> {title} (ID: {resolved_id})")
                return resolved_id
            except Exception as e:
                logger.error(f"❌ 无法解析目标: {identifier} - {e}")
                return None

        if snapshot.settings.default_target:
            resolved = await normalize_target(snapshot.settings.default_target)
            snapshot.targets_resolved_default = resolved

        for rule in snapshot.distribution_rules:
            rule.resolved_target_id = await normalize_target(rule.target_identifier)

        # 源标题缓存同步落表（v2 行为：resolved_id/cached_title 持久化）
        await self._sync_source_titles(client, snapshot)

    async def _sync_source_titles(self, client, snapshot) -> None:
        """解析源 resolved_id 与标题并落表（启动/重载时一次）。"""
        from tg_forwarder.storage.repositories import SourceRepository

        repo = SourceRepository(self.db)
        for s in snapshot.sources:
            try:
                if s.resolved_id:
                    try:
                        entity = await client.get_entity(s.resolved_id)
                    except Exception:
                        entity = None
                    if not entity:
                        entity = await client.get_entity(s.identifier)
                else:
                    entity = await client.get_entity(s.identifier)

                resolved_id = entity.id
                title = getattr(entity, "title", None)
                if isinstance(entity, Channel) and not str(resolved_id).startswith("-100"):
                    resolved_id = int(f"-100{resolved_id}")
                elif isinstance(entity, Chat) and not str(resolved_id).startswith("-"):
                    resolved_id = int(f"-{resolved_id}")

                if s.resolved_id != resolved_id or (title and s.cached_title != title):
                    s.resolved_id = resolved_id
                    if title:
                        s.cached_title = title
                    await repo.save(s.model_dump())
            except Exception as e:
                logger.error(f"无法解析源 '{s.identifier}': {e}")

    # --- 事件注册（main 层调用）---

    def register_handlers(self, client) -> None:
        """注册 NewMessage/Album 处理器（对齐 v2 事件拓扑）。"""

        @client.on(events.NewMessage())
        async def handle_new_message(event):
            if event.message.grouped_id:
                return
            await self.process_message(event.message)

        @client.on(events.Album())
        async def handle_album(event):
            main_message = next(
                (m for m in event.messages if m.text), event.messages[0]
            )
            await self.process_message(main_message, all_messages_in_group=event.messages)

    # --- dedup TTL 清理（P8 修复）---

    def start_prune_task(self) -> None:
        """启动后台 TTL 清理循环（6h 一次；保留天数从 settings 读）。"""
        if self._prune_task and not self._prune_task.done():
            return

        async def _loop():
            while True:
                try:
                    snapshot = self._snapshot
                    days = (
                        snapshot.settings.dedup_retention_days
                        if snapshot is not None
                        else 30
                    )
                    removed = await self.db.prune_old_hashes(days)
                    if removed:
                        logger.info(f"🧹 dedup_hashes TTL 清理: 删除 {removed} 条")
                except Exception as e:
                    logger.error(f"dedup 清理异常: {e}")
                await asyncio.sleep(6 * 3600)

        self._prune_task = asyncio.create_task(_loop())

    # --- catchup 兜底扫描（P1 修复：补齐事件漏送）---

    async def catchup_once(self, client: Any = None, limit: int = 50) -> int:
        """增量扫描各源 progress 之后的消息，补齐 telethon 事件漏送的。

        main.py 以 IntervalTrigger(300s) 周期调用。返回本轮补齐条数。
        与实时事件不双发：process_message 内部 progress 门控（min_id=last）
        为主、_seen_recently LRU + dedup hash 为兜底。
        """
        snapshot = self._snapshot
        if snapshot is None:
            return 0
        if client is None:
            healthy = self._am.healthy_accounts() if self._am else []
            if not healthy:
                logger.debug("catchup: 无可用客户端，跳过本轮。")
                return 0
            client = healthy[0]

        total = 0
        for src in list(snapshot.sources):
            if not src.resolved_id:
                continue
            try:
                total += await self._catchup_source(client, src.resolved_id, limit)
            except Exception as e:
                logger.error(f"catchup 源 {src.resolved_id} 异常: {e}")
        if total:
            logger.info(f"🧩 catchup 兜底补齐 {total} 条（事件漏送窗口）。")
        return total

    async def _catchup_source(self, client: Any, chat_id: int, limit: int) -> int:
        """单源增量兜底（对齐 v2 process_history 的 50 条/次 + 相册合并）。"""
        settings = self._snapshot.settings
        last = await self.db.get_progress(chat_id)

        # 新源基线：forward_new_only 时不倒灌历史，仅记录当前头部往后追
        if last == 0 and settings.forward_new_only:
            async for m in client.iter_messages(chat_id, limit=1):
                await self.db.set_progress(chat_id, m.id)
                logger.info(
                    f"catchup 源 {chat_id} 建立基线 progress={m.id}"
                    f"（forward_new_only，不倒灌历史）"
                )
            return 0

        # 增量：reverse=True 升序（oldest-first），保证 progress 连续推进
        batch: List[Message] = []
        async for m in client.iter_messages(
            chat_id, min_id=last, limit=limit, reverse=True
        ):
            batch.append(m)
        if not batch:
            return 0

        processed = 0
        i, n = 0, len(batch)
        while i < n:
            m = batch[i]
            gid = getattr(m, "grouped_id", None)
            if gid:
                group = [m]
                j = i + 1
                while j < n and getattr(batch[j], "grouped_id", None) == gid:
                    group.append(batch[j])
                    j += 1
                main_msg = next((g for g in group if g.text), group[0])
                await self.process_message(main_msg, all_messages_in_group=group)
                i = j
            else:
                await self.process_message(m)
                i += 1
            processed += 1

        # 本轮扫到的最大 id 抬升 progress（含相册尾部/被过滤消息），
        # 避免下轮重复拉取（process_message 内 finally 只抬到 main_msg.id）
        await self.db.set_progress(chat_id, batch[-1].id)
        return processed

    # --- 状态（R9：/api/status 与 /api/stats 消费）---

    def all_status(self) -> Dict[str, Any]:
        accounts = self._am.all_status().get("accounts", []) if self._am else []
        proxy_fallback = {
            a.get("session_name"): a.get("proxy_level")
            for a in accounts
            if a.get("session_name")
        }
        uptime = None
        flood_total = sum(a.get("flood_wait_count", 0) for a in accounts)
        return {
            "accounts": accounts,
            "proxy_fallback": proxy_fallback,
            "uptime": uptime,
            "flood_wait_total": flood_total,
        }
