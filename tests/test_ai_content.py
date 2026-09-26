# -*- coding: utf-8 -*-
"""F14 AI 结构化内容处理测试（广告判定 + 清洗正文，单次调用）。

覆盖 spec 契约：
- 判定映射：is_ad 且 confidence >= threshold 才 drop；低于阈值一律放行；
- **丢弃不由模型拍板**（防同类项目哨兵串方案的核心回归点）：模型即便返回
  意料之外的动作字段，也绝不会绕过本地阈值；
- 清洗护栏：空/过短（内容截失）/过长（模型跑偏）一律弃用回原文；
- 解析容错：```json 围栏、前后夹带解说、纯垃圾；
- fail-open：网络异常/非 2xx/不可解析/字段缺失/无 key → 原文放行且不 drop；
- 缓存命中不重复发请求；超长文本截断；空文本不调 API；
- 请求体契约：URL 拼 /chat/completions、json_mode 开关控制 response_format、
  系统提示词沿用 F13 标定判据（锚定商业广告、豁免资源分享）；
- `_build_ai_content` 默认关不构造、开启注入配置；`_reconcile_ai_features`
  热重载（开/关/字段变化重建/无变化保身份/尾斜杠不算变化）；
- Forwarder 集成：drop 拦截、fail-open 放行、清洗文本作为替换基底、源白名单。
全部 mock httpx，不连真实端点。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _build_ai_content, _reconcile_ai_features  # noqa: E402
from tg_forwarder.config import AiContentConfig, RuntimeConfig  # noqa: E402
from tg_forwarder.core.ai_content import (  # noqa: E402
    AD_THRESHOLD,
    MAX_TEXT_CHARS,
    AiContentProcessor,
    Verdict,
    _extract_json,
    build_ai_content,
)


# ---------------------------------------------------------------------------
# mock httpx（同 test_ad_judge 范式）
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    @property
    def text(self):
        return str(self._body)

    def json(self):
        return self._body


class _FakeHttpx:
    """替换 httpx.AsyncClient：记录请求，返回预设响应或抛异常。"""

    def __init__(self, responder):
        self.calls = []
        self.responder = responder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, json=None, **kw):
        self.calls.append({"url": url, "json": json})
        if isinstance(self.responder, Exception):
            raise self.responder
        if callable(self.responder):
            return self.responder(json)
        return self.responder


def _patch_httpx(monkeypatch, responder):
    import httpx

    fake = _FakeHttpx(responder)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)
    return fake


def _chat_resp(content, status=200):
    return _FakeResp({"choices": [{"message": {"content": content}}]}, status=status)


def _proc(**kw):
    kw.setdefault("api_key", "test-key")
    return AiContentProcessor(base_url="http://llm.invalid/v1", **kw)


# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_plain(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_code_fence(self):
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert _extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_surrounded_by_prose(self):
        """模型爱在 JSON 前后加解说，抠花括号兜底。"""
        assert _extract_json('好的：{"a": 1} 以上') == {"a": 1}

    def test_garbage_returns_none(self):
        assert _extract_json("完全不是 JSON") is None
        assert _extract_json("") is None
        assert _extract_json("[1, 2]") is None  # 列表不是 dict → 弃用


class TestVerdictMapping:
    """判定映射：drop 的唯一充分必要条件是 is_ad 且 confidence 过阈值。"""

    def test_hard_ad_drops(self, monkeypatch):
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":true,"ad_type":"sell_account","confidence":0.98,'
            '"changed":false,"cleaned_text":"","reason":"卖号"}'
        ))
        v = asyncio.run(_proc().process("限时开户送500"))
        assert v.drop is True
        assert v.is_ad is True
        assert v.ad_type == "sell_account"
        assert v.reason == "卖号"

    def test_resource_share_passes(self, monkeypatch):
        """F13 标定：资源分享 0.23-0.38 → 放行（锚定商业广告/引流，豁免资源分享）。"""
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":true,"confidence":0.31,"changed":false,"cleaned_text":""}'
        ))
        v = asyncio.run(_proc().process("【附网盘链接】本周新番合集"))
        assert v.drop is False  # 虽 is_ad=true，但 confidence 未过阈值

    def test_low_confidence_passes(self, monkeypatch):
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":true,"confidence":0.40,"changed":false,"cleaned_text":""}'
        ))
        assert asyncio.run(_proc().process("x")).drop is False

    def test_threshold_boundary_exact_drops(self, monkeypatch):
        """confidence == threshold 恰好拦（>=）。"""
        _patch_httpx(monkeypatch, _chat_resp(
            f'{{"is_ad":true,"confidence":{AD_THRESHOLD},"changed":false,'
            '"cleaned_text":""}'
        ))
        assert asyncio.run(_proc().process("x")).drop is True

    def test_normal_content_passes(self, monkeypatch):
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.05,"changed":false,"cleaned_text":"原文"}'
        ))
        v = asyncio.run(_proc().process("美联储维持利率不变"))
        assert v.drop is False
        assert v.cleaned_text is None  # 与原文相同 → 不视为清洗

    def test_model_cannot_forge_drop_below_threshold(self, monkeypatch):
        """回归点：模型自行加 action=drop 也拦不住——阈值由本地说了算。"""
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":true,"confidence":0.10,"action":"drop","drop":true,'
            '"changed":false,"cleaned_text":""}'
        ))
        assert asyncio.run(_proc().process("x")).drop is False

    def test_custom_threshold(self, monkeypatch):
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":true,"confidence":0.5,"changed":false,"cleaned_text":""}'
        ))
        assert asyncio.run(_proc(threshold=0.6).process("x")).drop is False
        assert asyncio.run(_proc(threshold=0.4).process("x")).drop is True

    def test_out_of_range_confidence_clamped(self, monkeypatch):
        """越界置信度收敛到 0~1：1.5 不因「大于阈值」而误拦成 ad，0 也不拦。"""
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":true,"confidence":1.5,"changed":false,"cleaned_text":""}'
        ))
        assert asyncio.run(_proc().process("x")).confidence == 1.0
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":true,"confidence":"很高","changed":false,"cleaned_text":""}'
        ))
        v = asyncio.run(_proc().process("x"))
        assert v.confidence == 0.0 and v.drop is False


class TestCleanGuards:
    """清洗护栏——防丢数据的核心，不能省。"""

    def test_valid_clean_applied(self, monkeypatch):
        original = "今日更新：某软件 v2.1 破解版\n点击查看完整版\n关注公众号送福利"
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"changed":true,'
            '"cleaned_text":"今日更新：某软件 v2.1 破解版"}'
        ))
        v = asyncio.run(_proc().process(original))
        assert v.cleaned_text == "今日更新：某软件 v2.1 破解版"

    def test_too_short_rejected(self, monkeypatch):
        """清洗结果只剩 10% → 判定内容截失，弃用回原文（绝不静默丢内容）。"""
        original = "今日更新：某软件 v2.1 破解版，附网盘链接，自取"
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"changed":true,"cleaned_text":"自取"}'
        ))
        assert asyncio.run(_proc().process(original)).cleaned_text is None

    def test_too_long_rejected(self, monkeypatch):
        """清洗结果膨胀到 5 倍 → 模型跑偏写小作文，弃用（防把垃圾推进频道）。"""
        original = "短消息"
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"changed":true,"cleaned_text":"' + "很长" * 50 + '"}'
        ))
        assert asyncio.run(_proc().process(original)).cleaned_text is None

    def test_empty_clean_rejected(self, monkeypatch):
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"changed":true,"cleaned_text":"   "}'
        ))
        assert asyncio.run(_proc().process("有内容的一条消息")).cleaned_text is None

    def test_non_string_clean_rejected(self, monkeypatch):
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"changed":true,"cleaned_text":123}'
        ))
        assert asyncio.run(_proc().process("有内容的一条消息")).cleaned_text is None

    def test_clean_disabled_keeps_original(self, monkeypatch):
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"changed":true,'
            '"cleaned_text":"今日更新：某软件 v2.1 破解版"}'
        ))
        v = asyncio.run(_proc(clean_enabled=False).process(
            "今日更新：某软件 v2.1 破解版\n点击查看完整版"
        ))
        assert v.cleaned_text is None  # 只判广告，不改文

    def test_ratio_bounds_configurable(self, monkeypatch):
        original = "一二三四五六七八九十" * 3  # 30 字
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"changed":true,"cleaned_text":"一二三四五"}'
        ))
        # 默认 min_ratio=0.3 → 5/30=17% 弃用；放宽到 0.1 → 采纳
        assert asyncio.run(_proc().process(original)).cleaned_text is None
        v = asyncio.run(_proc(min_ratio=0.1).process(original))
        assert v.cleaned_text == "一二三四五"

    def test_long_text_never_cleaned(self, monkeypatch):
        """超长消息降级为「只判广告不改文」（模型只看到前 max_text_chars）。

        回归：模型返回的是「前 4096 字的清洗结果」，长度对全文的占比仍在护栏内
        （本例 5000/10000 = 50%），护栏拦不住它。若无长度门，会用这段 4096 字
        视图整体替换 10000 字原文 —— 抹掉模型没看见的尾巴，属静默丢内容。
        """
        original = "A" * 10000
        assert len(original) > MAX_TEXT_CHARS
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"cleaned_text":"'
            + "A" * 5000
            + '"}'
        ))
        v = asyncio.run(_proc().process(original))
        assert v.drop is False
        assert v.cleaned_text is None  # 不改写 = 不丢尾巴

    def test_short_text_still_cleaned_after_gate_added(self, monkeypatch):
        """门只针对超长文本：正常长度的清洗行为不受影响（防过度收敛）。"""
        original = "今日更新：某软件 v2.1 破解版\n点击查看完整版"
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"cleaned_text":"今日更新：某软件 v2.1 破解版"}'
        ))
        assert asyncio.run(_proc().process(original)).cleaned_text == "今日更新：某软件 v2.1 破解版"

    def test_text_at_boundary_is_cleaned(self, monkeypatch):
        """边界：长度恰好等于 max_text_chars 时模型看得到全文，允许清洗。"""
        original = "x" * MAX_TEXT_CHARS
        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"cleaned_text":"y"}'
        ))
        # 清洗结果 y 相对 4096 字过短 → 由护栏拦下（说明确实走到了清洗分支）
        v = asyncio.run(_proc().process(original))
        assert v.cleaned_text is None

        _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.02,"cleaned_text":"' + "y" * 1600 + '"}'
        ))
        v = asyncio.run(_proc().process(original))
        assert v.cleaned_text is not None  # 40% 长度 → 通过护栏


class TestFailOpen:
    """铁律：任何失败都放行，绝不因 AI 不可用丢消息。"""

    def test_network_error(self, monkeypatch):
        _patch_httpx(monkeypatch, RuntimeError("conn refused"))
        v = asyncio.run(_proc().process("任意消息"))
        assert v == Verdict() and v.drop is False and v.cleaned_text is None

    def test_non_2xx(self, monkeypatch):
        _patch_httpx(monkeypatch, _FakeResp({"detail": "boom"}, status=500))
        assert asyncio.run(_proc().process("x")).drop is False

    def test_unparseable_content(self, monkeypatch):
        _patch_httpx(monkeypatch, _chat_resp("我觉得这条是广告"))
        assert asyncio.run(_proc().process("x")).drop is False

    def test_missing_choices(self, monkeypatch):
        _patch_httpx(monkeypatch, _FakeResp({"model": "x"}))
        assert asyncio.run(_proc().process("x")).drop is False

    def test_missing_fields_defaults_forward(self, monkeypatch):
        """模型漏字段 → 缺 confidence 即 0.0 → 永不 drop。"""
        _patch_httpx(monkeypatch, _chat_resp('{"is_ad":true}'))
        v = asyncio.run(_proc().process("x"))
        assert v.drop is False and v.confidence == 0.0

    def test_json_object_but_not_dict(self, monkeypatch):
        _patch_httpx(monkeypatch, _chat_resp('"一个字符串"'))
        assert asyncio.run(_proc().process("x")).drop is False

    def test_missing_api_key_skips_call(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, _chat_resp('{"is_ad":true,"confidence":1.0}'))
        p = AiContentProcessor(base_url="http://llm.invalid/v1", api_key="")
        assert asyncio.run(p.process("x")) == Verdict()
        assert fake.calls == []

    def test_empty_text_skips_api(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, _chat_resp('{"is_ad":true,"confidence":1.0}'))
        assert asyncio.run(_proc().process("")) == Verdict()
        assert asyncio.run(_proc().process("   ")) == Verdict()
        assert fake.calls == []


class TestRequestShape:
    def test_url_and_payload(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, _chat_resp("{}"))
        asyncio.run(_proc().process("测试消息"))
        call = fake.calls[0]
        assert call["url"] == "http://llm.invalid/v1/chat/completions"
        body = call["json"]
        assert body["model"] == "glm-5.3-flash"
        assert body["messages"][1]["content"] == "测试消息"

    def test_json_mode_toggles_response_format(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, _chat_resp("{}"))
        asyncio.run(_proc(json_mode=True).process("x"))
        assert fake.calls[0]["json"]["response_format"] == {"type": "json_object"}
        fake = _patch_httpx(monkeypatch, _chat_resp("{}"))
        asyncio.run(_proc(json_mode=False).process("x"))
        assert "response_format" not in fake.calls[0]["json"]

    def test_prompt_inherits_f13_calibrated_criteria(self, monkeypatch):
        """判据契约：锚定「商业广告」，且明示资源分享不算广告（F13 09-23 实测标定）。"""
        fake = _patch_httpx(monkeypatch, _chat_resp("{}"))
        asyncio.run(_proc().process("x"))
        sys_prompt = fake.calls[0]["json"]["messages"][0]["content"]
        assert "商业广告" in sys_prompt
        assert "不算广告" in sys_prompt  # 旧措辞无此限定，会把资源分享误杀
        assert "cleaned_text" in sys_prompt  # 清洗契约进提示词

    def test_long_text_truncated(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, _chat_resp("{}"))
        asyncio.run(_proc().process("文" * (MAX_TEXT_CHARS + 500)))
        sent = fake.calls[0]["json"]["messages"][1]["content"]
        assert len(sent) == MAX_TEXT_CHARS


class TestCache:
    def test_same_text_hits_cache(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, _chat_resp(
            '{"is_ad":false,"confidence":0.01,"changed":false,"cleaned_text":""}'
        ))
        p = _proc()
        asyncio.run(p.process("同一条消息"))
        asyncio.run(p.process("同一条消息"))
        assert len(fake.calls) == 1  # 第二次走缓存，不重复计费

    def test_cache_survives_fence_format(self, monkeypatch):
        """缓存里存原始回复，回放时仍要能解析（不能只认裸 JSON）。"""
        fake = _patch_httpx(monkeypatch, _chat_resp(
            '```json\n{"is_ad":false,"confidence":0.01,"changed":false,'
            '"cleaned_text":"缓存回放"}\n```'
        ))
        p = _proc()
        first = asyncio.run(p.process("带围栏的消息正文内容足够长"))
        second = asyncio.run(p.process("带围栏的消息正文内容足够长"))
        assert len(fake.calls) == 1
        assert first.cleaned_text == second.cleaned_text == "缓存回放"


class TestAppliesTo:
    def test_empty_sources_means_all(self):
        p = _proc(sources=[])
        assert p.applies_to(-1001) is True

    def test_whitelist(self):
        p = _proc(sources=[-1001, "-1002"])
        assert p.applies_to(-1001) is True
        assert p.applies_to(-1002) is True
        assert p.applies_to(-1003) is False

    def test_non_numeric_entries_ignored(self):
        p = _proc(sources=["t.me/chan", -1001])
        assert p.applies_to(-1001) is True
        assert p.applies_to(-1009) is False


class TestConfigValidation:
    def test_threshold_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="threshold"):
            AiContentConfig(enabled=True, threshold=1.5)

    def test_ratio_cross_rejected(self):
        """min >= max → 清洗结果永远过不了护栏，功能静默失效，入口必须拒。"""
        with pytest.raises(ValueError, match="min_ratio"):
            AiContentConfig(enabled=True, min_ratio=0.9, max_ratio=0.5)

    def test_defaults_valid(self):
        cfg = AiContentConfig()
        assert 0 < cfg.min_ratio < cfg.max_ratio
        assert cfg.enabled is False

    def test_mirror_defaults_aligned(self):
        """面板镜像默认值必须与 AiContentConfig 逐项一致（漂移=现网行为变化）。"""
        from tg_forwarder.config import _AI_CONTENT_MIRROR_KEYS, SystemSettings

        cfg, s = AiContentConfig(), SystemSettings()
        for k in _AI_CONTENT_MIRROR_KEYS:
            assert getattr(s, k) == getattr(cfg, k[len("ai_content_"):]), k


class TestBuild:
    def test_default_off_returns_none(self):
        assert _build_ai_content(RuntimeConfig()) is None
        assert build_ai_content(None) is None

    def test_enabled_builds_with_config(self):
        cfg = RuntimeConfig()
        cfg.ai_content = AiContentConfig(
            enabled=True, threshold=0.9, clean_enabled=False, sources=[-1001]
        )
        p = _build_ai_content(cfg)
        assert p is not None
        assert p.threshold == 0.9
        assert p.clean_enabled is False
        assert p.applies_to(-1001) is True


def _fwd():
    return SimpleNamespace(ad_judge=None, ai_content=None)


def _with_ai(enabled, **fields):
    cfg = RuntimeConfig()
    cfg.ai_content = AiContentConfig(enabled=enabled, **fields)
    return cfg


class TestReconcile:
    def test_enable_from_none_builds(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_ai(True))
        assert f.ai_content is not None

    def test_disable_removes(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_ai(True))
        _reconcile_ai_features(f, _with_ai(False))
        assert f.ai_content is None

    def test_default_off_stays_none(self):
        f = _fwd()
        _reconcile_ai_features(f, RuntimeConfig())
        assert f.ai_content is None

    def test_unchanged_keeps_identity(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_ai(True))
        old = f.ai_content
        _reconcile_ai_features(f, _with_ai(True))
        assert f.ai_content is old

    def test_base_url_trailing_slash_not_a_change(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_ai(True, base_url="http://x.invalid/v1"))
        old = f.ai_content
        _reconcile_ai_features(f, _with_ai(True, base_url="http://x.invalid/v1/"))
        assert f.ai_content is old

    @pytest.mark.parametrize(
        "field, value, attr",
        [
            ("base_url", "http://y.invalid/v1", "base_url"),
            ("model", "glm-9", "model"),
            ("api_key_env", "OTHER_KEY", "api_key_env"),
            ("threshold", 0.7, "threshold"),
            ("clean_enabled", False, "clean_enabled"),
            ("json_mode", False, "json_mode"),
            ("min_ratio", 0.5, "min_ratio"),
            ("max_ratio", 3.0, "max_ratio"),
            ("max_text_chars", 2048, "max_text_chars"),
            ("timeout", 20.0, "timeout"),
        ],
    )
    def test_any_field_change_rebuilds(self, field, value, attr):
        """非 threshold 字段改动也必须经 /reload 生效（ad_judge rc.6 同款教训）。"""
        f = _fwd()
        _reconcile_ai_features(f, _with_ai(True))
        old = f.ai_content
        _reconcile_ai_features(f, _with_ai(True, **{field: value}))
        assert f.ai_content is not old
        assert getattr(f.ai_content, attr) == value

    def test_sources_change_rebuilds(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_ai(True, sources=[]))
        old = f.ai_content
        _reconcile_ai_features(f, _with_ai(True, sources=[-1001]))
        assert f.ai_content is not old
        assert f.ai_content.applies_to(-1001) is True


# ---------------------------------------------------------------------------
# Forwarder 集成（真实 process_message 管线：位置契约 + fail-open + 清洗）
# ---------------------------------------------------------------------------


class _FakeProcessor:
    def __init__(self, verdict, sources=None):
        self._verdict = verdict
        self.sources = list(sources or [])
        self.calls = []

    def applies_to(self, source_id):
        if not self.sources:
            return True
        return int(source_id) in {int(s) for s in self.sources if str(s).lstrip("-").isdigit()}

    async def process(self, text):
        self.calls.append(text)
        if isinstance(self._verdict, Exception):
            raise self._verdict
        return self._verdict


class TestForwarderIntegration:
    async def _make(self, tmp_path, processor=None, replacements=None):
        from tests.test_core import (
            _CHAT,
            _CatchupClient,
            _Msg,
            _catchup_snap,
            _make_fwd,
        )

        snap = _catchup_snap()
        if replacements:
            snap.replacements = dict(replacements)
        fwd, db = await _make_fwd(tmp_path, snap, _CatchupClient([]))
        fwd.ai_content = processor
        seen = []

        async def stub_send(original, text, target_id, topic_id, s):
            seen.append(text)

        fwd._send_message = stub_send
        return fwd, db, seen, _CHAT, _Msg

    async def test_drop_blocks_message(self, tmp_path):
        fwd, db, seen, _CHAT, _Msg = await self._make(
            tmp_path, _FakeProcessor(Verdict(is_ad=True, confidence=0.98, drop=True))
        )
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="限时开户送500"))
        assert seen == []
        assert fwd._msg_stats["filtered"] == 1
        assert fwd._msg_stats["processed"] == 1
        await db.close()

    async def test_fuzzy_passes_message(self, tmp_path):
        """confidence 未过阈值 → 放行，且不替换正文。"""
        fwd, db, seen, _CHAT, _Msg = await self._make(
            tmp_path, _FakeProcessor(Verdict(is_ad=True, confidence=0.3, drop=False))
        )
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="【附网盘链接】自取"))
        assert seen == ["【附网盘链接】自取"]
        assert fwd._msg_stats["filtered"] == 0
        await db.close()

    async def test_processor_raising_exception_passes(self, tmp_path):
        """process() 抛异常 → forwarder 兜底 catch → fail-open 放行，绝不阻塞管线。"""
        fwd, db, seen, _CHAT, _Msg = await self._make(
            tmp_path, _FakeProcessor(RuntimeError("boom"))
        )
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="任意消息"))
        assert seen == ["任意消息"]
        assert fwd._msg_stats["filtered"] == 0
        await db.close()

    async def test_cleaned_text_becomes_replacement_base(self, tmp_path):
        """清洗文本作替换基底：先剥尾巴，再叠加用户配置的正则替换。"""
        fwd, db, seen, _CHAT, _Msg = await self._make(
            tmp_path,
            _FakeProcessor(Verdict(cleaned_text="新版本 v2 已发布")),
            replacements={"新版本": "最新版"},
        )
        await fwd.process_message(
            _Msg(11, chat_id=_CHAT, text="新版本 v2 已发布\n点击查看完整版")
        )
        assert seen == ["最新版 v2 已发布"]
        await db.close()

    async def test_no_clean_uses_original_as_base(self, tmp_path):
        fwd, db, seen, _CHAT, _Msg = await self._make(
            tmp_path, _FakeProcessor(Verdict()), replacements={"v2": "v3"}
        )
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="新版本 v2 已发布"))
        assert seen == ["新版本 v3 已发布"]
        await db.close()

    async def test_source_not_in_whitelist_skips_call(self, tmp_path):
        p = _FakeProcessor(Verdict(is_ad=True, confidence=0.99, drop=True), sources=[-1])
        fwd, db, seen, _CHAT, _Msg = await self._make(tmp_path, p)
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="限时开户送500"))
        assert p.calls == []  # 源不在白名单 → 一次模型请求都不发
        assert seen == ["限时开户送500"]
        await db.close()

    async def test_default_off_never_calls(self, tmp_path):
        """默认关（ai_content=None）= 绝对零路径，与升级前行为逐字一致。"""
        fwd, db, seen, _CHAT, _Msg = await self._make(tmp_path, None)
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="任意消息"))
        assert seen == ["任意消息"]
        assert fwd._msg_stats["filtered"] == 0
        await db.close()
