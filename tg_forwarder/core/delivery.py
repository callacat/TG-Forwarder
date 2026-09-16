# -*- coding: utf-8 -*-
"""F9：转发出口（delivery）抽象——把「已转发事件」以可选方式推送到外部。

v3 现网仅转发到 Telegram 目标；F9 提供可选的外部出口（RSS/Apprise/Webhook），
做成 delivery 抽象，默认关（现网行为零变化），后续按需接后端。

设计：
- ``DeliveryBackend`` 抽象基类：send(event) 推送一条已转发事件；
- ``WebhookDelivery``：POST JSON 到配置的 webhook URL（复用已 pin 的 httpx）；
- ``DeliveryManager``：聚合多个后端，fail-open（后端异常只记日志、不阻塞转发）。
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional

from loguru import logger


class DeliveryEvent:
    """一条已转发的结构化事件（delivery 出口的载荷）。

    source/target 为频道可读标识；text 摘要；media_type 媒体类型
    （text/photo/document/album）；occurred_at ISO 时间戳。
    """

    def __init__(
        self,
        source: str = "",
        target: str = "",
        destination: str = "",
        text: str = "",
        media_type: str = "text",
        link: str = "",
        occurred_at: str = "",
    ):
        self.source = source
        self.target = target
        self.destination = destination
        self.text = text
        self.media_type = media_type
        self.link = link
        self.occurred_at = occurred_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "destination": self.destination,
            "text": self.text,
            "media_type": self.media_type,
            "link": self.link,
            "occurred_at": self.occurred_at,
        }


class DeliveryBackend(abc.ABC):
    """delivery 后端抽象：实现 send(event) 即可接入（RSS/Apprise/Webhook 等）。"""

    name: str = "abstract"

    @abc.abstractmethod
    async def send(self, event: DeliveryEvent) -> bool:
        """推送一条已转发事件。返回是否成功；实现须自行容错。"""
        raise NotImplementedError


class WebhookDelivery(DeliveryBackend):
    """Webhook 出口：POST JSON 到目标 URL（httpx，已 pin 依赖）。

    配置：url（必填）、headers（可选 dict）、timeout_seconds（默认 5）。
    """

    name = "webhook"

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None,
                 timeout_seconds: float = 5.0):
        import httpx

        self.url = url
        self.headers = headers or {}
        self.timeout = timeout_seconds

    async def send(self, event: DeliveryEvent) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=event.to_dict(), headers=self.headers)
                ok = resp.status_code < 400
                if not ok:
                    logger.warning(
                        f"F9 webhook 返回 {resp.status_code}: {resp.text[:200]}"
                    )
                return ok
        except httpx.RequestError as e:
            logger.warning(f"F9 webhook 请求失败: {e}")
            return False
        except Exception as e:
            logger.error(f"F9 webhook 异常: {e}")
            return False


class DeliveryManager:
    """聚合多个后端，fail-open 推送。

    - ``enabled=False``（默认）：send_event 直接 no-op，现网零变化；
    - 单后端失败只记日志、不抛（绝不影响转发核心）。
    """

    def __init__(self, backends: Optional[List[DeliveryBackend]] = None):
        self.backends: List[DeliveryBackend] = list(backends or [])
        self._enabled = bool(self.backends)

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self.backends)

    async def send_event(self, event: DeliveryEvent) -> int:
        """推送到全部启用后端；返回成功条数。未启用返回 0。"""
        if not self.enabled:
            return 0
        ok_count = 0
        for b in self.backends:
            try:
                if await b.send(event):
                    ok_count += 1
            except Exception as e:
                logger.error(f"F9 delivery 后端 {getattr(b, 'name', '?')} 失败: {e}")
        return ok_count


def build_delivery_manager(cfg: Any) -> DeliveryManager:
    """从 RuntimeConfig.delivery 构建 DeliveryManager（默认关）。

    支持 multiple webhooks：``delivery.webhooks`` 为 [{url, headers?, timeout?}...]。
    未启用/空配置 → 空 manager（enabled=False）。
    """
    if cfg is None:
        return DeliveryManager([])
    delivery = getattr(cfg, "delivery", None)
    if delivery is None or not getattr(delivery, "enabled", False):
        return DeliveryManager([])

    backends: List[DeliveryBackend] = []
    for w in getattr(delivery, "webhooks", []) or []:
        if not w or not w.get("url"):
            continue
        backends.append(
            WebhookDelivery(
                w["url"],
                headers=w.get("headers"),
                timeout_seconds=w.get("timeout", 5.0),
            )
        )
    if not backends:
        logger.warning("F9 delivery 已启用但未配置任何 webhook url，实际不生效。")
    return DeliveryManager(backends)