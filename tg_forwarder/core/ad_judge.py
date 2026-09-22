# -*- coding: utf-8 -*-
"""AI 广告判别器（jev-1.13 System One 决策模型；默认关=现网行为零变化）。

协议（对齐老马实测 09-21 + jev_kanban_client.py）：
- POST {base}/v1/systemone，body = {model, state, questions}；
- questions.is_ad = {type: "noul", instructions, criteria: {true/false}}；
- 响应 answers.is_ad = {type: "noul", noul: <0~1 广告概率>}。
- 实测：硬广 0.98-0.99、正常内容 0.02-0.07、「附网盘链接」0.68（模糊区）。

fail-open 铁律：判定 None（模糊/失败/超时/异常）→ 调用方放行，
绝不因 AI 不可用丢消息。与 F11 翻译/F12 语义去重同类：默认关不装配，开启后
仅构造持有配置不发请求，请求在首次 is_ad 调用时才发生。
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

# 默认端点/模型（本机 opencode-free 容器，无需 key）
DEFAULT_BASE_URL = "http://100.64.0.2:28880"
DEFAULT_MODEL = "jev-1.13"

# 广告判定阈值：得分 >= AD_THRESHOLD 判广告；< FUZZY_LOW 判正常；
# [FUZZY_LOW, AD_THRESHOLD) 为模糊区 → None（fail-open 放行）。
AD_THRESHOLD = 0.85
FUZZY_LOW = 0.60

# state 前缀：把消息文本接在后面作为待判内容（与实测同构，保持标定一致）
_STATE_PREFIX = "判断下面消息是否广告："

# 单条消息送入 state 的最大长度（防超长消息撑爆模型上下文；超出截断）
MAX_TEXT_CHARS = 4096


class AdJudge:
    """jev System One 广告判别器：构造不发请求，行为全部 fail-open。

    用法（Forwarder 持有实例）：
        verdict = await ad_judge.is_ad(text)
        # True=广告、False=正常、None=模糊/失败（放行）
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        threshold: float = AD_THRESHOLD,
        fuzzy_low: float = FUZZY_LOW,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.threshold = threshold
        self.fuzzy_low = fuzzy_low
        self.timeout = timeout

    async def is_ad(self, text: str) -> Optional[bool]:
        """判断 text 是否为广告。

        True=广告；False=正常；None=模糊区/请求失败/解析失败（调用方应放行）。
        任何异常都不外泄——即使 httpx/解析炸了也返回 None，绝不阻塞转发主线。
        """
        if not text or not text.strip():
            return None  # 空文本无内容可判 → 放行
        try:
            score = await self._call(text)
        except Exception as e:  # noqa: BLE001 —— 运行期异常也 fail-open
            logger.warning(f"AI 广告判别异常（fail-open 放行）: {e}")
            return None
        if score is None:
            return None
        if score >= self.threshold:
            return True
        if score < self.fuzzy_low:
            return False
        return None  # 模糊区 → 放行

    async def _call(self, text: str) -> Optional[float]:
        """调用 /v1/systemone 取 is_ad 得分；失败/异常 → None。"""
        import httpx

        state = f"{_STATE_PREFIX}{text[:MAX_TEXT_CHARS].strip()}"
        payload = {
            "model": self.model,
            "state": state,
            "questions": {
                "is_ad": {
                    "type": "noul",
                    "instructions": "这条消息是广告/推广内容吗？",
                    "criteria": {"true": "是广告/推广", "false": "不是广告/推广"},
                }
            },
        }
        url = f"{self.base_url}/v1/systemone"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
        except Exception as e:
            logger.warning(f"AI 广告判别网络请求失败: {e}")
            return None

        if resp.status_code >= 400:
            logger.warning(
                f"AI 广告判别端点返回 {resp.status_code}: {resp.text[:200]}"
            )
            return None

        try:
            data = resp.json()
            ans = ((data or {}).get("answers") or {}).get("is_ad") or {}
            score = ans.get("noul")
            if score is None:
                logger.warning("AI 广告判别响应缺少 is_ad.noul，按失败处理。")
                return None
            return float(score)
        except Exception as e:
            logger.warning(f"AI 广告判别响应解析失败: {e}")
            return None
