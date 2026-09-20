# -*- coding: utf-8 -*-
"""F10：AI digest 滚动窗口聚合摘要（OpenAI 兼容端点，默认关=现网行为零变化）。

设计（对齐 F9 delivery 的 fail-open 哲学，绝不影响转发主流程）：
- ``DigestPipeline``：按源滚动窗口缓冲消息；窗口到期 → 调 LLM 出摘要 → 发贴到目标；
- ``RollingWindow``：纯内存窗口（opened_at + interval，到期判据可注入时钟便于单测）；
- ``LLMClient``：OpenAI 兼容 chat/completions 客户端（httpx，已 pin 依赖），
  端点不可用/超时/非 2xx 一律抛给上层，由 pipeline 捕获后 fail-open；
- 关闭态：零缓冲、零 API 调用（should_buffer=False + flush no-op）——
  ``forwarder.process_message`` 里仅当启用才调用 add_message，legacy 路径零侵入。

接线（main.py / forwarder.py 后续步骤或步骤内完成）：
  pipeline = DigestPipeline(llm=LLMClient(...), now_fn=time.time)
  处理消息时（仅 digest 全局开且源开）：pipe.add_message(source, snapshot, msg)
  周期调度（与 catchup 同 IntervalTrigger）：await pipe.flush(forwarder)
"""
from __future__ import annotations

import time
from typing import Any, Callable, List, Optional

from loguru import logger

# axonhub 默认端点（东哥拍板，模型免费）
DEFAULT_BASE_URL = "http://100.64.0.2:8091/v1"
DEFAULT_MODEL = "glm-5.3-flash"

_SYSTEM_PROMPT = (
    "你是一个 Telegram 频道摘要助手。请把下列同源消息按时间顺序整理成"
    "一段约 200 字以内的中文摘要，只输出摘要正文，不要额外解释。"
)


def build_digest_prompt(source_title: str, messages: List[Any]) -> str:
    """把源标题与窗口内消息拼成 LLM 请求 prompt。"""
    lines = [f"# 源频道：{source_title or '未知'}", ""]
    for m in messages:
        text = (getattr(m, "text", None) or "").strip()
        if text:
            lines.append(f"- {text}")
        else:
            lines.append("- [媒体消息]")
    return "\n".join(lines)


class RollingWindow:
    """单源滚动窗口：opened_at 起聚合，到期后 drain。"""

    def __init__(self, opened_at: float, interval: float):
        self.opened_at = opened_at
        self.interval = interval
        self.messages: List[Any] = []

    def add(self, message: Any) -> None:
        self.messages.append(message)

    def drain(self) -> List[Any]:
        out = self.messages
        self.messages = []
        return out

    def due(self, now: float) -> bool:
        return now >= self.opened_at + self.interval


class LLMClient:
    """OpenAI 兼容 chat/completions 客户端（httpx）。失败抛异常由上层 fail-open。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout_seconds

    async def summarize(self, prompt: str) -> str:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return (content or "").strip()


class DigestPipeline:
    """滚动窗口聚合 + fail-open 摘要发送。

    - ``should_buffer(source, snapshot)``：全局 digest.enabled AND 源 digest_enabled；
    - ``add_message``：仅缓冲启用源（关闭态零缓冲）；
    - ``flush(forwarder)``：窗口到期源出摘要；API/发送失败只记日志不抛（不阻塞主流程）。
    """

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        now_fn: Callable[[], float] = time.time,
    ):
        self.llm = llm or LLMClient()
        self._now = now_fn
        self._windows: dict = {}
        self._fwd: Any = None  # 集成注入（main 装配时可选；flush 也可显式传）

    # --- 门控 ---

    @staticmethod
    def _source_digest_on(source: Any) -> bool:
        return bool(getattr(source, "digest_enabled", False))

    def should_buffer(self, source: Any, snapshot: Any) -> bool:
        digest = getattr(snapshot, "digest", None)
        if digest is None or not getattr(digest, "enabled", False):
            return False
        return self._source_digest_on(source)

    # --- 缓冲 ---

    def _window_for(self, source: Any, snapshot: Any) -> RollingWindow:
        chat_id = getattr(source, "resolved_id", None) or getattr(source, "identifier", None)
        w = self._windows.get(chat_id)
        if w is None:
            digest = getattr(snapshot, "digest", None)
            interval = float(getattr(digest, "interval_seconds", 1800) or 1800)
            w = RollingWindow(opened_at=self._now(), interval=interval)
            self._windows[chat_id] = w
        return w

    def add_message(self, source: Any, snapshot: Any, message: Any) -> None:
        if not self.should_buffer(source, snapshot):
            return
        try:
            self._window_for(source, snapshot).add(message)
        except Exception as e:
            logger.error(f"F10 digest 缓冲失败（忽略）: {e}")

    # --- 出摘要 ---

    async def flush(self, forwarder: Any = None) -> int:
        """窗口到期源逐一生成摘要并发贴；失败 fail-open。返回成功发贴数。"""
        fwd = forwarder or self._fwd
        if fwd is None:
            logger.error("F10 digest flush: 无 forwarder 引用，跳过。")
            return 0
        snapshot = fwd.get_snapshot()
        if snapshot is None:
            return 0
        digest = getattr(snapshot, "digest", None)
        if digest is None or not getattr(digest, "enabled", False):
            return 0

        now = self._now()
        posted = 0
        # 快照出到期窗口（避免迭代中修改 dict）
        due_chats = [cid for cid, w in self._windows.items() if w.due(now)]
        for chat_id in due_chats:
            w = self._windows.get(chat_id)
            if w is None:
                continue
            messages = w.drain()
            if not messages:
                continue
            source = self._find_source(snapshot, chat_id)
            try:
                prompt = build_digest_prompt(
                    str(getattr(source, "cached_title", None) or ""), messages
                )
                summary = await self.llm.summarize(prompt)
                if not summary:
                    logger.warning(f"F10 digest 源 {chat_id}: LLM 返回空摘要，跳过发贴。")
                    continue
                target_id = snapshot.targets_resolved_default
                if not target_id:
                    logger.warning(f"F10 digest 源 {chat_id}: 无默认目标，跳过发贴。")
                    continue
                client = fwd._get_next_client()
                if client is None:
                    logger.error("F10 digest: 无可用客户端，跳过发贴。")
                    continue
                await client.send_message(
                    target_id, message=summary, file=None, parse_mode=None
                )
                posted += 1
                title = getattr(source, "cached_title", None) or chat_id
                logger.info(
                    f"F10 digest: {title} 窗口 {len(messages)} 条 → 摘要已发 ({target_id})"
                )
            except Exception as e:
                # fail-open：不抛、不阻塞转发主流程
                logger.error(f"F10 digest 源 {chat_id} 生成/发贴失败（降级跳过）: {e}")
        return posted

    def _find_source(self, snapshot: Any, chat_id: Any):
        for s in getattr(snapshot, "sources", []) or []:
            if getattr(s, "resolved_id", None) == chat_id:
                return s
        return None