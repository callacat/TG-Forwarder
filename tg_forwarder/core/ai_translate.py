# -*- coding: utf-8 -*-
"""F11 AI 翻译：同 axonhub OpenAI 兼容端点，带缓存与 fail-open 逻辑。

设计（对齐 M4 验收：默认关、关闭态零行为变化、失败绝不放丢消息）：
- ``TranslationCache``：进程内 LRU + TTL 结果缓存，避免同文本重复计费；
- ``AiTranslator``：OpenAI 兼容 /chat/completions 客户端——
  * 空文本/API key 缺失/请求失败/非 2xx：一律返回 None（不抛异常）；
  * 调用方拿到 None 即用原文放行，转发主流程绝不被翻译阻塞；
- ``maybe_translate``：Forwarder 集成入口的纯函数包装（默认关时零调用）。
"""
from __future__ import annotations

import os
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from loguru import logger

# 默认端点/模型与 F10 digest 共用 axonhub（避免双源漂移）
DEFAULT_ENDPOINT = "http://100.64.0.2:8091/v1"
DEFAULT_MODEL = "glm-5.3-flash"
DEFAULT_API_KEY_ENV = "AXONHUB_API_KEY"


class TranslationCache:
    """进程内翻译结果缓存（LRU + TTL）。

    - get(key)：命中且未过期返回结果，否则 None（视为 miss）；
    - put(key, value)：写入；超容量逐出最久未使用项。
    线程模型：Forwarder 单线程事件循环调用，无需加锁。
    """

    def __init__(self, max_entries: int = 128, ttl_seconds: int = 3600):
        self.max_entries = max(1, int(max_entries))
        # TTL 保留浮点（亚秒级测试/精细控制）；<=0 语义=永不过期
        self.ttl_seconds = float(ttl_seconds) if ttl_seconds else 0.0
        self._data: "OrderedDict[str, tuple[float, str]]" = OrderedDict()

    def _now(self) -> float:
        return time.monotonic()

    def _is_fresh(self, ts: float) -> bool:
        if self.ttl_seconds <= 0:
            return True  # 0=永不过期
        return (self._now() - ts) < self.ttl_seconds

    def get(self, key: str) -> Optional[str]:
        item = self._data.get(key)
        if item is None:
            return None
        ts, value = item
        if not self._is_fresh(ts):
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: str, value: str) -> None:
        self._data[key] = (self._now(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)


class AiTranslator:
    """OpenAI 兼容翻译客户端（httpx，已 pin 依赖）。

    translate() 永不抛异常：任何失败返回 None，调用方以原文放行。
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        api_key: Optional[str] = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 8.0,
        cache: Optional[TranslationCache] = None,
    ):
        self.endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout_seconds
        self._api_key = api_key
        if not self._api_key and api_key_env:
            self._api_key = os.getenv(api_key_env, "")
        self.cache = cache or TranslationCache()

    # --- 对外接口（fail-open）---

    async def translate(self, text: str) -> Optional[str]:
        """翻译一条文本。失败/配置缺失 → None（调用方原文放行）。"""
        if not text or not text.strip():
            return ""
        if not self._api_key:
            logger.warning("F11 翻译未配置 API key，静默跳过（原文放行）。")
            return None

        hit = self.cache.get(text)
        if hit is not None:
            return hit

        try:
            result = await self._call(text)
        except Exception as e:
            logger.warning(f"F11 翻译请求异常（原文放行）: {e}")
            return None

        if result is None:
            return None
        self.cache.put(text, result)
        return result

    async def _call(self, text: str) -> Optional[str]:
        import httpx

        url = f"{self.endpoint}/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": text}],
            "temperature": 0.3,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url, json=payload, headers=headers
                )
        except Exception as e:
            logger.warning(f"F11 翻译网络请求失败: {e}")
            return None

        if resp.status_code >= 400:
            logger.warning(
                f"F11 翻译端点返回 {resp.status_code}: {resp.text[:200]}"
            )
            return None

        try:
            data = resp.json()
            choices = (data or {}).get("choices") or []
            if not choices:
                logger.warning("F11 翻译响应缺少 choices，按失败处理。")
                return None
            content = (choices[0].get("message") or {}).get("content") or ""
            return content.strip() or None
        except Exception as e:
            logger.warning(f"F11 翻译响应解析失败: {e}")
            return None


def build_translator(translate_cfg: Any) -> Optional[AiTranslator]:
    """从 RuntimeConfig.translate 构建翻译器；未启用/无配置 → None。"""
    if translate_cfg is None or not getattr(translate_cfg, "enabled", False):
        return None
    try:
        return AiTranslator(
            endpoint=getattr(translate_cfg, "endpoint", DEFAULT_ENDPOINT),
            api_key_env=getattr(translate_cfg, "api_key_env", DEFAULT_API_KEY_ENV),
            model=getattr(translate_cfg, "model", DEFAULT_MODEL),
            timeout_seconds=getattr(translate_cfg, "timeout_seconds", 8.0),
            cache=TranslationCache(
                max_entries=getattr(translate_cfg, "cache_max_entries", 128),
                ttl_seconds=getattr(translate_cfg, "cache_ttl_seconds", 3600),
            ),
        )
    except Exception as e:
        logger.warning(f"F11 翻译器构建失败（翻译降级为关闭）: {e}")
        return None