# -*- coding: utf-8 -*-
"""F11 AI 翻译测试（step 2/3）。

三组必测：
  (a) 关闭态（translate.enabled=False 默认）→ 不发起任何 API 调用，行为与现状完全一致；
  (b) 开启态 + 成功 → 原文被翻译、结果可缓存（同文本二次不重复调 API）；
  (c) 开启态 + 失败（网络/HTTP/超时）→ 原文放行，绝不丢消息、不影响转发主流程。

无外部依赖：全部 mock httpx.AsyncClient，不连真实 axonhub。
"""
import asyncio
import time

import pytest

from tg_forwarder.config import (
    AdFilterConfig,
    ContentFilterConfig,
    RuntimeConfig,
    SourceConfig,
    SystemSettings,
    TranslateConfig,
)
from tg_forwarder.core.ai_translate import AiTranslator, TranslationCache


# ---------------------------------------------------------------------------
# 辅助：RuntimeConfig + Forwarder 快照（F11 专属）
# ---------------------------------------------------------------------------

_CHAT = -100555


def _snap(translate: TranslateConfig = None, sources=None):
    from tg_forwarder.core.forwarder import Forwarder

    return RuntimeConfig(
        sources=sources or [SourceConfig(identifier=str(_CHAT), resolved_id=_CHAT)],
        settings=SystemSettings(default_target="-100999", forward_new_only=False),
        targets_resolved_default=-100999,
        ad_filter=AdFilterConfig(enable=False),
        content_filter=ContentFilterConfig(enable=False),
        translate=translate or TranslateConfig(),
    )


class _Msg:
    def __init__(self, id, chat_id=_CHAT, text="", media=None, grouped_id=None):
        self.id = id
        self.chat_id = chat_id
        self.text = text
        self.media = media
        self.grouped_id = grouped_id


class _AMStub:
    def __init__(self, client):
        self._c = client

    def healthy_accounts(self):
        return [self._c]


class _Client:
    session_name_for_forwarder = "f11"

    def __init__(self):
        self.sent = []

    async def send_message(self, target_id, message=None, file=None, **kw):
        self.sent.append(message)
        return _Sent()

    def is_connected(self):
        return True

    def iter_messages(self, *a, **k):
        async def gen():
            return
            yield

        return gen()

    async def mark_read(self, *a, **k):
        pass


class _Sent:
    id = 999


async def _make_fwd(tmp_path, snapshot, client):
    import os

    from tg_forwarder.storage.db import Database

    db = Database(os.path.join(str(tmp_path), "f11.sqlite"))
    await db.open()
    await db.migrate()
    from tg_forwarder.core.forwarder import Forwarder

    fwd = Forwarder(db, _AMStub(client))
    fwd.update_snapshot(snapshot)
    return fwd, db


# ---------------------------------------------------------------------------
# TranslationCache（LRU + TTL）
# ---------------------------------------------------------------------------


class TestTranslationCache:
    def test_hit_after_put(self):
        c = TranslationCache(max_entries=8)
        c.put("hello", "你好")
        assert c.get("hello") == "你好"

    def test_miss_when_absent(self):
        c = TranslationCache(max_entries=8)
        assert c.get("nope") is None

    def test_evicts_lru(self):
        c = TranslationCache(max_entries=2)
        c.put("a", "1")
        c.put("b", "2")
        c.get("a")  # 提升 a 为最近使用
        c.put("c", "3")
        assert c.get("b") is None  # b 被逐出
        assert c.get("a") == "1"

    def test_ttl_expiry(self):
        c = TranslationCache(max_entries=8, ttl_seconds=0.1)
        c.put("a", "1")
        assert c.get("a") == "1"
        time.sleep(0.25)
        assert c.get("a") is None  # 过期视为 miss

    def test_ttl_zero_never_expires(self):
        c = TranslationCache(max_entries=8, ttl_seconds=0)
        c.put("a", "1")
        time.sleep(0.05)
        assert c.get("a") == "1"


# ---------------------------------------------------------------------------
# AiTranslator（mock httpx）
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

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

    async def post(self, url, json=None, headers=None, **kw):
        self.calls.append({"url": url, "json": json, "headers": headers})
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


def _ok_responder(json_body):
    def _resp(json):
        content = (json or {}).get("messages", [{}])[-1].get("content", "")
        return _FakeResp(
            {"choices": [{"message": {"content": content}}]}
        )

    return _resp


class TestAiTranslator:
    def test_translate_calls_endpoint(self, monkeypatch):
        fake = _patch_httpx(
            monkeypatch,
            lambda json: _FakeResp(
                {"choices": [{"message": {"content": "Bonjour"}}]}
            ),
        )
        t = AiTranslator(
            endpoint="http://example.invalid/v1",
            api_key="ah-test",
            model="glm-5.3-flash",
            timeout_seconds=5,
        )
        out = asyncio.run(t.translate("hello"))
        assert out == "Bonjour"
        assert fake.calls[0]["url"].endswith("/chat/completions")
        assert fake.calls[0]["json"]["model"] == "glm-5.3-flash"
        assert fake.calls[0]["json"]["messages"][0]["role"] == "user"
        assert fake.calls[0]["json"]["messages"][0]["content"] == "hello"
        assert fake.calls[0]["headers"]["Authorization"] == "Bearer ah-test"

    def test_empty_text_returns_empty_without_call(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, _FakeResp({"choices": []}))
        t = AiTranslator(endpoint="http://example.invalid/v1", api_key="k")
        out = asyncio.run(t.translate(""))
        assert out == ""
        assert fake.calls == []  # 空文本不调 API

    def test_non_2xx_returns_none(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, _FakeResp({"error": "fail"}, status=500))
        t = AiTranslator(endpoint="http://example.invalid/v1", api_key="k")
        out = asyncio.run(t.translate("hello"))
        assert out is None
        assert len(fake.calls) == 1

    def test_network_error_returns_none(self, monkeypatch):
        import httpx

        fake = _patch_httpx(
            monkeypatch, httpx.RequestError("net down", request=None)
        )
        t = AiTranslator(endpoint="http://example.invalid/v1", api_key="k")
        out = asyncio.run(t.translate("hello"))
        assert out is None
        assert len(fake.calls) == 1

    def test_missing_key_disables_translation(self, monkeypatch):
        """api_key 为空（如 env 未配）→ 直接返回 None，绝不发起 HTTP 调用。"""
        # 确定性：显式空字符串即禁用 key，env 是否有值都不回填（Codex minor）
        monkeypatch.delenv("AXONHUB_API_KEY", raising=False)
        fake = _patch_httpx(monkeypatch, _FakeResp({"choices": []}))
        t = AiTranslator(endpoint="http://example.invalid/v1", api_key="")
        out = asyncio.run(t.translate("hello"))
        assert out is None
        assert fake.calls == []

    def test_explicit_empty_key_wins_over_env(self, monkeypatch):
        """显式空 api_key = 明确禁用 key；即使 env 已导出也不回填，不发请求。"""
        monkeypatch.setenv("AXONHUB_API_KEY", "leaked-secret")
        fake = _patch_httpx(monkeypatch, _FakeResp({"choices": []}))
        t = AiTranslator(endpoint="http://example.invalid/v1", api_key="")
        out = asyncio.run(t.translate("hello"))
        assert out is None
        assert fake.calls == []

    def test_none_backfills_env_key(self, monkeypatch):
        """api_key=None（未显式指定）→ 回填 api_key_env 环境变量。"""
        monkeypatch.setenv("AXONHUB_API_KEY", "env-secret")
        t = AiTranslator(endpoint="http://example.invalid/v1")
        assert t._api_key == "env-secret"


# ---------------------------------------------------------------------------
# Forwarder 集成：关闭态 = 现状零变化 / 开启成功 / 失败放行
# ---------------------------------------------------------------------------


class TestForwarderTranslateDisabled:
    async def test_disabled_does_not_call_api_and_passes_original(self, tmp_path, monkeypatch):
        """(a) disabled：无 API 调用，发送原文，与现状完全一致。"""
        client = _Client()
        fwd, db = await _make_fwd(tmp_path, _snap(TranslateConfig(enabled=False)), client)

        # 计数器：若调用任何翻译路径就置位
        called = {"api": False}

        async def _no_api(text, **kw):
            called["api"] = True
            return "translated"

        fwd.translator = type("_Fake", (), {"translate": _no_api})()
        await fwd.process_message(_Msg(11, text="Hello world"))
        assert await db.get_progress(_CHAT) == 11
        assert client.sent == ["Hello world"]  # 原样
        assert called["api"] is False  # 未触发翻译
        await db.close()

    async def test_disabled_source_not_in_list_untouched(self, tmp_path, monkeypatch):
        """开启全局但源不在 translate.sources → 该源仍不翻译。"""
        client = _Client()
        snap = _snap(
            TranslateConfig(enabled=True, sources=["-100OTHER"]),
        )
        fwd, db = await _make_fwd(tmp_path, snap, client)
        flag = {"api": False}

        async def _no_api(text, **kw):
            flag["api"] = True
            return "translated"

        fwd.translator = type("_Fake", (), {"translate": _no_api})()
        await fwd.process_message(_Msg(12, text="Hello world"))
        assert client.sent == ["Hello world"]
        assert flag["api"] is False
        await db.close()


class TestForwarderTranslateSuccess:
    async def test_enabled_source_translates(self, tmp_path, monkeypatch):
        """(b) 开启 + 成功：发送翻译结果。"""
        client = _Client()
        snap = _snap(
            TranslateConfig(
                enabled=True,
                sources=[str(_CHAT)],
                endpoint="http://example.invalid/v1",
                api_key="ah-test",
                model="glm-5.3-flash",
            ),
        )
        fwd, db = await _make_fwd(tmp_path, snap, client)

        # 注入真实 AiTranslator + mock httpx，验证请求到达
        fake = _patch_httpx(
            monkeypatch,
            lambda json: _FakeResp(
                {"choices": [{"message": {"content": "你好世界"}}]}
            ),
        )
        fwd.translator = AiTranslator(
            endpoint="http://example.invalid/v1",
            api_key="ah-test",
            model="glm-5.3-flash",
            timeout_seconds=5,
            cache=TranslationCache(),
        )
        await fwd.process_message(_Msg(13, text="Hello world"))
        assert client.sent == ["你好世界"]
        assert len(fake.calls) == 1  # 恰好一次
        await db.close()

    async def test_cache_avoids_duplicate_call(self, tmp_path, monkeypatch):
        """(b) 同文本二次 → 命中缓存，不重复调 API。"""
        client = _Client()
        snap = _snap(
            TranslateConfig(
                enabled=True,
                sources=[str(_CHAT)],
                endpoint="http://example.invalid/v1",
                api_key="ah-test",
            ),
        )
        fwd, db = await _make_fwd(tmp_path, snap, client)

        fake = _patch_httpx(
            monkeypatch,
            lambda json: _FakeResp(
                {"choices": [{"message": {"content": "你好世界"}}]}
            ),
        )
        cache = TranslationCache(max_entries=16)
        fwd.translator = AiTranslator(
            endpoint="http://example.invalid/v1",
            api_key="ah-test",
            timeout_seconds=5,
            cache=cache,
        )
        await fwd.process_message(_Msg(14, text="Hello world"))
        await fwd.process_message(_Msg(15, text="Hello world"))  # 同文本
        assert client.sent == ["你好世界", "你好世界"]
        assert len(fake.calls) == 1  # 第二次命中缓存
        await db.close()


class TestForwarderTranslateFailure:
    async def test_failure_passes_original_no_drop(self, tmp_path, monkeypatch):
        """(c) 失败（网络错误）→ 原文放行，不丢消息、不阻塞。"""
        import httpx

        client = _Client()
        snap = _snap(
            TranslateConfig(
                enabled=True,
                sources=[str(_CHAT)],
                endpoint="http://example.invalid/v1",
                api_key="ah-test",
            ),
        )
        fwd, db = await _make_fwd(tmp_path, snap, client)

        fake = _patch_httpx(
            monkeypatch, httpx.RequestError("net down", request=None)
        )
        fwd.translator = AiTranslator(
            endpoint="http://example.invalid/v1",
            api_key="ah-test",
            timeout_seconds=5,
            cache=TranslationCache(),
        )
        await fwd.process_message(_Msg(16, text="Hello world"))
        assert client.sent == ["Hello world"]  # 原文放行
        assert await db.get_progress(_CHAT) == 16  # 进度照常
        assert len(fake.calls) == 1
        await db.close()

    async def test_failure_non2xx_passes_original(self, tmp_path, monkeypatch):
        client = _Client()
        snap = _snap(
            TranslateConfig(
                enabled=True,
                sources=[str(_CHAT)],
                endpoint="http://example.invalid/v1",
                api_key="ah-test",
            ),
        )
        fwd, db = await _make_fwd(tmp_path, snap, client)
        fake = _patch_httpx(monkeypatch, _FakeResp({"error": "boom"}, status=503))
        fwd.translator = AiTranslator(
            endpoint="http://example.invalid/v1",
            api_key="ah-test",
            timeout_seconds=5,
            cache=TranslationCache(),
        )
        await fwd.process_message(_Msg(17, text="Hello world"))
        assert client.sent == ["Hello world"]
        assert fake.calls == [fake.calls[0]]  # 只调一次
        await db.close()


class TestTranslateConfigSchema:
    def test_defaults_off(self):
        c = TranslateConfig()
        assert c.enabled is False
        assert c.sources == []
        assert c.endpoint == "http://100.64.0.2:8091/v1"
        assert c.model == "glm-5.3-flash"

    def test_runtime_config_default_off(self):
        cfg = RuntimeConfig()
        assert cfg.translate.enabled is False
        assert cfg.translate.sources == []

    def test_negative_timeout_rejected(self):
        with pytest.raises(ValueError):
            TranslateConfig(timeout_seconds=-1)

    def test_bootstrap_maps_translate(self, tmp_path):
        """yaml 可配置 translate 段（bootstrap_from_yaml）。"""
        from tg_forwarder.config import bootstrap_from_yaml

        import os

        p = os.path.join(str(tmp_path), "cfg.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(
                "translate:\n"
                "  enabled: true\n"
                "  sources: ['-100555']\n"
            )
        cfg = bootstrap_from_yaml(p)
        assert cfg.translate.enabled is True
        assert cfg.translate.sources == ["-100555"]