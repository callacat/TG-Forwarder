# -*- coding: utf-8 -*-
"""F14 AI 结构化内容处理：一次调用同时出「广告判定 + 清洗正文」（默认关）。

与 F10-F13 同族的第四个 AI 能力，差异在**输出是结构化 JSON**，且**丢弃与否由本地
代码按阈值决定**，不由模型自己拍板：

    POST {base_url}/chat/completions          （OpenAI 兼容，axonhub 默认）
    -> {"is_ad":bool,"ad_type":str,"confidence":float,
        "confidence":0~1,"cleaned_text":str,"reason":str}

为什么不抄同类项目的「哨兵串」做法（模型返回 `#不转发` 即丢弃，见
Heavrnl/TelegramForwarder）：模型偶然吐出该字符串就会静默丢消息，正撞本项目
反复强调的 fail-open 铁律（ad_judge.py / digest.py / ai_translate.py 同款）。
这里模型只负责**描述**，drop 需要 is_ad 且 confidence 过阈值两个条件同时成立。

判据措辞直接继承 F13 于 2026-09-23 实测标定的版本（锚定「商业广告/引流牟利」，
资源分享 0.96→0.23 放行、卖号硬广 0.98 拦、群推广 0.86 拦、闲聊 0.14 放行），
不重新试错。

fail-open 铁律：请求失败/超时/解析失败/字段缺失 → 返回默认 Verdict（不 drop、
不替换），调用方拿到的永远是可用的 Verdict 对象而非 None，本方法永不抛异常。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from loguru import logger

from tg_forwarder.core.ai_translate import TranslationCache

# 默认端点/模型与 F10 digest、F11 翻译同源（axonhub 中转，避免多套默认值漂移）
DEFAULT_BASE_URL = "http://100.64.0.2:8091/v1"
DEFAULT_MODEL = "glm-5.3-flash"
DEFAULT_API_KEY_ENV = "AXONHUB_API_KEY"

# 广告判定阈值：confidence >= threshold 才 drop；低于即放行（等价 F13 的
# 「模糊区 fail-open」，此处不设双阈值——结构化输出多给了 reason，可事后复盘）。
AD_THRESHOLD = 0.85

# 清洗结果采纳护栏（防丢数据，不可省）：
#   - 清洗后为空 → 拒（模型可能把整条消息清掉）
#   - 长度不足原文 min_ratio 倍 → 拒（内容被截没了）
#   - 长度超原文 2 倍 → 拒（模型跑偏输出长文，会把垃圾推进转发频道）
MIN_KEEP_RATIO = 0.3
MAX_KEEP_RATIO = 2.0

# 单条消息送入模型的最大长度（超出截断，防撑爆上下文）
MAX_TEXT_CHARS = 4096

# 模型偶尔用 ```json 围栏或前后带解说，解析前先剥围栏再兜底截花括号
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

_SYSTEM_PROMPT = (
    "你是 Telegram 频道消息的内容审核与清洗助手。只输出一个 JSON 对象，不要任何解释文字。\n\n"
    "【广告判定】判断消息是否为「与本频道内容无关的商业广告或引流推广」"
    "（如卖号/接单/招代理/群推广/付费服务推销）。"
    "注意：频道内正常分享软件、工具、App、破解版资源（含下载链接）属于内容本身，不算广告。\n\n"
    "【正文清洗】在保留原意与全部有效信息的前提下：\n"
    "- 删除频道尾巴/引流尾巴（如「点击查看完整版」「关注公众号」「更多资源」"
    "「无关注价值的自推链接行」等与内容无关的行）\n"
    "- 删除纯广告行、重复行、无意义装饰符号，合并多余空行\n"
    "- 正文本身逐字保留：不要改写、翻译、总结、缩写、补充\n"
    "- 无需清洗时 cleaned_text 原样返回正文\n\n"
    "【输出】严格如下 JSON，confidence 为 0~1 的小数：\n"
    '{"is_ad": true/false, "ad_type": "sell_account|promo_group|paid_service|'
    'resource_share|none", "confidence": 0.0, '
    '"cleaned_text": "清洗后的完整正文", "reason": "不超过30字的理由"}'
)


@dataclass
class Verdict:
    """一次判定的结果。默认构造即为 fail-open 值（不 drop、不替换）。"""

    is_ad: bool = False
    ad_type: str = "none"
    confidence: float = 0.0
    drop: bool = False
    cleaned_text: Optional[str] = None
    reason: str = ""


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    """从模型回复里抠出 JSON 对象；代码围栏/前后解说都能容忍。失败 → None。"""
    s = (raw or "").strip()
    if not s:
        return None
    s = _FENCE_RE.sub("", s).strip()
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 —— 下面还有一次兜底尝试
        pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(s[start:end + 1])
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 —— 确实不是 JSON，交给调用方 fail-open
            return None
    return None


def _clamp01(value: Any) -> float:
    """把模型给的置信度收敛到 0~1；非数值/缺失 → 0.0（=最保守，永不 drop）。"""
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


class AiContentProcessor:
    """OpenAI 兼容结构化内容处理器（httpx 依赖已 pin）。

    用法（Forwarder 持有实例）：
        verdict = await processor.process(text)
        if verdict.drop:      # 仅 is_ad 且 confidence 过阈值
            ...
        text = verdict.cleaned_text or text
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        threshold: float = AD_THRESHOLD,
        clean_enabled: bool = True,
        timeout: float = 8.0,
        json_mode: bool = True,
        min_ratio: float = MIN_KEEP_RATIO,
        max_ratio: float = MAX_KEEP_RATIO,
        max_text_chars: int = MAX_TEXT_CHARS,
        sources: Optional[List[Union[int, str]]] = None,
        cache_max_entries: int = 256,
        cache_ttl_seconds: int = 3600,
        cache: Optional[TranslationCache] = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.threshold = threshold
        self.clean_enabled = clean_enabled
        self.timeout = timeout
        self.json_mode = json_mode
        self.min_ratio = min_ratio
        self.max_ratio = max_ratio
        self.max_text_chars = max_text_chars
        # 空列表 = 全部源生效（与 F11 的「空=不启用」相反：F14 默认关，
        # 一旦显式开启就应当全局生效，源级收窄是可选的收紧动作）
        self.sources = list(sources or [])
        # 显式传空串 = 明确「端点无需鉴权」，即使 env 已设也不回填
        self.api_key_env = api_key_env
        if api_key is not None:
            self._api_key = api_key
        else:
            self._api_key = os.getenv(api_key_env, "") if api_key_env else ""
        # 缓存参数单独留档：main.py 热重载按全字段元组比对时要读回它们，
        # 否则 yaml 改了 cache_ttl 后 reload 会静默沿用旧缓存。
        self.cache_max_entries = cache_max_entries
        self.cache_ttl_seconds = cache_ttl_seconds
        self.cache = cache or TranslationCache(
            max_entries=cache_max_entries, ttl_seconds=cache_ttl_seconds
        )

    def applies_to(self, source_id: Any) -> bool:
        """该源是否在启用范围内（sources 为空 = 全源）。"""
        if not self.sources:
            return True
        for s in self.sources:
            try:
                if int(s) == int(source_id):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    # --- 对外接口（永不抛异常）---

    async def process(self, text: str) -> Verdict:
        """判定 + 清洗。失败/空文本/无 key → 默认 Verdict（放行且不替换）。"""
        if not text or not text.strip():
            return Verdict()
        if not self._api_key:
            logger.warning("F14 AI 内容处理未配置 API key，静默跳过（原文放行）。")
            return Verdict()

        # 清洗基准 = 完整原文，但只有全文都看得见才允许改写：模型只收到前
        # max_text_chars，若让它改写并用结果整体替换长文本，模型没看见的尾巴会被
        # 一并替换掉（静默丢内容）。超长消息降级为「只判广告不改文」。
        cached = self.cache.get(text)
        if cached is not None:
            parsed = _extract_json(cached)
            if parsed is not None:
                return self._to_verdict(parsed, text)

        try:
            raw = await self._call(text)
        except Exception as e:  # noqa: BLE001 —— 运行期异常也 fail-open
            logger.warning(f"F14 AI 内容处理异常（原文放行）: {e}")
            return Verdict()

        if raw is None:
            return Verdict()
        data = _extract_json(raw)
        if data is None:
            logger.warning("F14 AI 内容处理响应不是可解析的 JSON（原文放行）。")
            return Verdict()

        self.cache.put(text, raw)
        return self._to_verdict(data, text)

    # --- 内部 ---

    def _to_verdict(self, data: Dict[str, Any], original: str) -> Verdict:
        """把模型 JSON 映射成 Verdict；只有 is_ad 且 confidence 过阈值才 drop。"""
        is_ad = data.get("is_ad") is True
        confidence = _clamp01(data.get("confidence"))
        verdict = Verdict(
            is_ad=is_ad,
            ad_type=str(data.get("ad_type") or "none")[:32],
            confidence=confidence,
            drop=is_ad and confidence >= self.threshold,
            reason=str(data.get("reason") or "")[:120],
        )
        # 分数落日志（沿用 F13 观测做法）：过滤与否都留痕，误杀/漏杀可复盘
        logger.info(
            f"F14 AI 判定 is_ad={verdict.is_ad} conf={confidence:.2f}"
            f"（阈值 {self.threshold}）type={verdict.ad_type} reason={verdict.reason!r}"
        )
        if self.clean_enabled:
            if len(original) > self.max_text_chars:
                # 模型只见前缀 → 不敢让它改写整体（会连带抹掉没看见的尾巴）
                logger.info(
                    f"F14 正文超过 max_text_chars（{len(original)} > "
                    f"{self.max_text_chars}），跳过清洗只判广告；如需清洗请调大该值。"
                )
            else:
                cleaned = self._acceptable_clean(
                    data.get("cleaned_text"), original
                )
                if cleaned is not None:
                    verdict.cleaned_text = cleaned
        return verdict

    def _acceptable_clean(self, candidate: Any, original: str) -> Optional[str]:
        """清洗结果护栏：空/截没/跑偏一律拒绝，返回 None 表示「用原文」。"""
        if not isinstance(candidate, str):
            return None
        cleaned = candidate.strip()
        if not cleaned or cleaned == original:
            return None
        ratio = len(cleaned) / max(1, len(original))
        if ratio < self.min_ratio:
            logger.warning(
                f"F14 清洗结果过短（{ratio:.0%} < {self.min_ratio:.0%}），判定为内容截失，弃用。"
            )
            return None
        if ratio > self.max_ratio:
            logger.warning(
                f"F14 清洗结果异常冗长（{ratio:.0%} > {self.max_ratio:.0%}），判定为模型跑偏，弃用。"
            )
            return None
        return cleaned

    async def _call(self, text: str) -> Optional[str]:
        """调用 /chat/completions 取回复原文；网络/状态码异常 → None。"""
        import httpx

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text[: self.max_text_chars].strip()},
            ],
            "temperature": 0.2,
        }
        if self.json_mode:
            # 结构化输出约束；端点不支持时返回 4xx → 下方按失败 fail-open，
            # 报错日志会点名 json_mode，面板关掉即可退回纯提示词模式。
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
        except Exception as e:
            logger.warning(f"F14 AI 内容处理网络请求失败: {e}")
            return None

        if resp.status_code >= 400:
            hint = "（该端点可能不支持 response_format，可关掉 json_mode）" if (
                self.json_mode and resp.status_code == 400
            ) else ""
            logger.warning(
                f"F14 AI 内容处理端点返回 {resp.status_code}{hint}: {resp.text[:200]}"
            )
            return None

        try:
            data = resp.json()
            choices = (data or {}).get("choices") or []
            if not choices:
                logger.warning("F14 AI 内容处理响应缺少 choices，按失败处理。")
                return None
            content = (choices[0].get("message") or {}).get("content") or ""
            return str(content) or None
        except Exception as e:
            logger.warning(f"F14 AI 内容处理响应解析失败: {e}")
            return None


def build_ai_content(ai_cfg: Any) -> Optional[AiContentProcessor]:
    """从 RuntimeConfig.ai_content 构建处理器；未启用/无配置 → None。"""
    if ai_cfg is None or not getattr(ai_cfg, "enabled", False):
        return None
    try:
        return AiContentProcessor(
            base_url=getattr(ai_cfg, "base_url", DEFAULT_BASE_URL),
            model=getattr(ai_cfg, "model", DEFAULT_MODEL),
            api_key_env=getattr(ai_cfg, "api_key_env", DEFAULT_API_KEY_ENV),
            threshold=float(getattr(ai_cfg, "threshold", AD_THRESHOLD) or AD_THRESHOLD),
            clean_enabled=bool(getattr(ai_cfg, "clean_enabled", True)),
            timeout=float(getattr(ai_cfg, "timeout", 8.0) or 8.0),
            json_mode=bool(getattr(ai_cfg, "json_mode", True)),
            min_ratio=float(getattr(ai_cfg, "min_ratio", MIN_KEEP_RATIO)),
            max_ratio=float(getattr(ai_cfg, "max_ratio", MAX_KEEP_RATIO)),
            max_text_chars=int(
                getattr(ai_cfg, "max_text_chars", MAX_TEXT_CHARS) or MAX_TEXT_CHARS
            ),
            sources=getattr(ai_cfg, "sources", None),
            # 不用 `or` 兜底：cache_ttl_seconds=0 在 TranslationCache 语义里是
            # 「永不过期」，`or 3600` 会把它悄悄改成 1 小时。
            cache_max_entries=int(getattr(ai_cfg, "cache_max_entries", 256)),
            cache_ttl_seconds=int(getattr(ai_cfg, "cache_ttl_seconds", 3600)),
        )
    except Exception as e:  # noqa: BLE001 —— 构建失败降级为关闭，绝不阻塞转发
        logger.warning(f"F14 AI 内容处理器构建失败（功能降级为关闭）: {e}")
        return None
