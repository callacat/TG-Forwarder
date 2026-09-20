# -*- coding: utf-8 -*-
"""F10 AI digest 单测（全 mock，不连真实端点；默认关=现网行为零变化）。

覆盖验收关键三态：
  (a) 关闭时：零 API 调用、零缓冲、与 legacy 行为完全一致；
  (b) 开启时：滚动窗口聚合 → LLM 摘要 → 发贴到默认目标；
  (c) API 失败：不崩溃、不阻塞转发主流程（fail-open）。
"""
import asyncio
from typing import List, Optional

import pytest

from tg_forwarder.core.digest import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DigestPipeline,
    LLMClient,
    RollingWindow,
    build_digest_prompt,
)


# ---------------------------------------------------------------------------
# 通用构建
# ---------------------------------------------------------------------------


def _cfg(**overrides):
    from tg_forwarder.config import RuntimeConfig

    cfg = RuntimeConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _digest_cfg(**kw):
    from tg_forwarder.config import DigestConfig

    defaults = dict(enabled=True, interval_seconds=1800)
    defaults.update(kw)
    return DigestConfig(**defaults)


class _Clock:
    """可控时钟：pipeline now_fn。"""

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class _Msg:
    def __init__(self, id: int, text: str = "", chat_id: int = -1001, media=None):
        self.id = id
        self.text = text
        self.chat_id = chat_id
        self.media = media


class _DigestClient:
    """记录 send_message 的 fake client（对齐 _SendRecordingClient）。"""

    session_name_for_forwarder = "digest"

    def __init__(self):
        self.sent: List[dict] = []

    async def send_message(self, target_id, message=None, file=None, **kw):
        class _Sent:
            id = 1

        self.sent.append({"target_id": target_id, "message": message, "file": file, **kw})
        return _Sent()

    async def mark_read(self, *a, **k):
        pass


class _FakeFwd:
    """轻量 forwarder 替身：get_snapshot/_get_next_client 两个接口。"""

    def __init__(self, snapshot, client):
        self._snapshot = snapshot
        self._client = client

    def get_snapshot(self):
        return self._snapshot

    def _get_next_client(self):
        return self._client


class _LLM:
    """可控 LLM：返回固定摘要或抛指定异常。"""

    def __init__(self, reply: str = "摘要内容", fail: Optional[Exception] = None):
        self.reply = reply
        self.fail = fail
        self.calls = 0
        self.last_prompt: Optional[str] = None

    async def summarize(self, prompt: str) -> str:
        self.calls += 1
        self.last_prompt = prompt
        if self.fail is not None:
            raise self.fail
        return self.reply


def _src(chat_id: int = -1001, digest_enabled: bool = True):
    from tg_forwarder.config import SourceConfig

    return SourceConfig(identifier=str(chat_id), resolved_id=chat_id, digest_enabled=digest_enabled)


def _digest_snap(enabled: bool = True, interval: int = 1800, src_digest: bool = True, target: int = -100999):
    from tg_forwarder.config import (
        AdFilterConfig,
        ContentFilterConfig,
        SystemSettings,
    )

    return _cfg(
        sources=[_src(-1001, src_digest), _src(-1002, False)],  # 二号源默认不聚合
        settings=SystemSettings(default_target="-100999"),
        targets_resolved_default=target,
        ad_filter=AdFilterConfig(enable=False),
        content_filter=ContentFilterConfig(enable=False),
        digest=_digest_cfg(enabled=enabled, interval_seconds=interval),
    )


# ---------------------------------------------------------------------------
# 1. 配置默认值（schema）
# ---------------------------------------------------------------------------


class TestDigestConfig:
    def test_defaults_off_and_1800(self):
        from tg_forwarder.config import DigestConfig

        d = DigestConfig()
        assert d.enabled is False
        assert d.interval_seconds == 1800

    def test_default_endpoint_and_model(self):
        from tg_forwarder.config import DigestConfig

        d = DigestConfig()
        assert d.base_url == DEFAULT_BASE_URL == "http://100.64.0.2:8091/v1"
        assert d.model == DEFAULT_MODEL == "glm-5.3-flash"

    def test_source_flag_default_false(self):
        from tg_forwarder.config import SourceConfig

        s = SourceConfig(identifier=1)
        assert s.digest_enabled is False

    def test_runtime_config_has_digest_section(self):
        from tg_forwarder.config import DigestConfig, RuntimeConfig

        cfg = RuntimeConfig()
        assert isinstance(cfg.digest, DigestConfig)
        assert cfg.digest.enabled is False

    async def test_bootstrap_parses_digest_section(self, tmp_path):
        """yaml 含 digest 段 → bootstrap_from_yaml 解析（基础设施段，与 link_checker 同源）。"""
        import os

        from tg_forwarder.config import bootstrap_from_yaml

        p = os.path.join(str(tmp_path), "config.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(
                "accounts:\n"
                "  - api_id: 123456\n"
                "    api_hash: 'h'\n"
                "    session_name: 'a1'\n"
                "digest:\n"
                "  enabled: true\n"
                "  interval_seconds: 600\n"
                "  base_url: 'http://x.test/v1'\n"
                "  model: 'm-test'\n"
            )
        cfg = bootstrap_from_yaml(p)
        assert cfg.digest.enabled is True
        assert cfg.digest.interval_seconds == 600
        assert cfg.digest.base_url == "http://x.test/v1"
        assert cfg.digest.model == "m-test"


# ---------------------------------------------------------------------------
# 2. 纯函数：prompt 构建
# ---------------------------------------------------------------------------


class TestBuildDigestPrompt:
    def test_contains_source_label_and_messages(self):
        prompt = build_digest_prompt("测试频道", [_Msg(1, "第一条消息"), _Msg(2, "第二条消息")])
        assert "测试频道" in prompt
        assert "第一条消息" in prompt
        assert "第二条消息" in prompt

    def test_media_only_message_placeholder(self):
        prompt = build_digest_prompt("源", [_Msg(1)])
        assert "[媒体消息]" in prompt

    def test_empty_messages_ok(self):
        assert build_digest_prompt("源", []).strip() != ""


# ---------------------------------------------------------------------------
# 3. RollingWindow
# ---------------------------------------------------------------------------


class TestRollingWindow:
    def test_add_and_drain(self):
        w = RollingWindow(opened_at=100.0, interval=1800.0)
        w.add(_Msg(1, "a"))
        w.add(_Msg(2, "b"))
        assert len(w.messages) == 2
        drained = w.drain()
        assert len(drained) == 2
        assert w.messages == []

    def test_due_after_interval(self):
        w = RollingWindow(opened_at=100.0, interval=1800.0)
        assert w.due(100.0 + 1799.0) is False
        assert w.due(100.0 + 1800.0) is True


# ---------------------------------------------------------------------------
# 4. LLMClient（OpenAI 兼容端点，mock httpx）
# ---------------------------------------------------------------------------


class _FakeAsyncClient:
    """httpx.AsyncClient 替身：记录 payload，返回固定 choices。"""

    def __init__(self, status_code: int = 200, body=None, raise_error: Optional[Exception] = None):
        self.status_code = status_code
        self.body = body if body is not None else {
            "choices": [{"message": {"content": "  模型生成的摘要  "}}]
        }
        self.raise_error = raise_error
        self.calls: List[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.raise_error is not None:
            raise self.raise_error

        class _Resp:
            status_code = self.status_code
            body = self.body

            def raise_for_status(self):
                if self.status_code >= 400:
                    import httpx

                    req = httpx.Request("POST", url)
                    raise httpx.HTTPStatusError(
                        f"HTTP {self.status_code}", request=req, response=None
                    )

            def json(self):
                return self.body

        resp = _Resp()
        resp.raise_for_status()
        return resp


def _patch_httpx(monkeypatch, fake):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)


class TestLLMClient:
    def test_defaults(self):
        c = LLMClient()
        assert c.base_url == "http://100.64.0.2:8091/v1"
        assert c.model == "glm-5.3-flash"

    async def test_summarize_returns_stripped_content(self, monkeypatch):
        fake = _FakeAsyncClient()
        _patch_httpx(monkeypatch, fake)
        c = LLMClient()
        out = await c.summarize("prompt")
        assert out == "模型生成的摘要"  # strip
        assert fake.calls[0]["url"] == "http://100.64.0.2:8091/v1/chat/completions"
        assert fake.calls[0]["json"]["model"] == "glm-5.3-flash"
        assert fake.calls[0]["json"]["messages"][-1]["content"] == "prompt"

    async def test_api_key_header_when_set(self, monkeypatch):
        fake = _FakeAsyncClient()
        _patch_httpx(monkeypatch, fake)
        c = LLMClient(api_key="sk-test")
        await c.summarize("p")
        assert fake.calls[0]["headers"].get("Authorization") == "Bearer sk-test"

    async def test_network_error_raises_for_caller_failopen(self, monkeypatch):
        import httpx

        fake = _FakeAsyncClient(raise_error=httpx.RequestError("net down"))
        _patch_httpx(monkeypatch, fake)
        c = LLMClient()
        with pytest.raises(httpx.RequestError):
            await c.summarize("p")


# ---------------------------------------------------------------------------
# 5. DigestPipeline 核心（(a) 关闭态 / (b) 成功 / (c) 失败降级）
# ---------------------------------------------------------------------------


class TestDigestPipelineDisabled:
    """(a) 默认关：零缓冲、零 API 调用、与 legacy 完全一致。"""

    def test_disabled_no_buffering(self):
        llm = _LLM()
        clock = _Clock(0.0)
        pipe = DigestPipeline(llm=llm, now_fn=clock)
        snap = _digest_snap(enabled=False)
        pipe.add_message(_src(-1001, True), snap, _Msg(1, "hello"))
        assert pipe._windows == {}

    async def test_disabled_no_api_calls_and_returns_zero(self):
        llm = _LLM()
        clock = _Clock(0.0)
        pipe = DigestPipeline(llm=llm, now_fn=clock)
        snap = _digest_snap(enabled=False)
        client = _DigestClient()
        n = await pipe.flush(_FakeFwd(snap, client))
        assert n == 0
        assert llm.calls == 0
        assert client.sent == []

    async def test_disabled_source_flag_ignored(self):
        """digest 全局关 + 源开 → 仍然不聚合（双开关 AND）。"""
        llm = _LLM()
        pipe = DigestPipeline(llm=llm, now_fn=_Clock(0.0))
        snap = _digest_snap(enabled=False, src_digest=True)
        pipe.add_message(_src(-1001, True), snap, _Msg(1, "x"))
        assert pipe._windows == {}

    def test_should_buffer_false_when_off(self):
        pipe = DigestPipeline(llm=_LLM(), now_fn=_Clock())
        assert pipe.should_buffer(_src(-1001, True), _digest_snap(enabled=False)) is False


class TestDigestPipelineEnabled:
    """(b) 开启态：按源聚合 → 窗口到期摘要 → 发贴。"""

    def test_buffers_only_digest_enabled_sources(self):
        pipe = DigestPipeline(llm=_LLM(), now_fn=_Clock(0.0))
        snap = _digest_snap(enabled=True)
        pipe.add_message(_src(-1001, True), snap, _Msg(1, "新闻A"))
        pipe.add_message(_src(-1002, False), snap, _Msg(2, "非摘要源"))
        assert set(pipe._windows.keys()) == {-1001}

    def test_window_opens_at_first_message_with_interval(self):
        pipe = DigestPipeline(llm=_LLM(), now_fn=_Clock(100.0))
        snap = _digest_snap(enabled=True, interval=1800)
        pipe.add_message(_src(-1001, True), snap, _Msg(1, "x"))
        w = pipe._windows[-1001]
        assert w.opened_at == 100.0
        assert w.interval == 1800.0

    async def test_successful_digest_generation_and_post(self):
        """窗口到期 → LLM 生成 → 发贴到默认目标；缓冲排空。"""
        llm = _LLM(reply="今日新闻摘要")
        clock = _Clock(0.0)
        pipe = DigestPipeline(llm=llm, now_fn=clock)
        snap = _digest_snap(enabled=True, interval=1800)
        pipe.add_message(_src(-1001, True), snap, _Msg(1, "新闻A"))
        pipe.add_message(_src(-1001, True), snap, _Msg(2, "新闻B"))
        client = _DigestClient()

        # 窗口未到期 → flush 不动作
        n = await pipe.flush(_FakeFwd(snap, client))
        assert n == 0
        assert llm.calls == 0
        assert client.sent == []

        # 到期 → 聚合摘要并发贴
        clock.t = 1800.0
        n = await pipe.flush(_FakeFwd(snap, client))
        assert n == 1
        assert llm.calls == 1
        assert "新闻A" in (llm.last_prompt or "")
        assert "新闻B" in (llm.last_prompt or "")
        assert len(client.sent) == 1
        assert client.sent[0]["target_id"] == -100999
        assert client.sent[0]["message"] == "今日新闻摘要"
        # 缓冲已排空（下轮不再重复）
        assert pipe._windows[-1001].messages == []

    async def test_digest_posts_via_forwarder_client(self):
        """发贴复用转发 client（_get_next_client），走纯文本 send_message。"""
        llm = _LLM(reply="摘要")
        clock = _Clock(0.0)
        pipe = DigestPipeline(llm=llm, now_fn=clock)
        snap = _digest_snap(enabled=True, interval=60)
        pipe.add_message(_src(-1001, True), snap, _Msg(1, "内容"))
        client = _DigestClient()
        clock.t = 60.0
        await pipe.flush(_FakeFwd(snap, client))
        call = client.sent[0]
        assert call["file"] is None
        assert call.get("parse_mode") is None

    async def test_no_target_skips_post(self):
        llm = _LLM(reply="摘要")
        clock = _Clock(0.0)
        pipe = DigestPipeline(llm=llm, now_fn=clock)
        snap = _digest_snap(enabled=True, interval=60, target=None)
        pipe.add_message(_src(-1001, True), snap, _Msg(1, "内容"))
        client = _DigestClient()
        clock.t = 60.0
        n = await pipe.flush(_FakeFwd(snap, client))
        assert n == 0
        assert client.sent == []


class TestDigestPipelineFailure:
    """(c) API 失败：不抛、不阻塞转发主流程，窗口已排空可重试。"""

    async def test_api_failure_no_crash_and_zero_count(self):
        llm = _LLM(fail=RuntimeError("endpoint down"))
        clock = _Clock(0.0)
        pipe = DigestPipeline(llm=llm, now_fn=clock)
        snap = _digest_snap(enabled=True, interval=60)
        pipe.add_message(_src(-1001, True), snap, _Msg(1, "内容"))
        client = _DigestClient()
        clock.t = 60.0

        n = await pipe.flush(_FakeFwd(snap, client))  # 不抛
        assert n == 0
        assert llm.calls == 1
        assert client.sent == []  # 失败不发贴
        assert pipe._windows[-1001].messages == []  # 缓冲清空（下次重来）

    async def test_llm_returns_empty_skips_post(self):
        llm = _LLM(reply="")
        clock = _Clock(0.0)
        pipe = DigestPipeline(llm=llm, now_fn=clock)
        snap = _digest_snap(enabled=True, interval=60)
        pipe.add_message(_src(-1001, True), snap, _Msg(1, "x"))
        client = _DigestClient()
        clock.t = 60.0
        n = await pipe.flush(_FakeFwd(snap, client))
        assert n == 0
        assert client.sent == []

    async def test_next_flush_works_after_failure(self):
        """失败后窗口重建，端点恢复可再次出摘要（降级不锁死）。"""
        llm = _LLM(fail=RuntimeError("down"))
        clock = _Clock(0.0)
        pipe = DigestPipeline(llm=llm, now_fn=clock)
        snap = _digest_snap(enabled=True, interval=60)
        pipe.add_message(_src(-1001, True), snap, _Msg(1, "x"))
        client = _DigestClient()
        clock.t = 60.0
        await pipe.flush(_FakeFwd(snap, client))  # 失败
        # 端点恢复
        llm.fail = None
        llm.reply = "恢复后摘要"
        clock.t = 60.0  # 窗口刚排空，重新加入消息再走一轮
        pipe.add_message(_src(-1001, True), snap, _Msg(2, "y"))
        clock.t = 60.0 + 60.0
        n = await pipe.flush(_FakeFwd(snap, client))
        assert n == 1
        assert client.sent[-1]["message"] == "恢复后摘要"


# ---------------------------------------------------------------------------
# 6. Forwarder 集成：关闭=legacy 等价；开启+失败=不阻塞转发主流程
# ---------------------------------------------------------------------------


class _SendRecordingClient:
    """对齐 test_core._SendRecordingClient：记录 send 调用。"""

    session_name_for_forwarder = "digestfwd"

    def __init__(self):
        self.calls: List[dict] = []

    async def send_message(self, target_id, message=None, file=None, **kw):
        class _Sent:
            id = 42

        self.calls.append({"target_id": target_id, "message": message, "file": file, **kw})
        return _Sent()

    async def mark_read(self, *a, **k):
        pass


class _AMStub:
    def __init__(self, client):
        self._c = client

    def healthy_accounts(self):
        return [self._c]

    def all_status(self):
        return {"accounts": []}

    def record_flood_wait(self, *a, **k):
        pass

    def touch(self, *a, **k):
        pass


async def _make_fwd(tmp_path, snapshot, client):
    import os

    from tg_forwarder.core.forwarder import Forwarder
    from tg_forwarder.storage.db import Database

    db = Database(os.path.join(str(tmp_path), "d.sqlite"))
    await db.open()
    await db.migrate()
    fwd = Forwarder(db, _AMStub(client))
    fwd.update_snapshot(snapshot)
    return fwd, db


def _forward_snap(digest_enabled: bool, src_digest: bool):
    from tg_forwarder.config import (
        AdFilterConfig,
        ContentFilterConfig,
        DigestConfig,
        SourceConfig,
        SystemSettings,
    )

    return _cfg(
        sources=[
            SourceConfig(
                identifier="-1001", resolved_id=-1001,
                digest_enabled=src_digest,
            )
        ],
        settings=SystemSettings(default_target="-100999"),
        targets_resolved_default=-100999,
        ad_filter=AdFilterConfig(enable=False),
        content_filter=ContentFilterConfig(enable=False),
        digest=DigestConfig(enabled=digest_enabled, interval_seconds=60),
    )


class TestDigestForwarderIntegration:
    async def test_disabled_forwarding_identical_to_legacy(self, tmp_path):
        """digest 关：process_message 照常转发（legacy 等价），零缓冲。"""
        client = _SendRecordingClient()
        fwd, db = await _make_fwd(tmp_path, _forward_snap(False, True), client)
        # process_message 读的是 self.digest_pipeline（Codex minor #3：原 _digest 赋值是空洞断言）
        fwd.digest_pipeline = DigestPipeline(llm=_LLM(), now_fn=_Clock(0.0))
        await fwd.process_message(_Msg(11, "普通文本", chat_id=-1001))
        assert len(client.calls) == 1  # 照常转发
        assert client.calls[0]["message"] == "普通文本"
        assert fwd.digest_pipeline._windows == {}  # 零缓冲（digest 关，should_buffer=False）
        await db.close()

    async def test_enabled_buffers_and_forwarding_continues(self, tmp_path):
        """digest 开：消息既照常转发，又进入 digest 缓冲。"""
        llm = _LLM()
        clock = _Clock(0.0)
        client = _SendRecordingClient()
        fwd, db = await _make_fwd(tmp_path, _forward_snap(True, True), client)
        fwd.digest_pipeline = DigestPipeline(llm=llm, now_fn=clock)
        fwd.digest_pipeline._fwd = fwd  # 集成注入
        await fwd.process_message(_Msg(11, "新闻A", chat_id=-1001))
        assert len(client.calls) == 1  # 转发不中断
        assert len(fwd.digest_pipeline._windows[-1001].messages) == 1  # 已缓冲
        await db.close()

    async def test_api_failure_does_not_block_forwarding_loop(self, tmp_path):
        """核心验收 (c)：LLM 端点挂了 → process_message 不炸、flush 不炸、转发照常。"""
        llm = _LLM(fail=RuntimeError("endpoint down"))
        clock = _Clock(0.0)
        client = _SendRecordingClient()
        fwd, db = await _make_fwd(tmp_path, _forward_snap(True, True), client)
        pipe = DigestPipeline(llm=llm, now_fn=clock)
        pipe._fwd = fwd
        fwd.digest_pipeline = pipe
        # 多轮消息 + 到期 flush：全链路不抛
        for i in range(3):
            await fwd.process_message(_Msg(10 + i, f"新闻{i}", chat_id=-1001))
        clock.t = 60.0
        n = await pipe.flush()  # 端点失败 → 0，不抛
        assert n == 0
        assert len(client.calls) == 3  # 转发主流程 3 条全发出
        assert llm.calls == 1
        await db.close()