# -*- coding: utf-8 -*-
"""AI 广告判别器（jev-1.13 System One）测试。

覆盖 spec 契约：
- mock httpx 四态：广告(True) / 正常(False) / 模糊(None) / 异常(None，fail-open)；
- 阈值边界：>= threshold 判广告、[fuzzy_low, threshold) 模糊放行、< fuzzy_low 判正常；
- 空文本不调 API；超长文本截断（MAX_TEXT_CHARS）；
- `_build_ad_judge` 默认关不构造、开启时注入配置；
- `_reconcile_ai_features` ad_judge 热重载（开/关/阈值变化重建/无变化保留）；
- Forwarder 集成：verdict=True 拦截（进 filtered）、None/False 放行、
  默认关（ad_judge=None）行为与升级前一致。
全部 mock httpx，不连真实 jev 端点。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _build_ad_judge, _reconcile_ai_features  # noqa: E402
from tg_forwarder.config import AdJudgeConfig, RuntimeConfig  # noqa: E402
from tg_forwarder.core.ad_judge import (  # noqa: E402
    AD_THRESHOLD,
    FUZZY_LOW,
    MAX_TEXT_CHARS,
    AdJudge,
)


# ---------------------------------------------------------------------------
# mock httpx（同 test_ai_translate._FakeHttpx 范式）
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


def _noul_resp(score):
    return _FakeResp({"answers": {"is_ad": {"type": "noul", "noul": score}}})


def _judge(**kw):
    return AdJudge(base_url="http://jev.invalid", **kw)


class TestAdJudgeVerdicts:
    """四态 + 阈值边界（spec 核心契约）。"""

    def test_ad_score_returns_true(self, monkeypatch):
        _patch_httpx(monkeypatch, _noul_resp(0.98))
        out = asyncio.run(_judge().is_ad("限时开户送500"))
        assert out is True

    def test_normal_score_returns_false(self, monkeypatch):
        _patch_httpx(monkeypatch, _noul_resp(0.03))
        out = asyncio.run(_judge().is_ad("美联储维持利率不变"))
        assert out is False

    def test_fuzzy_score_returns_none(self, monkeypatch):
        """[0.60, 0.85) 模糊区 → None（fail-open 放行）。"""
        _patch_httpx(monkeypatch, _noul_resp(0.68))
        out = asyncio.run(_judge().is_ad("【附网盘链接】本周新番合集"))
        assert out is None

    def test_network_error_returns_none(self, monkeypatch):
        import httpx

        fake = _patch_httpx(
            monkeypatch, httpx.RequestError("net down", request=None)
        )
        out = asyncio.run(_judge().is_ad("任意消息"))
        assert out is None
        assert len(fake.calls) == 1

    def test_non_2xx_returns_none(self, monkeypatch):
        _patch_httpx(monkeypatch, _FakeResp({"detail": "err"}, status=500))
        out = asyncio.run(_judge().is_ad("任意消息"))
        assert out is None

    def test_missing_answers_returns_none(self, monkeypatch):
        _patch_httpx(monkeypatch, _FakeResp({"model": "jev-1.13"}))
        out = asyncio.run(_judge().is_ad("任意消息"))
        assert out is None

    def test_threshold_boundary_exact(self, monkeypatch):
        """得分 == threshold 恰好判广告（>=）。"""
        _patch_httpx(monkeypatch, _noul_resp(AD_THRESHOLD))
        assert asyncio.run(_judge().is_ad("x")) is True

    def test_fuzzy_low_boundary_is_fuzzy(self, monkeypatch):
        """得分 == fuzzy_low 仍在模糊区（>= fuzzy_low 且 < threshold → None）。"""
        _patch_httpx(monkeypatch, _noul_resp(FUZZY_LOW))
        assert asyncio.run(_judge().is_ad("x")) is None

    def test_just_below_fuzzy_low_returns_false(self, monkeypatch):
        _patch_httpx(monkeypatch, _noul_resp(FUZZY_LOW - 0.01))
        assert asyncio.run(_judge().is_ad("x")) is False

    def test_empty_text_skips_api(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, _noul_resp(0.98))
        assert asyncio.run(_judge().is_ad("")) is None
        assert asyncio.run(_judge().is_ad("   ")) is None
        assert fake.calls == []  # 空文本不调 API

    def test_long_text_truncated(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, _noul_resp(0.02))
        long_text = "文" * (MAX_TEXT_CHARS + 500)
        asyncio.run(_judge().is_ad(long_text))
        state = fake.calls[0]["json"]["state"]
        assert len(state) <= MAX_TEXT_CHARS + 20  # 前缀 + 截断后正文
        assert len(state) < len(long_text)

    def test_request_shape(self, monkeypatch):
        """协议契约：POST /v1/systemone、model=jev-1.13、noul 题型 + criteria。"""
        fake = _patch_httpx(monkeypatch, _noul_resp(0.5))
        asyncio.run(_judge().is_ad("测试消息"))
        call = fake.calls[0]
        assert call["url"].endswith("/v1/systemone")
        body = call["json"]
        assert body["model"] == "jev-1.13"
        assert body["state"].endswith("测试消息")
        q = body["questions"]["is_ad"]
        assert q["type"] == "noul"
        assert set(q["criteria"]) == {"true", "false"}


class TestBuildAdJudge:
    """main.py 装配：默认关不构造（现网零变化）、开启注入配置。"""

    def test_default_off_returns_none(self):
        assert _build_ad_judge(RuntimeConfig()) is None

    def test_enabled_builds_with_config(self):
        cfg = RuntimeConfig()
        cfg.ad_judge = AdJudgeConfig(enabled=True, threshold=0.9, fuzzy_low=0.5)
        judge = _build_ad_judge(cfg)
        assert judge is not None
        assert judge.threshold == 0.9
        assert judge.fuzzy_low == 0.5
        assert judge.model == "jev-1.13"


def _fwd():
    return SimpleNamespace(ad_judge=None)


def _with_ad(enabled, threshold=0.85):
    cfg = RuntimeConfig()
    cfg.ad_judge = AdJudgeConfig(enabled=enabled, threshold=threshold)
    return cfg


class TestReconcileAdJudge:
    """热重载对齐（照 F12 范式）。"""

    def test_enable_from_none_builds(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_ad(True))
        assert f.ad_judge is not None

    def test_disable_removes(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_ad(True))
        _reconcile_ai_features(f, _with_ad(False))
        assert f.ad_judge is None

    def test_threshold_change_rebuilds(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_ad(True, 0.8))
        old = f.ad_judge
        _reconcile_ai_features(f, _with_ad(True, 0.9))
        assert f.ad_judge is not old
        assert f.ad_judge.threshold == 0.9

    def test_unchanged_keeps_identity(self):
        """无变化不重建（避免每次普通设置保存丢状态）。"""
        f = _fwd()
        _reconcile_ai_features(f, _with_ad(True, 0.85))
        old = f.ad_judge
        _reconcile_ai_features(f, _with_ad(True, 0.85))
        assert f.ad_judge is old

    def test_default_off_stays_none(self):
        f = _fwd()
        _reconcile_ai_features(f, RuntimeConfig())
        assert f.ad_judge is None


# ---------------------------------------------------------------------------
# Forwarder 集成（真实 process_message 管线：位置契约 + fail-open）
# ---------------------------------------------------------------------------


class TestForwarderIntegration:
    async def _make(self, tmp_path, ad_judge):
        from tests.test_core import (
            _CatchupClient,
            _CHAT,
            _Msg,
            _catchup_snap,
            _make_fwd,
        )

        fwd, db = await _make_fwd(tmp_path, _catchup_snap(), _CatchupClient([]))
        if ad_judge is not None:
            fwd.ad_judge = ad_judge
        return fwd, db, _CHAT, _Msg

    async def test_verdict_true_blocks_message(self, tmp_path):
        """verdict=True → 拦截：进 filtered 计数、不发给目标（应在去重之前）。"""

        class _AdTrue:
            async def is_ad(self, text):
                return True

        fwd, db, _CHAT, _Msg = await self._make(tmp_path, _AdTrue())
        seen = []

        async def stub_send(original, text, target_id, topic_id, snap):
            seen.append(text)

        fwd._send_message = stub_send
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="限时开户送500"))
        assert seen == []
        assert fwd._msg_stats["filtered"] == 1
        assert fwd._msg_stats["processed"] == 1
        await db.close()

    async def test_verdict_none_passes_message(self, tmp_path):
        """verdict=None（模糊/失败）→ fail-open 放行：消息照常转发。"""

        class _AdNone:
            async def is_ad(self, text):
                return None

        fwd, db, _CHAT, _Msg = await self._make(tmp_path, _AdNone())
        seen = []

        async def stub_send(original, text, target_id, topic_id, snap):
            seen.append(text)

        fwd._send_message = stub_send
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="【附网盘链接】自取"))
        assert seen == ["【附网盘链接】自取"]
        assert fwd._msg_stats["filtered"] == 0
        await db.close()

    async def test_verdict_false_passes_message(self, tmp_path):
        """verdict=False（正常内容）→ 放行。"""

        class _AdFalse:
            async def is_ad(self, text):
                return False

        fwd, db, _CHAT, _Msg = await self._make(tmp_path, _AdFalse())
        seen = []

        async def stub_send(original, text, target_id, topic_id, snap):
            seen.append(text)

        fwd._send_message = stub_send
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="美联储维持利率不变"))
        assert seen == ["美联储维持利率不变"]
        assert fwd._msg_stats["filtered"] == 0
        await db.close()

    async def test_judge_raising_exception_passes(self, tmp_path):
        """is_ad 内部抛异常 → forwarder 兜底 catch → fail-open 放行，绝不阻塞管线。"""

        class _AdBoom:
            async def is_ad(self, text):
                raise RuntimeError("boom")

        fwd, db, _CHAT, _Msg = await self._make(tmp_path, _AdBoom())
        seen = []

        async def stub_send(original, text, target_id, topic_id, snap):
            seen.append(text)

        fwd._send_message = stub_send
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="任意消息"))
        assert seen == ["任意消息"]
        assert fwd._msg_stats["filtered"] == 0
        await db.close()

    async def test_default_none_no_change(self, tmp_path):
        """默认关（ad_judge=None）：路径与升级前一致，消息照常转发。"""
        fwd, db, _CHAT, _Msg = await self._make(tmp_path, None)
        assert fwd.ad_judge is None
        seen = []

        async def stub_send(original, text, target_id, topic_id, snap):
            seen.append(text)

        fwd._send_message = stub_send
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="普通消息"))
        assert seen == ["普通消息"]
        assert fwd._msg_stats["filtered"] == 0
        await db.close()
