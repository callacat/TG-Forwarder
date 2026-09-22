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


def fmt_uptime(seconds: float) -> str:
    """人类可读运行时长（M3 仪表盘/`/api/status` 消费）。防御负数与非法输入。"""
    if seconds is None or seconds < 0:
        return "0秒"
    try:
        secs = int(seconds)
    except (TypeError, ValueError):
        return "0秒"
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    mins, secs = divmod(secs, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if mins:
        parts.append(f"{mins}分")
    if not parts:
        parts.append(f"{secs}秒")
    return " ".join(parts)


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


# ---------------------------------------------------------------------------
# F1：跨源内容级去重指纹（跨源同资源：不同消息 id、不同源频道，资源本体相同）。
# 现单源 dedup 只 hash caption+媒体 id，跨源重发同一资源（换图/换源贴同链）漏掉。
# 指纹维度：① 链接（消息文本中的网盘/普通 URL，归一化去 query）；
#           ② 文件名+大小（文档媒体，跨源重发的同一文件 id 不同但 name+size 相同）。
# ---------------------------------------------------------------------------
URL_TOKEN_PATTERN = r"https?://[^\s]+"


def extract_url_tokens(text: str) -> List[str]:
    """提取文本中的 URL 并归一化（去 query/fragment、去尾标点、小写）。

    尾标点覆盖中英文常用（。；，、！？与 ASCII 对应符）——中文文案贴链接后
    紧跟全角标点是现网常态，不剥离会导致同链接指纹不一致漏判。
    """
    if not text:
        return []
    _TRAIL = ".,;:!?)]}\"'》」」。；：！？、，…"
    out = []
    for raw in re.findall(URL_TOKEN_PATTERN, text):
        u = raw.rstrip(_TRAIL)
        # 去 query 与 fragment，保 path（网盘分享码在 path）
        u = u.split("#", 1)[0].split("?", 1)[0]
        if len(u) > 8:
            out.append(u.lower())
    return out


def _doc_meta(media: Any) -> Optional[Tuple[str, int]]:
    """提取文档媒体的 (文件名, 大小)；两层形状兼容与 message_hash/_doc_file_name 同法。"""
    if not media:
        return None
    doc = getattr(media, "document", None)
    if not doc:
        return None
    doc = getattr(doc, "document", doc)
    if not doc:
        return None
    name = next(
        (attr.file_name for attr in doc.attributes if hasattr(attr, "file_name")),
        None,
    )
    size = getattr(doc, "size", 0) or 0
    if not name or size <= 0:
        return None
    return (name.lower(), size)


def content_fingerprints(text: str, media: Any) -> List[str]:
    """消息的内容指纹集合（F1）：链接指纹 + 文件名+大小指纹。

    空 media+短文本 → 无指纹（不参与跨源去重，走既有 id hash）。
    """
    fps: List[str] = []
    for u in extract_url_tokens(text or ""):
        fps.append(f"link:{u}")
    meta = _doc_meta(media)
    if meta:
        fps.append(f"file:{meta[0]}:{meta[1]}")
    return fps


def build_header(source_config, message) -> Optional[str]:
    """F6：按源的 header_template 生成源标注头部（telemirror ForwardFormatFilter 范式）。

    占位符：
      {title}  源频道名（resolved 缓存标题）；{link}  源链接（https://t.me/s/<标题>）；
      {time}  消息时间（YYYY-MM-DD HH:MM）。
    模板为 None/空 → 返回 None（不标注，默认关=现网行为零变化）。
    占位符未配置的字段以空串/未知占位，但不会抛异常。
    """
    template = getattr(source_config, "header_template", None)
    if not template:
        return None

    title = getattr(source_config, "cached_title", None) or ""
    link = ""
    if title:
        link = f"https://t.me/s/{title}"
    ts = ""
    ts_attr = getattr(message, "date", None)
    if ts_attr and not str(ts_attr).startswith("None"):
        try:
            ts = ts_attr.strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = str(ts_attr)

    text = template.replace("{title}", title)
    text = text.replace("{link}", link)
    text = text.replace("{time}", ts)
    return text


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


def _source_in_translate_list(source_id: Any, translate_cfg: Any) -> bool:
    """F11：源（resolved_id）是否在 translate.sources 启用列表内。"""
    if translate_cfg is None:
        return False
    sources = getattr(translate_cfg, "sources", None) or []
    for s in sources:
        try:
            if int(s) == int(source_id):
                return True
        except (TypeError, ValueError):
            continue
    return False


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

    def __init__(
        self,
        db: Database,
        account_manager: Any,
        semantic_engine: Any = None,
        ad_judge: Any = None,
    ) -> None:
        self.db = db
        self._am = account_manager
        self._snapshot = None
        self._client_flood_wait: Dict[str, float] = {}
        self._rr_index = 0
        self._processed_lru: "OrderedDict[str, None]" = OrderedDict()
        self._prune_task: Optional[asyncio.Task] = None
        # F12 语义去重引擎（默认 None=不启用；main.py 装配时注入外观类
        # SemanticDedup，内部含 SemanticDedupEngine——勿注入裸 Engine，其
        # check_duplicate 签名为 (text, marker)，与下方三参调用不匹配）
        self.semantic_engine = semantic_engine
        # AI 广告判别器（jev；默认 None=不启用；main.py 装配时注入 AdJudge，
        # 构造不发请求。None=绝对零路径，现网默认配置下无任何相关日志/请求）
        self.ad_judge = ad_judge
        # F10：AI digest 滚动窗口聚合（默认 None=不启用；main.py 装配时注入）
        self.digest_pipeline: Optional[Any] = None
        # M3 仪表盘数据源：进程启动时刻 + 消息处理统计（内存计数）
        self._start_ts = time.time()
        self._msg_stats: Dict[str, int] = {
            "processed": 0,  # 监控源进入流水线
            "filtered": 0,   # 被过滤模型/黑名单/内容过滤
            "duplicates": 0, # 单源或跨源去重命中
            "forwarded": 0,  # 发送成功
            "failed": 0,     # 无目标/无客户端/发送异常
        }

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

        self._msg_stats["processed"] += 1  # M3：监控源消息进入流水线
        text = message.text or ""
        media = message.media

        # 是否已进入发送路径：发送成功后（含 _send_message 内部失败计数）的
        # post-send 异常不得再计 failed，防止同一条消息 forwarded/failed 双计
        send_attempted = False

        try:
            # 过滤
            filter_reason, filter_keyword = should_filter(text, media, snapshot)
            if filter_reason:
                logger.info(
                    f"消息 {message.id} 被过滤。原因: {filter_reason} | "
                    f"关键词: {filter_keyword}"
                )
                self._msg_stats["filtered"] += 1
                return

            # AI 广告判别（jev-1.13；默认关=不装配，绝对零路径）。
            # 位置：should_filter 之后、去重之前（spec 契约）。verdict=True → 拦截；
            # False → 放行；None（模糊/失败/异常）→ fail-open 放行，绝不丢消息。
            # 不新建独立统计键：复用 filtered 计数（与既有过滤同路径）。
            if self.ad_judge is not None:
                try:
                    ad_verdict = await self.ad_judge.is_ad(text)
                except Exception as e:
                    logger.warning(f"AI 广告判别失败（fail-open 放行）: {e}")
                    ad_verdict = None
                if ad_verdict is True:
                    logger.info(
                        f"消息 {message.id} 被 AI 广告过滤（jev 判定 is_ad=true）。"
                    )
                    self._msg_stats["filtered"] += 1
                    return

            # 去重
            if snapshot.deduplication.enable:
                h = message_hash(text, media, message.id)
                if h and await self.db.check_hash(h):
                    logger.info(f"消息 {message.id} 重复。")
                    self._msg_stats["duplicates"] += 1
                    return

            # F1 跨源内容级去重（默认关；任一指纹已见即跨源丢弃）
            if snapshot.deduplication.cross_source_enable:
                fps = content_fingerprints(text, media)
                for fp in fps:
                    if await self.db.check_hash(fp):
                        logger.info(
                            f"消息 {message.id} 跨源重复（{fp.split(':', 1)[0]} 指纹已见）。"
                        )
                        self._msg_stats["duplicates"] += 1
                        return

            # F12 语义去重（默认关；模型缺失/加载失败自动降级为不启用）。
            # 仅当开关开启且引擎可用才做 embedding 比对；任何失败都放行不阻塞。
            sem = getattr(snapshot.deduplication, "semantic_dedup_enabled", False)
            if sem and self.semantic_engine is not None:
                # 全程 fail-open：注入 API 不匹配/引擎异常 → 放行，不丢消息不计 failed
                try:
                    engine_ready = await self.semantic_engine.ensure_ready()
                except Exception as e:
                    logger.warning(f"F12 引擎就绪检查失败（放行）: {e}")
                    engine_ready = False
                if engine_ready:
                    try:
                        is_sem_dup = await self.semantic_engine.check_duplicate(
                            text, message.id, self.db
                        )
                    except Exception as e:
                        logger.warning(f"F12 语义比对失败（放行）: {e}")
                        is_sem_dup = False
                    if is_sem_dup:
                        logger.info(
                            f"消息 {message.id} 语义重复（F12 引擎命中），丢弃。"
                        )
                        self._msg_stats["duplicates"] += 1
                        return

            # 目标路由
            target_id, topic_id = find_target(text, media, snapshot)
            if not target_id:
                logger.error(f"消息 {message.id} 无有效目标。")
                self._msg_stats["failed"] += 1
                return

            # 替换
            new_text = apply_replacements(text, snapshot)

            # F11 AI 翻译（默认关；仅对 translate.sources 内源生效；
            # 失败/无 key/端点异常 → 原文放行，绝不丢消息）
            translate_cfg = getattr(snapshot, "translate", None)
            if (
                translate_cfg is not None
                and getattr(translate_cfg, "enabled", False)
                and source_config is not None
                and _source_in_translate_list(numeric_chat_id, translate_cfg)
            ):
                try:
                    translator = self._get_translator(translate_cfg)
                    if translator is not None:
                        translated = await translator.translate(new_text)
                        if translated is not None:
                            new_text = translated
                except Exception as e:
                    logger.warning(f"F11 翻译失败，原文放行: {e}")

            # F6 源标注模板（默认关=不标注；有模板时前置 header）
            header = build_header(source_config, message)
            if header:
                rendered = f"{header}\n\n{new_text}".strip()
                new_text = rendered

            # 发送（相册取整组）
            send_attempted = True
            to_send = all_messages_in_group if all_messages_in_group else message
            await self._send_message(to_send, new_text, target_id, topic_id, snapshot)

            # F7 评论区资源抓取（per 源 check_replies 开关，默认关）
            if source_config and source_config.check_replies:
                try:
                    await self._collect_reply_links(message, source_config)
                except Exception as e:
                    logger.warning(f"F7 评论区抓取异常（不阻塞转发）: {e}")

            # F10：AI digest 缓冲（全局+per 源 AND，默认关=零缓冲零 API 调用）
            if (
                self.digest_pipeline is not None
                and self.digest_pipeline.should_buffer(source_config, snapshot)
            ):
                try:
                    self.digest_pipeline.add_message(source_config, snapshot, to_send)
                except Exception as e:
                    logger.error(f"F10 digest 入窗失败（不阻塞转发）: {e}")

            # 标记已见
            if snapshot.deduplication.enable:
                h = message_hash(text, media, message.id)
                if h:
                    await self.db.add_hash(h)
            # F1：发送成功后登记内容指纹（跨源后续消息被拦）
            if snapshot.deduplication.cross_source_enable:
                for fp in content_fingerprints(text, media):
                    await self.db.add_hash(fp)
            # F12：发送成功后登记语义向量指纹（后续语义重复被拦；失败不阻塞）
            eng_enabled = False
            try:
                eng_enabled = bool(getattr(self.semantic_engine, "enabled", False))
            except Exception:
                pass
            if sem and self.semantic_engine is not None and eng_enabled:
                try:
                    # marker=消息 id：add 复用 check 阶段已算向量，避免二次 embed
                    await self.semantic_engine.add(text, message.id, self.db)
                except Exception as e:
                    logger.warning(f"F12 语义指纹登记失败（不阻塞）: {e}")

        except Exception as e:
            logger.error(f"处理消息失败: {e}", exc_info=True)
            # 仅「未发起发送」路径计 failed：发送后的登记/进度异常不得再来一次
            if not send_attempted:
                self._msg_stats["failed"] += 1
        finally:
            # 断点续传（v2 对齐）；进度写入失败不阻塞后续消息也不影响计数
            try:
                await self.db.set_progress(numeric_chat_id, message.id)
            except Exception as e:
                logger.warning(f"进度写入失败（不阻塞）: {e}")

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
            self._msg_stats["failed"] += 1
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
                    # F8：parse_mode 由 "md" 改为 None——replacements 替换后残留
                    # `* _ [` 等符号会触发 telegram markdown 解析异常（apppro 实证坑），
                    # None = 纯文本发送，格式零污染、安全兜底。
                    sent_message = await client.send_message(
                        target_id, message=text, file=None, parse_mode=None,
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

            # F2：登记 src→dest 映射（编辑/删除级联；copy 与 forward 模式都登记）
            if (
                sent_message
                and self._source_sync_enabled(snapshot, original_message)
            ):
                try:
                    dest_list = (
                        sent_message if isinstance(sent_message, list) else [sent_message]
                    )
                    src_msgs = (
                        original_message
                        if isinstance(original_message, list)
                        else [original_message]
                    )
                    # 相册整组：逐条配对（按组内顺序）
                    for i, dest in enumerate(dest_list):
                        src = src_msgs[min(i, len(src_msgs) - 1)]
                        await self.db.add_message_map(
                            src.chat_id, src.id, target_id, dest.id
                        )
                except Exception as e:
                    logger.warning(f"F2 映射登记失败（不阻塞转发）: {e}")

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
            self._msg_stats["failed"] += 1
        else:
            if sent_message:
                self._msg_stats["forwarded"] += 1  # M3：发送成功计数

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

    # --- F2：编辑/删除实时同步（telemirror/appro 模式）---

    def _get_translator(self, translate_cfg: Any):
        """F11：按需构建翻译器（懒加载，构建失败降级 None=原文放行）。

        测试/装配可注入 ``fwd.translator`` 覆盖（与 F12 semantic_engine 同模式）；
        否则按 config 构建并缓存于实例；热重载换配置会重建（id 变更）。
        """
        injected = getattr(self, "translator", None)
        if injected is not None:
            return injected
        cache_key = id(translate_cfg)
        cached = getattr(self, "_translator_cache", None)
        if cached and cached[0] == cache_key:
            return cached[1]
        from tg_forwarder.core.ai_translate import build_translator

        translator = build_translator(translate_cfg)
        self._translator_cache = (cache_key, translator)
        return translator

    @staticmethod
    def _source_sync_enabled(snapshot, original_message) -> bool:
        """源消息所在频道的编辑/删除同步开关（per 源，默认关=现网行为零变化）。"""
        src_config = None
        chat_id = original_message.chat_id if hasattr(original_message, "chat_id") else None
        if chat_id is not None:
            for s in snapshot.sources:
                if s.resolved_id == chat_id:
                    src_config = s
                    break
        return bool(src_config and src_config.sync_edits)

    async def _on_message_edited(self, message) -> None:
        """源消息编辑 → 同步编辑全部镜像（F2）。

        只改文本部分（媒体本体不动）；映射缺失（开启前转发的）跳过。
        """
        snapshot = self._snapshot
        if snapshot is None or not snapshot.deduplication:
            pass  # 快照判空在下方统一处理
        if snapshot is None:
            return
        try:
            src_chat = message.chat_id
            src_id = message.id
            mirrors = await self.db.get_message_map_by_src(src_chat, src_id)
            if not mirrors:
                return
            new_text = apply_replacements(message.text or "", snapshot)
            client = self._get_next_client()
            if client is None:
                logger.warning("F2 编辑同步：无可用客户端，跳过本轮。")
                return
            for dest_channel_id, dest_message_id in mirrors:
                try:
                    await client.edit_message(
                        dest_channel_id, dest_message_id, new_text
                    )
                    logger.info(
                        f"F2 编辑同步: {src_chat}/{src_id} → {dest_channel_id}/{dest_message_id}"
                    )
                except Exception as e:
                    logger.warning(
                        f"F2 编辑镜像 {dest_channel_id}/{dest_message_id} 失败: {e}"
                    )
        except Exception as e:
            logger.error(f"F2 编辑同步异常: {e}")

    async def _on_message_deleted(self, event) -> None:
        """源消息删除 → 级联删除全部镜像并清映射（F2，per 源 sync_deletes）。"""
        snapshot = self._snapshot
        if snapshot is None:
            return
        try:
            deleted_ids = getattr(event, "deleted_ids", None) or []
            channel_id = getattr(event, "channel_id", None)
            if not deleted_ids or channel_id is None:
                return
            # 该源是否开启删除同步（per 源开关）
            src_config = None
            for s in snapshot.sources:
                if s.resolved_id == channel_id:
                    src_config = s
                    break
            if not src_config or not src_config.sync_deletes:
                return
            client = self._get_next_client()
            if client is None:
                logger.warning("F2 删除同步：无可用客户端，跳过本轮。")
                return
            for src_id in deleted_ids:
                mirrors = await self.db.get_message_map_by_src(channel_id, src_id)
                if mirrors:
                    try:
                        # 镜像可能分布多目标：按目标分组删除
                        by_dest: Dict[int, List[int]] = {}
                        for d_ch, d_id in mirrors:
                            by_dest.setdefault(d_ch, []).append(d_id)
                        for d_ch, d_ids in by_dest.items():
                            await client.delete_messages(d_ch, d_ids)
                        logger.info(
                            f"F2 删除同步: {channel_id}/{src_id} → "
                            f"{len(mirrors)} 条镜像已删"
                        )
                    except Exception as e:
                        logger.warning(f"F2 删除镜像 {channel_id}/{src_id} 失败: {e}")
                await self.db.delete_message_map_by_src(channel_id, src_id)
        except Exception as e:
            logger.error(f"F2 删除同步异常: {e}")

    # --- 事件注册（main 层调用）---

    # --- F7：评论区资源抓取（per 源 check_replies，默认关）---

    async def _collect_reply_links(self, message, source_config) -> int:
        """F7：把该源消息的回复中的网盘链接收集进死链检测管线。

        触发条件：源开启 check_replies 且消息有回复。
        抓取回复文本里的网盘链接（NET_DISK_DOMAINS），add_pending_link 排队，
        后续 LinkChecker 统一检测。默认关=现网行为零变化。
        返回本轮收集到的链接数。
        """
        if not source_config or not source_config.check_replies:
            return 0
        client = self._get_next_client()
        if client is None:
            return 0
        chat_id = message.chat_id
        limit = getattr(source_config, "replies_limit", 10) or 10
        from tg_forwarder.core.link_checker import extract_links, NET_DISK_DOMAINS

        collected = 0
        try:
            async for reply in client.iter_messages(
                chat_id, reply_to=message.id, limit=limit
            ):
                if not getattr(reply, "text", None):
                    continue
                links = extract_links(reply.text, NET_DISK_DOMAINS)
                for link in links:
                    await self.db.add_pending_link(link, getattr(reply, "id", 0))
                    collected += 1
            if collected:
                logger.info(
                    f"F7 源 {chat_id} 消息 {message.id} 回复区收集到 "
                    f"{collected} 个网盘链接进死链检测管线。"
                )
        except Exception as e:
            logger.warning(f"F7 抓取回复链接失败: {e}")
        return collected

    def register_handlers(self, client) -> None:
        """注册 NewMessage/Album/Edited/Deleted 处理器（v2 拓扑 + F2 级联）。"""

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

        # F2 编辑/删除级联（内部按 per 源开关门控，默认关）
        @client.on(events.MessageEdited())
        async def handle_edited(event):
            await self._on_message_edited(event.message)

        @client.on(events.MessageDeleted())
        async def handle_deleted(event):
            await self._on_message_deleted(event)

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
                total += await self._catchup_source(client, src, limit)
            except Exception as e:
                logger.error(f"catchup 源 {src.resolved_id} 异常: {e}")
        if total:
            logger.info(f"🧩 catchup 兜底补齐 {total} 条（事件漏送窗口）。")
        return total

    async def _catchup_source(self, client: Any, src, limit: int) -> int:
        """单源增量兜底（对齐 v2 process_history 的 50 条/次 + 相册合并 + F4 年龄截断）。

        参数 src 为 SourceConfig：携带 resolved_id 与 age_cutoff_hours（F4，
        超龄旧消息跳过不转发，默认 None=不限制=现网行为零变化）。
        """
        chat_id = src.resolved_id
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

        # F4 年龄截断（per 源，默认不限制）：超龄消息跳过转发（仅抬 progress）
        cutoff_ts = None
        if (getattr(src, "age_cutoff_hours", None) or 0) > 0:
            try:
                cutoff_ts = time.time() - float(src.age_cutoff_hours) * 3600
            except (TypeError, ValueError):
                cutoff_ts = None

        processed = 0
        skipped_old = 0
        i, n = 0, len(batch)
        while i < n:
            m = batch[i]
            # F4：超龄消息不进 process_message（不转发），但 progress 仍抬升跳过
            if cutoff_ts:
                m_ts = getattr(m, "date", None)
                if m_ts is None:
                    m_ts_ts = 0
                else:
                    try:
                        m_ts_ts = m_ts.timestamp()
                    except Exception:
                        m_ts_ts = 0
                if m_ts_ts and m_ts_ts < cutoff_ts:
                    skipped_old += 1
                    i += 1
                    continue

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

        if skipped_old:
            logger.info(
                f"F4 catchup 源 {chat_id}: 跳过 {skipped_old} 条超龄旧消息"
                f"（age_cutoff_hours={src.age_cutoff_hours}）"
            )

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
        uptime = fmt_uptime(time.time() - self._start_ts)  # M3：真实运行时长
        flood_total = sum(a.get("flood_wait_count", 0) for a in accounts)
        return {
            "accounts": accounts,
            "proxy_fallback": proxy_fallback,
            "uptime": uptime,
            "flood_wait_total": flood_total,
            "message_stats": dict(self._msg_stats),  # M3：消息处理统计
        }
