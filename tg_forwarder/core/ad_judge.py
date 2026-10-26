# -*- coding: utf-8 -*-
"""AI 广告判别器（jev-1.13 System One 决策模型；默认关=现网行为零变化）。

协议（对齐老马实测 09-21 + jev_kanban_client.py）：
- POST {base}/v1/systemone，body = {model, state, questions}；
- questions.is_ad = {type: "noul", instructions, criteria: {true/false}}；
- 响应 answers.is_ad = {type: "noul", noul: <0~1 广告概率>}。
- 实测：硬广 0.98-0.99、正常内容 0.02-0.07、「附网盘链接」0.68（模糊区）。
- 09-23 判据重标（见 _INSTRUCTIONS）：旧措辞把资源分享误杀（0.96），新措辞
  实测：资源分享 0.23-0.38 放行、卖号硬广 0.98 拦、群推广 0.86 拦、闲聊 0.14 放行。

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

# 判据措辞（09-23 实测标定）：旧措辞「这条消息是广告/推广内容吗？」会把
# 资源分享消息（软件/工具/App 推荐含下载链接）判成「推广」误杀（0.96）。
# 新措辞锚定「商业广告/引流牟利」，资源分享 0.96→0.23，真广告 0.98/群推广 0.86 仍拦。
_INSTRUCTIONS = (
    "这条消息是否为与本频道内容无关的商业广告或引流推广"
    "（如卖号/接单/招代理/群推广/付费服务推销）？"
    "注意：频道内正常分享软件、工具、App、破解版资源（含下载链接）"
    "属于内容本身，不算广告。"
)
_CRITERIA_TRUE = "是商业广告/引流推广"
_CRITERIA_FALSE = "是正常内容分享（软件/工具/资源分享不算广告）"

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
        api_key: Optional[str] = None,
        protocol: str = "systemone",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.threshold = threshold
        self.fuzzy_low = fuzzy_low
        # CMP-284：渠道协议决定打哪个端点。systemone = /v1/systemone 的 noul
        # 决策（jev/laya 同族）；openai = /chat/completions + 提示词要 JSON
        # （能读判据措辞的模型走这条——laya 的 noul 分支忽略 instructions）。
        self.protocol = (protocol or "systemone").strip().lower()
        # CMP-284：laya 等渠道要求 Bearer key。None/空 = 不带 Authorization
        # （jev 这类本机端点无需鉴权，行为与升级前一致）。
        self.api_key = api_key
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
        # 分数落日志（观测核心）：过滤与否都留痕，误杀/漏杀排查有据可查
        logger.info(f"AI 广告判别得分 {score:.2f}（阈值 {self.threshold}）。")
        if score >= self.threshold:
            return True
        if score < self.fuzzy_low:
            return False
        return None  # 模糊区 → 放行

    async def _call(self, text: str) -> Optional[float]:
        """取 is_ad 得分；失败/异常 → None。按 self.protocol 选端点。"""
        if self.protocol == "openai":
            return await self._call_openai(text)
        return await self._call_systemone(text)

    async def _call_systemone(self, text: str) -> Optional[float]:
        import httpx

        state = f"{_STATE_PREFIX}{text[:MAX_TEXT_CHARS].strip()}"
        payload = {
            "model": self.model,
            "state": state,
            "questions": {
                "is_ad": {
                    "type": "noul",
                    "instructions": _INSTRUCTIONS,
                    "criteria": {
                        "true": _CRITERIA_TRUE,
                        "false": _CRITERIA_FALSE,
                    },
                }
            },
        }
        url = f"{self.base_url}/v1/systemone"
        try:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
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

    async def _call_openai(self, text: str) -> Optional[float]:
        """openai 兼容端点：把判据写进提示词，要模型回 JSON，再映射成 0/1 分。

        与 systemone 的差别只在协议：返回仍是「分数」以复用同一套阈值语义。
        模型回 true → 1.0、false → 0.0（二值，故 fuzzy_low 区间实际不出现）；
        解析不出明确布尔 → None（fail-open 放行），绝不当广告处理。
        """
        import httpx

        payload = {
            "model": self.model,
            "temperature": 0,
            # 上限要够推理模型把 reasoning_content 写完再落 content：
            # 实测 glm-5.3 一次判定烧 144 个 reasoning token，max_tokens 给 40 时
            # content 会被截成空串 → 解析不出布尔 → 全程 fail-open，广告判别静默失效。
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{_INSTRUCTIONS}\n"
                        f"是{_CRITERIA_TRUE}回 {{\"is_ad\": true}}，"
                        f"{_CRITERIA_FALSE}回 {{\"is_ad\": false}}。"
                        "只回 JSON，不要解释。"
                    ),
                },
                {"role": "user", "content": text[:MAX_TEXT_CHARS].strip()},
            ],
        }
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
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
            choices = (data or {}).get("choices") or []
            if not choices:
                logger.warning("AI 广告判别响应缺少 choices，按失败处理。")
                return None
            msg = choices[0].get("message") or {}
            content = msg.get("content") or ""
            # 推理模型（glm-5.3 等）把答案写进 content、把思考写进 reasoning_content；
            # content 为空时（max_tokens 被思考吃光）仍可从 reasoning 尾部捞出答案。
            if not content:
                content = msg.get("reasoning_content") or ""
        except Exception as e:
            logger.warning(f"AI 广告判别响应解析失败: {e}")
            return None

        verdict = _parse_is_ad(content)
        if verdict is None:
            logger.warning(
                f"AI 广告判别响应无法解析为 is_ad 布尔，按失败处理: {content[:120]}"
            )
            return None
        return 1.0 if verdict else 0.0


def _parse_is_ad(content: str) -> Optional[bool]:
    """从模型回复里取 is_ad 布尔；找不到明确布尔 → None。

    取**最后一个**可解析的 is_ad，不是第一个：推理模型的 reasoning 里会先复述
    提示词样例（如「输出 {"is_ad": true} 或 {"is_ad": false}」），首个匹配往往是
    样例而非结论 —— 按首个解析会把「资源」误判成广告，正是 09-23 误杀形态。
    结论总在最后，所以取末尾。
    """
    import json
    import re

    verdict = None
    for m in re.finditer(r"\{[^{}]*\}", content or "", re.S):
        try:
            v = json.loads(m.group(0)).get("is_ad")
        except Exception:
            continue
        if isinstance(v, bool):
            verdict = v
        elif isinstance(v, str):
            low = v.strip().lower()
            if low in ("true", "yes", "是"):
                verdict = True
            elif low in ("false", "no", "否"):
                verdict = False
    return verdict
