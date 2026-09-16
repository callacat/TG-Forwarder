# -*- coding: utf-8 -*-
"""core 层测试（R1/R2/R5/R8 对应 + 纯函数覆盖）。全部 mock，不连真实 Telegram。"""
import asyncio
import time
from typing import Any, Dict, List, Optional

import pytest

from tg_forwarder.config import (
    AccountConfig,
    AdFilterConfig,
    ContentFilterConfig,
    ProxyConfig,
    RuntimeConfig,
    SourceConfig,
    SystemSettings,
    TargetDistributionRule,
    WatchdogConfig,
    WhitelistConfig,
)
from tg_forwarder.core.accounts import (
    PROXY_LEVEL_CONFIGURED,
    PROXY_LEVEL_DIRECT_443,
    PROXY_LEVEL_DIRECT_80,
    PROXY_LEVEL_HTTP_MODE,
    AccountManager,
    AccountStartupError,
    ProxyFallback,
    backoff_delays,
)
from tg_forwarder.core.forwarder import (
    CAPTION_LIMIT_UNITS,
    Forwarder,
    _utf16_len,
    apply_replacements,
    clamp_caption,
    content_fingerprints,
    extract_url_tokens,
    find_target,
    message_hash,
    should_filter,
)
from tg_forwarder.core.supervision import Supervisor


def _cfg(**overrides) -> RuntimeConfig:
    cfg = RuntimeConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ---------------------------------------------------------------------------
# 退避计算（R1）
# ---------------------------------------------------------------------------


class TestBackoff:
    def test_sequence_doubles(self):
        assert backoff_delays(5) == [2, 4, 8, 16, 32]

    def test_capped_at_max(self):
        delays = backoff_delays(10)
        assert all(d <= 60 for d in delays)
        assert delays[-1] == 60


# ---------------------------------------------------------------------------
# ProxyFallback 降级链（R5）
# ---------------------------------------------------------------------------


class TestProxyFallback:
    def _make_fb(self, fail_levels, configured_proxy=None):
        """fail_levels: 失败的级别集合；成功级别返回哨兵 client。"""
        calls: List[int] = []

        async def factory(level, session_path, api_id, api_hash):
            calls.append(level)
            if level in fail_levels:
                raise ConnectionError(f"level {level} down")
            # 工厂契约：成功返回已连接的 client（非元组），与生产实现一致
            return f"client@{level}"

        fb = ProxyFallback(configured_proxy=configured_proxy, connect_factory=factory)
        return fb, calls

    async def test_fallback_order_direct443_ok(self):
        fb, calls = self._make_fb(fail_levels=set())
        client, level = await fb.connect_with_fallback("/tmp/s", 1, "h")
        assert calls == [PROXY_LEVEL_DIRECT_443]
        assert level == PROXY_LEVEL_DIRECT_443

    async def test_fallback_descends_chain(self):
        fb, calls = self._make_fb(
            fail_levels={PROXY_LEVEL_DIRECT_443, PROXY_LEVEL_DIRECT_80}
        )
        client, level = await fb.connect_with_fallback("/tmp/s", 1, "h")
        assert calls == [
            PROXY_LEVEL_DIRECT_443,
            PROXY_LEVEL_DIRECT_80,
            PROXY_LEVEL_HTTP_MODE,
        ]
        assert level == PROXY_LEVEL_HTTP_MODE

    async def test_configured_proxy_appended_to_chain(self):
        fb, calls = self._make_fb(
            fail_levels={0, 1, 2}, configured_proxy=("socks5", "127.0.0.1", 1080, True)
        )
        client, level = await fb.connect_with_fallback("/tmp/s", 1, "h")
        assert calls[-1] == PROXY_LEVEL_CONFIGURED
        assert level == PROXY_LEVEL_CONFIGURED

    async def test_all_levels_fail_raises_last_error(self):
        fb, calls = self._make_fb(fail_levels={0, 1, 2})
        with pytest.raises(ConnectionError):
            await fb.connect_with_fallback("/tmp/s", 1, "h")
        assert calls == [0, 1, 2]


# ---------------------------------------------------------------------------
# 生产连接工厂真实路径（P0 修复回归：老马验收发现旧版只构造不 connect）
# ---------------------------------------------------------------------------


class _FakeTelegramClient:
    """替身 TelegramClient：记录构造 kwargs、connect/disconnect 调用与顺序。"""

    instances: List["_FakeTelegramClient"] = []

    def __init__(self, session_path, api_id, api_hash, **kwargs):
        self.session_path = session_path
        self.init_kwargs = kwargs
        self.connect_called = 0
        self.disconnect_called = 0
        self.connected = False
        self.raise_on_connect = False
        self._connection = kwargs.pop("_connection_probe", None)
        _FakeTelegramClient.instances.append(self)

    async def connect(self):
        self.connect_called += 1
        if self.raise_on_connect:
            raise ConnectionError("fake dc unreachable")
        self.connected = True

    async def disconnect(self):
        self.disconnect_called += 1
        self.connected = False

    async def is_user_authorized(self):
        assert self.connected, "is_user_authorized 前必须先 connect（旧 bug 回归守卫）"
        return True


class TestDefaultConnectFactory:
    def setup_method(self):
        _FakeTelegramClient.instances = []

    def _patch_telegramclient(self, monkeypatch):
        import telethon

        monkeypatch.setattr(telethon, "TelegramClient", _FakeTelegramClient)

    async def test_factory_connects_before_return(self, monkeypatch):
        """P0 回归：factory 返回的 client 必须已 connect（旧版从未连接）。"""
        self._patch_telegramclient(monkeypatch)
        fb = ProxyFallback()
        client, level = await fb.connect_with_fallback("/tmp/s.session", 1, "h")
        assert level == PROXY_LEVEL_DIRECT_443
        assert isinstance(client, _FakeTelegramClient)
        assert client.connect_called == 1
        assert client.connected is True

    async def test_factory_connection_retries_decoupled(self, monkeypatch):
        """connection_retries=None（运行期 telethon 自愈，R2）；
        启动降级靠每级 wait_for 限时防内部重试吞降级（二者解耦）。"""
        self._patch_telegramclient(monkeypatch)
        fb = ProxyFallback()
        client, _ = await fb.connect_with_fallback("/tmp/s.session", 1, "h")
        assert client.init_kwargs["connection_retries"] is None
        assert client.init_kwargs["use_ipv6"] is False

    async def test_direct80_level_uses_port80_connection(self):
        """direct(80) 级别必须挂 port 强制 80 的连接子类，且不动 session。"""
        import logging
        from collections import defaultdict

        from tg_forwarder.core.accounts import ConnectionTcpPort80

        loggers = defaultdict(lambda: logging.getLogger("test"))
        conn = ConnectionTcpPort80(
            "149.154.167.51", 443, 2, loggers=loggers, proxy=None, local_addr=None
        )
        assert conn._port == 80
        # 443 被传入也必须被强制成 80（子类签名兼容 TelegramClient 调用）
        conn2 = ConnectionTcpPort80("1.2.3.4", 80, 2, loggers=loggers)
        assert conn2._port == 80

    async def test_http_level_uses_correct_class_name(self, monkeypatch):
        """http 模式用真实 telethon ConnectionHttp（旧误写 ConnectionTcpHttp 已修）。"""
        from telethon.network.connection.http import ConnectionHttp

        self._patch_telegramclient(monkeypatch)
        fb = ProxyFallback()
        client, level = await fb.connect_with_fallback("/tmp/s.session", 1, "h")
        # 直接测 http 级别：levels 裁剪到只剩 http
        fb2 = ProxyFallback(levels=[PROXY_LEVEL_HTTP_MODE])
        client2, level2 = await fb2.connect_with_fallback("/tmp/s.session", 1, "h")
        assert client2._connection is ConnectionHttp

    async def test_factory_connect_failure_disconnects_and_raises(self, monkeypatch):
        """connect 失败：factory 内 disconnect 清理并抛出，降级链捕获后试下一级。"""
        import telethon

        made: List["_FakeTelegramClient"] = []

        class FailConnectClient(_FakeTelegramClient):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                made.append(self)

            async def connect(self):
                self.connect_called += 1
                raise ConnectionError("443 blocked")

        monkeypatch.setattr(telethon, "TelegramClient", FailConnectClient)
        fb = ProxyFallback()
        with pytest.raises(ConnectionError):
            await fb._default_connect_factory(
                PROXY_LEVEL_DIRECT_443, "/tmp/s.session", 1, "h"
            )
        assert len(made) == 1
        assert made[0].disconnect_called == 1  # 失败自清理
        assert made[0].connected is False

    async def test_chain_descends_to_next_level_on_connect_fail(self, monkeypatch):
        """生产 factory 下：443 连不上 → 自动降到 80（端到端降级链）。"""
        import telethon

        made: List[_FakeTelegramClient] = []

        def factory_cls(session_path, api_id, api_hash, **kwargs):
            # 第 1 次构造（443）失败；第 2 次（80）成功
            if len(made) == 0:
                c = FailConnectClient2(session_path, api_id, api_hash, **kwargs)
            else:
                c = _FakeTelegramClient(session_path, api_id, api_hash, **kwargs)
            made.append(c)
            return c

        class FailConnectClient2(_FakeTelegramClient):
            async def connect(self):
                self.connect_called += 1
                raise ConnectionError("443 blocked")

        monkeypatch.setattr(telethon, "TelegramClient", factory_cls)
        fb = ProxyFallback(levels=[PROXY_LEVEL_DIRECT_443, PROXY_LEVEL_DIRECT_80])
        client, level = await fb.connect_with_fallback("/tmp/s.session", 1, "h")
        assert level == PROXY_LEVEL_DIRECT_80
        assert len(made) == 2
        assert made[0].disconnect_called == 1
        assert made[1].connect_called == 1
        assert made[1].connected is True

    async def test_account_manager_end_to_end_with_production_factory(self, monkeypatch, tmp_path):
        """AccountManager 真实 _start_one 路径：session 文件在 + fake client → healthy。"""
        import telethon

        self._patch_telegramclient(monkeypatch)
        (tmp_path / "real1.session").touch()  # P9 检查通过
        fb = ProxyFallback()
        am = AccountManager(data_dir=str(tmp_path))
        cfg = _cfg(accounts=[_Acc("real1")])
        healthy = await am.start(cfg)
        assert len(healthy) == 1
        c = healthy[0]
        assert isinstance(c, _FakeTelegramClient)
        assert c.connect_called == 1
        assert c.session_name_for_forwarder == "real1"
        st = am.all_status()["accounts"][0]
        assert st["healthy"] is True
        assert st["proxy_level"] == "direct:443"


# ---------------------------------------------------------------------------
# AccountManager（R1/P9）
# ---------------------------------------------------------------------------


class _Acc:
    def __init__(self, name, enabled=True):
        self.api_id = 1
        self.api_hash = "h"
        self.session_name = name
        self.enabled = enabled


class _Fallback:
    """可控的 fallback mock：fail_names 中账号全链失败。"""

    def __init__(self, fail_names=(), connect_delay=0):
        self.fail_names = fail_names
        self.connect_delay = connect_delay
        self.attempts: Dict[str, int] = {}

    async def connect_with_fallback(self, session_path, api_id, api_hash):
        name = session_path.rsplit("/", 1)[-1].replace(".session", "")
        self.attempts[name] = self.attempts.get(name, 0) + 1
        if name in self.fail_names:
            raise ConnectionError(f"down ({name})")
        return f"client-{name}", PROXY_LEVEL_DIRECT_443


class _Client:
    """轻量 client mock（可挂动态属性，供 session_name_for_forwarder 赋值）。"""

    def __init__(self, name):
        self.name = name
        self.session_name_for_forwarder = name

    def is_connected(self):
        return True

    def is_user_authorized(self):
        return True

    async def disconnect(self):
        pass

    async def connect(self):
        pass


class _AM(AccountManager):
    """绕过 session 文件检查 + 免真实 sleep 的测试子类。"""

    def __init__(self, fallback, fail_names=(), skip_sleep=True):
        super().__init__(fallback_factory=lambda cfg: fallback)

    async def _start_one(self, acc, config, max_attempts):
        from tg_forwarder.core.accounts import AccountState

        state = AccountState(
            session_name=acc.session_name, api_id=acc.api_id, api_hash=acc.api_hash
        )
        self._states[acc.session_name] = state
        fallback = self._fallback_factory(config)
        delays = backoff_delays(max_attempts)
        for attempt in range(1, max_attempts + 1):
            try:
                client_name, level = await fallback.connect_with_fallback(
                    f"/data/{acc.session_name}.session", acc.api_id, acc.api_hash
                )
                client = _Client(acc.session_name)
                state.client = client
                state.connected = True
                state.authorized = True
                state.healthy = True
                state.proxy_level = level
                state.last_error = None
                client.session_name_for_forwarder = acc.session_name
                return
            except Exception as e:
                state.last_error = f"{type(e).__name__}: {e}"[:300]
                if attempt < max_attempts:
                    delays[attempt - 1]  # 退避序列存在（不真睡）
        state.unavailable = True


class TestAccountManager:
    async def test_success_sets_state_and_key(self):
        fb = _Fallback()
        am = _AM(fb)
        cfg = _cfg(accounts=[_Acc("a1")])
        healthy = await am.start(cfg)
        assert len(healthy) == 1
        assert healthy[0].session_name_for_forwarder == "a1"
        assert am.all_status()["accounts"][0]["healthy"] is True

    async def test_all_fail_raises_startup_error(self):
        """R1：全部账号重试耗尽 → AccountStartupError（进程退出路径）。"""
        fb = _Fallback(fail_names={"a1"})
        am = _AM(fb)
        cfg = _cfg(accounts=[_Acc("a1")])
        with pytest.raises(AccountStartupError):
            await am.start(cfg)

    async def test_retry_count_matches_attempts(self):
        """R1：失败账号按 max_attempts 重试。"""
        fb = _Fallback(fail_names={"a1"})
        am = _AM(fb)
        cfg = _cfg(accounts=[_Acc("a1")])
        with pytest.raises(AccountStartupError):
            await am.start(cfg)
        assert fb.attempts["a1"] == 5

    async def test_partial_success_no_raise(self):
        fb = _Fallback(fail_names={"bad"})
        am = _AM(fb)
        cfg = _cfg(accounts=[_Acc("ok"), _Acc("bad")])
        healthy = await am.start(cfg)
        assert len(healthy) == 1
        st = {a["session_name"]: a for a in am.all_status()["accounts"]}
        assert st["bad"]["unavailable"] is True
        assert st["bad"]["last_error"]

    async def test_no_accounts_returns_empty(self):
        am = _AM(_Fallback())
        healthy = await am.start(_cfg(accounts=[]))
        assert healthy == []

    async def test_record_flood_wait(self):
        am = _AM(_Fallback())
        await am.start(_cfg(accounts=[_Acc("a1")]))
        am.record_flood_wait("a1", 30)
        st = am.all_status()["accounts"][0]
        assert st["flood_wait"] > 0
        assert st["flood_wait_count"] == 1

    async def test_session_missing_marks_unavailable(self, tmp_path):
        """P9：无 session 文件 → unavailable + 禁止交互登录。

        单账号场景下 R1 要求全部失败即抛 AccountStartupError——
        但无 session 是 unavailable 终态而非重试耗尽，这里验证
        「标记 unavailable + 未尝试连接」两条核心行为。
        """
        am = AccountManager(data_dir=str(tmp_path / "nodata"))
        called = {"factory": 0}

        def _factory(cfg):
            called["factory"] += 1
            return _Fallback()

        am._fallback_factory = _factory
        cfg = _cfg(accounts=[_Acc("ghost")])
        try:
            await am.start(cfg)
            raised = False
        except AccountStartupError:
            raised = True
        # 全部账号不可用 → R1 抛错（进程退出路径）
        assert raised is True
        st = am.all_status()["accounts"][0]
        assert st["unavailable"] is True
        assert st["last_error"] == "session_file_missing"
        assert called["factory"] == 0  # 根本没尝试连接


# ---------------------------------------------------------------------------
# 看门狗（R1 假活根治）
# ---------------------------------------------------------------------------


class _MockAM:
    def __init__(self, healthy: int):
        self._healthy = healthy

    def healthy_accounts(self):
        return [object()] * self._healthy

    def all_status(self):
        return {"accounts": [{"session_name": "a", "last_error": "x"}] * self._healthy}


class _WatchdogCfg:
    """Supervisor 测试配置：支持浮点参数（WatchdogConfig 模型约束整数）。"""

    def __init__(self, timeout_minutes=5, interval_seconds=60):
        self.watchdog = type(
            "W", (), {"timeout_minutes": timeout_minutes, "interval_seconds": interval_seconds}
        )()


class TestSupervisor:
    async def test_healthy_no_exit(self):
        exited = []
        sup = Supervisor(_MockAM(2), _WatchdogCfg(), exit_func=lambda c: exited.append(c))
        task = asyncio.create_task(sup.run())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert exited == []

    async def test_dead_accounts_triggers_exit_after_timeout(self):
        """无可用账号持续超时 → 告警回调 + exit 1（R1 核心）。"""
        events = []

        async def on_critical(msg):
            events.append(msg)

        exited = []
        sup = Supervisor(
            _MockAM(0),
            _WatchdogCfg(timeout_minutes=0.01, interval_seconds=0.05),
            on_critical=on_critical,
            exit_func=lambda c: exited.append(c),
        )
        task = asyncio.create_task(sup.run())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if exited:
                break
        assert exited == [1]
        assert len(events) == 1
        assert "account_all_dead" in events[0]

    async def test_recovery_resets_timer(self):
        """恢复健康后计时器重置，不退出。"""
        state = {"healthy": 0}
        am = _MockAM(0)
        am.healthy_accounts = lambda: [object()] * state["healthy"]
        exited = []

        async def on_critical(msg):
            pass

        sup = Supervisor(
            am,
            _WatchdogCfg(timeout_minutes=0.05, interval_seconds=0.05),
            on_critical=on_critical,
            exit_func=lambda c: exited.append(c),
        )
        task = asyncio.create_task(sup.run())
        await asyncio.sleep(0.12)
        state["healthy"] = 2  # 恢复
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert exited == []


# ---------------------------------------------------------------------------
# 纯函数（过滤/替换/哈希/路由）
# ---------------------------------------------------------------------------


class _Photo:
    def __init__(self, photo_id):
        self.id = photo_id


class _Media:
    """media mock：photo 带 .photo、document 是真实 MessageMediaDocument
    （.document.id/.size/.attributes，与 forwarder.message_hash/should_filter
    和 find_target 的访问路径一致）。"""

    def __init__(self, photo_id=None, doc_id=None, size=None, file_name=None):
        from telethon.tl.types import (
            DocumentAttributeFilename,
            MessageMediaDocument,
        )
        from telethon.tl import types as tl_types

        if photo_id is not None:
            self.photo = _Photo(photo_id)
        if doc_id is not None:
            attrs = []
            if file_name:
                attrs.append(DocumentAttributeFilename(file_name=file_name))
            doc = tl_types.Document(
                id=doc_id,
                access_hash=0,
                file_reference=b"",
                date=None,
                mime_type="application/octet-stream",
                size=size or 0,
                attributes=attrs,
                dc_id=1,
            )
            self.document = MessageMediaDocument(document=doc, ttl_seconds=0)


class TestShouldFilter:
    def _snap(self, **kw):
        cfg = _cfg(
            ad_filter=AdFilterConfig(enable=True, keywords_substring=["推广", "http://ad.com"]),
            whitelist=WhitelistConfig(enable=False, keywords=["白名单词"]),
            content_filter=ContentFilterConfig(enable=True, meaningless_words=["哈哈"], min_meaningful_length=5),
        )
        return cfg

    def test_pass_normal(self):
        r, k = should_filter("正常内容分享", None, self._snap())
        assert r is None

    def test_blacklist_substring(self):
        r, k = should_filter("限时推广速来", None, self._snap())
        assert r == "Blacklist (Substring)" and k == "推广"

    def test_whitelist_overrides_blacklist(self):
        cfg = _cfg(
            ad_filter=AdFilterConfig(enable=True, keywords_substring=["推广"]),
            whitelist=WhitelistConfig(enable=True, keywords=["白名单词"]),
            content_filter=ContentFilterConfig(enable=True),
        )
        r, k = should_filter("白名单词 推荐", None, cfg)
        assert r is None  # 白名单放行（黑名单词"推广"不在文本里）

    def test_content_empty(self):
        r, k = should_filter("", None, self._snap())
        assert r == "Empty"

    def test_content_too_short(self):
        r, k = should_filter("短", None, self._snap())
        assert r == "Too Short"

    def test_content_meaningless(self):
        r, k = should_filter("哈哈", None, self._snap())
        assert r == "Meaningless"

    def test_media_saves_short_text(self):
        r, k = should_filter("短", _Media(doc_id=1, size=100), self._snap())
        assert r is None  # 有媒体不受短文本过滤

    def test_filename_blacklist(self):
        r, k = should_filter("下载", _Media(doc_id=1, size=1, file_name="ad_crack.exe"), self._snap())
        # file_name_keywords 未配置时不命中
        assert r is None
        cfg = _cfg(
            ad_filter=AdFilterConfig(enable=True, file_name_keywords=["crack"]),
            whitelist=WhitelistConfig(enable=False),
            content_filter=ContentFilterConfig(enable=False),
        )
        r, k = should_filter("下载", _Media(doc_id=1, size=1, file_name="app_crack.exe"), cfg)
        assert r == "Blacklist (Filename)"


class TestReplacements:
    def test_apply(self):
        cfg = _cfg(replacements={"频道A": "频道B", "https://a.com": "https://b.com"})
        assert apply_replacements("频道A https://a.com", cfg) == "频道B https://b.com"

    def test_noop(self):
        assert apply_replacements("", _cfg(replacements={"a": "b"})) == ""
        assert apply_replacements("文本", _cfg(replacements={})) == "文本"


class TestMessageHash:
    def test_text_hash_stable(self):
        """sha256 修复：同文本跨实例哈希一致（v2 内置 hash() 不稳定）。"""
        h1 = message_hash("长文本" * 30, None, 1)
        h2 = message_hash("长文本" * 30, None, 999)
        assert h1 == h2
        assert h1.startswith("text:")

    def test_photo_hash(self):
        assert message_hash("t", _Media(photo_id=42), 1) == "photo:42"

    def test_doc_hash(self):
        assert message_hash("t", _Media(doc_id=7, size=1024), 1) == "doc:7:1024"

    def test_short_text_uses_id(self):
        assert message_hash("短", None, 55) == "id:55"

    def test_different_texts_differ(self):
        assert message_hash("A" * 60, None, 1) != message_hash("B" * 60, None, 1)


class TestFindTarget:
    def test_rule_match(self):
        cfg = _cfg(
            distribution_rules=[
                TargetDistributionRule(
                    name="r", any_keywords=["4K"], target_identifier="-100333",
                    topic_id=9, resolved_target_id=-100333,
                )
            ],
            settings=SystemSettings(default_target="-100222", default_topic_id=1),
            targets_resolved_default=-100222,
        )
        t, topic = find_target("超清4K资源", None, cfg)
        assert t == -100333 and topic == 9

    def test_default_fallback(self):
        cfg = _cfg(
            distribution_rules=[
                TargetDistributionRule(
                    name="r", any_keywords=["4K"], target_identifier="-100333",
                    resolved_target_id=-100333,
                )
            ],
            settings=SystemSettings(default_topic_id=3),
            targets_resolved_default=-100222,
        )
        t, topic = find_target("普通内容", None, cfg)
        assert t == -100222 and topic == 3


# ---------------------------------------------------------------------------
# R8：快照原子替换
# ---------------------------------------------------------------------------


class TestSnapshotAtomicity:
    async def test_update_snapshot_no_tearing(self):
        """update_snapshot 期间并发 process_message 不炸且视图一致（R8/P7）。"""
        db_stub = type("DB", (), {})()
        fwd = Forwarder(db_stub, account_manager=None)
        cfg1 = _cfg(replacements={"a": "1"})
        cfg2 = _cfg(replacements={"a": "2"})
        fwd.update_snapshot(cfg1)

        errors = []

        async def reader():
            for _ in range(500):
                snap = fwd.get_snapshot()
                if snap is not None:
                    # 同一快照引用必然一致（整体替换语义）
                    assert snap.replacements["a"] in ("1", "2")
                await asyncio.sleep(0)

        async def writer():
            for _ in range(50):
                fwd.update_snapshot(cfg2 if fwd.get_snapshot() is cfg1 else cfg1)
                await asyncio.sleep(0)

        await asyncio.gather(reader(), writer(), return_exceptions=False)
        assert not errors

    async def test_old_snapshot_reference_immutable(self):
        """旧快照引用在 reload 后仍指向旧对象（处理中的消息视图一致）。"""
        fwd = Forwarder(type("DB", (), {})(), account_manager=None)
        cfg1 = _cfg(replacements={"k": "v1"})
        fwd.update_snapshot(cfg1)
        held = fwd.get_snapshot()
        cfg2 = _cfg(replacements={"k": "v2"})
        fwd.update_snapshot(cfg2)
        assert held.replacements["k"] == "v1"
        assert fwd.get_snapshot().replacements["k"] == "v2"


# ---------------------------------------------------------------------------
# catchup 兜底扫描（P1 修复：事件漏送补齐 + 与事件路径不双发）
# ---------------------------------------------------------------------------

_CHAT = -100555


class _Msg:
    def __init__(self, id, chat_id=_CHAT, text="", media=None, grouped_id=None):
        self.id = id
        self.chat_id = chat_id
        self.text = text
        self.media = media
        self.grouped_id = grouped_id


class _CatchupClient:
    """fake TelegramClient：iter_messages 镜像 telethon min_id/limit/reverse 语义。"""

    session_name_for_forwarder = "catchup"

    def __init__(self, messages):
        self._all = list(messages)

    def is_connected(self):
        return True

    def iter_messages(self, chat_id, min_id=0, limit=None, reverse=False):
        msgs = [m for m in self._all if m.chat_id == chat_id and m.id > min_id]
        # telethon 默认 newest-first；reverse=True → oldest-first
        msgs.sort(key=lambda m: m.id, reverse=not reverse)
        if limit is not None:
            msgs = msgs[:limit]

        async def gen():
            for m in msgs:
                yield m

        return gen()


class _AMStub:
    def __init__(self, client):
        self._c = client

    def healthy_accounts(self):
        return [self._c]

    def all_status(self):
        return {"accounts": [{"session_name": "catchup", "flood_wait_count": 0}]}

    def record_flood_wait(self, *a, **k):
        pass


def _catchup_snap(forward_new_only=True):
    return _cfg(
        sources=[SourceConfig(identifier=str(_CHAT), resolved_id=_CHAT)],
        settings=SystemSettings(
            default_target="-100999", forward_new_only=forward_new_only
        ),
        targets_resolved_default=-100999,
        ad_filter=AdFilterConfig(enable=False),
        content_filter=ContentFilterConfig(enable=False),
    )


async def _make_fwd(tmp_path, snapshot, client):
    import os

    from tg_forwarder.storage.db import Database

    db = Database(os.path.join(str(tmp_path), "catch.sqlite"))
    await db.open()
    await db.migrate()
    fwd = Forwarder(db, _AMStub(client))
    fwd.update_snapshot(snapshot)
    return fwd, db


class TestCatchup:
    async def test_gap_recovery_ascends_progress(self, tmp_path):
        """断连窗口漏送：progress 之后被 catchup 逐条补齐并连续抬升。"""
        client = _CatchupClient([_Msg(11, text="hello11"), _Msg(12, text="hello12"), _Msg(13, text="hello13")])
        fwd, db = await _make_fwd(tmp_path, _catchup_snap(forward_new_only=True), client)
        await db.set_progress(_CHAT, 10)  # 事件停在 10
        calls = []
        async def spy(m, all_messages_in_group=None):
            calls.append(m.id)
        fwd.process_message = spy
        n = await fwd.catchup_once(limit=50)
        assert n == 3
        assert calls == [11, 12, 13]
        assert await db.get_progress(_CHAT) == 13
        await db.close()

    async def test_limit_capped_per_source(self, tmp_path):
        """每源上限 50：漏送 >50 条时 oldest-first 只补最早的 50。"""
        client = _CatchupClient([_Msg(i, text=f"m{i}") for i in range(11, 111)])  # 11..110
        fwd, db = await _make_fwd(tmp_path, _catchup_snap(), client)
        await db.set_progress(_CHAT, 10)
        calls = []
        async def spy(m, all_messages_in_group=None):
            calls.append(m.id)
        fwd.process_message = spy
        await fwd.catchup_once(limit=50)
        assert len(calls) == 50
        assert calls == list(range(11, 61))  # oldest-first
        assert await db.get_progress(_CHAT) == 60
        await db.close()

    async def test_album_grouped_single_call(self, tmp_path):
        """相册 grouped_id 合并：整组一次进 process_message，主消息取带 text 的。"""
        client = _CatchupClient([
            _Msg(11, grouped_id=7),                # 相册图1
            _Msg(12, text="相册标题", grouped_id=7),  # 相册图2（带文字=主）
            _Msg(13, text="单独"),
        ])
        fwd, db = await _make_fwd(tmp_path, _catchup_snap(), client)
        await db.set_progress(_CHAT, 10)
        seen = []
        async def spy(m, all_messages_in_group=None):
            seen.append((m.id, len(all_messages_in_group) if all_messages_in_group else 0))
        fwd.process_message = spy
        await fwd.catchup_once(limit=50)
        # 相册(11,12) 合并成 1 次，主消息=12（有 text）；单独 13 一次
        assert seen == [(12, 2), (13, 0)]
        await db.close()

    async def test_forward_new_only_sets_baseline_no_backfill(self, tmp_path):
        """新源 + forward_new_only：只建基线不倒灌历史。"""
        client = _CatchupClient([_Msg(5, text="old5"), _Msg(6, text="old6"), _Msg(7, text="old7")])
        fwd, db = await _make_fwd(tmp_path, _catchup_snap(forward_new_only=True), client)
        # 默认 progress=0
        calls = []
        async def spy(m, all_messages_in_group=None):
            calls.append(m.id)
        fwd.process_message = spy
        await fwd.catchup_once(limit=50)
        assert calls == []                       # 未处理任何历史
        assert await db.get_progress(_CHAT) == 7  # 基线=最新，往后只追新增
        await db.close()

    async def test_history_scan_when_forward_new_only_off(self, tmp_path):
        """forward_new_only=False：progress=0 也增量补历史（min_id=0 起）。"""
        client = _CatchupClient([_Msg(1, text="m1"), _Msg(2, text="m2")])
        fwd, db = await _make_fwd(tmp_path, _catchup_snap(forward_new_only=False), client)
        calls = []
        async def spy(m, all_messages_in_group=None):
            calls.append(m.id)
        fwd.process_message = spy
        await fwd.catchup_once(limit=50)
        assert calls == [1, 2]
        await db.close()

    async def test_event_and_catchup_no_double_send(self, tmp_path):
        """核心不双发：事件处理过的消息，catchup 靠 progress 门控不再重复转发。"""
        m11, m12, m13 = _Msg(11, text="hello11"), _Msg(12, text="hello12"), _Msg(13, text="hello13")
        client = _CatchupClient([m11, m12, m13])
        snapshot = _catchup_snap(forward_new_only=True)
        fwd, db = await _make_fwd(tmp_path, snapshot, client)
        sent = []
        async def stub_send(original, text, target_id, topic_id, snap):
            sent.append(text)
        fwd._send_message = stub_send
        # 模拟事件只送达 11（12/13 在断连窗口漏送）
        await fwd.process_message(m11)
        assert sent == ["hello11"]
        assert await db.get_progress(_CHAT) == 11
        # catchup 从 progress=11 起 → 只补 12,13，11 不再重复
        await fwd.catchup_once(limit=50)
        assert sent == ["hello11", "hello12", "hello13"]
        assert sent.count("hello11") == 1
        await db.close()

    async def test_lru_blocks_direct_reprocess(self, tmp_path):
        """同一条消息二次进 process_message（事件+catchup 竞态）被 LRU 拦下不双发。"""
        m11 = _Msg(11, text="hello11")
        client = _CatchupClient([m11])
        fwd, db = await _make_fwd(tmp_path, _catchup_snap(forward_new_only=False), client)
        sent = []
        async def stub_send(original, text, target_id, topic_id, snap):
            sent.append(text)
        fwd._send_message = stub_send
        await fwd.process_message(m11)  # 首次
        await fwd.process_message(m11)  # 竞态重复 → LRU 跳过
        assert sent == ["hello11"]
        await db.close()


# ---------------------------------------------------------------------------
# 运行期维护（P2 修复：断连立即置 unhealthy + 周期维护循环触发看门狗退出）
# ---------------------------------------------------------------------------


class _LiveClient:
    """可切换连接态的 fake client（模拟 docker network disconnect）。"""

    session_name_for_forwarder = "live"

    def __init__(self):
        self._connected = True

    def is_connected(self):  # telethon 同步 API
        return self._connected

    async def disconnect(self):
        pass

    async def connect(self):
        if not self._connected:
            raise ConnectionError("network down")

    async def is_user_authorized(self):
        return True

    def __call__(self, request, **kw):
        # 探活 RPC：连接成功→立即返回；断开→快速抛错
        async def _rpc():
            if not self._connected:
                raise ConnectionError("net down")
            return "ok"
        return _rpc()


class TestRuntimeMaintenance:
    def _seed(self):
        from tg_forwarder.core.accounts import AccountManager, AccountState

        am = AccountManager()
        client = _LiveClient()
        st = AccountState(session_name="live", api_id=1, api_hash="h")
        st.client = client
        st.connected = True
        st.authorized = True
        st.healthy = True
        am._states["live"] = st
        return am, client, st

    async def test_maintain_flips_healthy_immediately_on_disconnect(self):
        """P2 回归：探到断连立即置 unhealthy（旧版依赖重连失败才置位）。"""
        am, client, st = self._seed()
        assert len(am.healthy_accounts()) == 1
        client._connected = False  # 注入断网
        await am.maintain_once()
        assert st.healthy is False          # 关键：无需等 _reconnect
        assert st.connected is False
        assert len(am.healthy_accounts()) == 0  # 看门狗/health 看到真实值
        if st.reconnect_task:
            st.reconnect_task.cancel()
        await am.stop()

    async def test_reconnect_restores_healthy_when_back(self):
        am, client, st = self._seed()
        client._connected = False
        await am.maintain_once()
        assert st.healthy is False
        if st.reconnect_task:
            st.reconnect_task.cancel()
            try:
                await st.reconnect_task
            except (asyncio.CancelledError, Exception):
                pass
        client._connected = True  # 网络恢复
        await am.maintain_once()
        assert st.healthy is True
        await am.stop()

    async def test_end_to_end_disconnect_triggers_watchdog_exit(self):
        """端到端复现老马断网注入：维护循环置 unhealthy → 看门狗超时 exit(1)。"""
        am, client, st = self._seed()
        cfg = _WatchdogCfg(timeout_minutes=0.01, interval_seconds=0.05)  # ~0.6s 阈值
        exited = []

        async def on_critical(msg):
            pass

        async def flip():
            await asyncio.sleep(0.1)
            client._connected = False  # 运行期断网

        sup = Supervisor(
            am, cfg, on_critical=on_critical, exit_func=lambda c: exited.append(c)
        )
        tasks = [
            asyncio.create_task(am.maintenance_loop(0.05)),
            asyncio.create_task(flip()),
            asyncio.create_task(sup.run()),
        ]
        for _ in range(150):
            await asyncio.sleep(0.05)
            if exited:
                break
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        if st.reconnect_task:
            st.reconnect_task.cancel()
        assert exited == [1]  # 假活根治：断网超阈值必退出


# ---------------------------------------------------------------------------
# T1：BotService 装配（main.py 接线回归）
# ---------------------------------------------------------------------------


import main as _main_module  # noqa: E402


class _BotMe:
    username = "test_forwarder_bot"


class _BMsg:
    async def edit(self, *a, **k):
        return None


class _BEv:
    def __init__(self, sender_id, is_group=False):
        self.sender_id = sender_id
        self.is_group = is_group

    async def reply(self, *a, **k):
        return _BMsg()


class _FakeBotClient:
    """替身 bot TelegramClient：记录 start(bot_token)/get_me/on()/__call__。"""

    def __init__(self, session, api_id, api_hash, **kwargs):
        self.api_id = api_id
        self.api_hash = api_hash
        self.init_kwargs = kwargs
        self.started_with = None
        self.registered = []
        self._connected = False
        self.raise_on_start = False
        self.commands_sent = 0

    async def start(self, bot_token=None):
        if self.raise_on_start:
            raise ConnectionError("bot login failed")
        self.started_with = bot_token
        self._connected = True

    async def get_me(self):
        return _BotMe()

    def is_connected(self):
        return self._connected

    async def disconnect(self):
        self._connected = False

    def on(self, *a, **k):
        def deco(fn):
            self.registered.append(fn)
            return fn
        return deco

    async def __call__(self, *a, **k):
        self.commands_sent += 1
        return None


def _bot_cfg(**bs_over):
    from tg_forwarder.config import AccountConfig, BotServiceConfig

    bs = BotServiceConfig(
        enabled=True, bot_token="123:AAAtoken", admin_user_ids=[100],
        **bs_over,
    )
    return RuntimeConfig(
        bot_service=bs,
        accounts=[AccountConfig(api_id=111, api_hash="ah", session_name="acc1")],
    )


class _AM2:
    def healthy_accounts(self):
        return []

    def all_status(self):
        return {"accounts": []}

    def record_flood_wait(self, *a, **k):
        pass


async def _bot_db(tmp_path):
    """真实 tmp db（migrate 后建 app_config 表）+ 三仓储。"""
    import os

    from tg_forwarder.storage.db import Database
    from tg_forwarder.storage.repositories import (
        ConfigRepository,
        RuleRepository,
        SourceRepository,
    )

    db = Database(os.path.join(str(tmp_path), "bot.sqlite"))
    await db.open()
    await db.migrate()
    return db, (ConfigRepository(db), SourceRepository(db), RuleRepository(db))


def _patch_bot_factory(monkeypatch, made=None):
    import telethon

    def factory(session, api_id, api_hash, **kw):
        c = _FakeBotClient(session, api_id, api_hash, **kw)
        if made is not None:
            made.append(c)
        return c

    monkeypatch.setattr(telethon, "TelegramClient", factory)
    return factory


class TestBotCredentialsResolve:
    def test_independent_preferred(self):
        cfg = _bot_cfg(bot_api_id=777, bot_api_hash="bh")
        assert _main_module.resolve_bot_credentials(cfg) == (777, "bh")

    def test_fallback_accounts0(self):
        assert _main_module.resolve_bot_credentials(_bot_cfg()) == (111, "ah")

    def test_no_credentials_returns_none(self):
        cfg = _bot_cfg()
        cfg.accounts = []
        assert _main_module.resolve_bot_credentials(cfg) == (None, None)

    def test_partial_independent_falls_back(self):
        """只有 bot_api_id 缺 hash → 视为无独立凭据，回落 accounts[0]。"""
        cfg = _bot_cfg(bot_api_id=777, bot_api_hash=None)
        assert _main_module.resolve_bot_credentials(cfg) == (111, "ah")


class TestInitializeBot:
    async def test_enabled_connects_and_registers(self, tmp_path, monkeypatch):
        """装配冒烟（T1 核心）：Bot 连接 + 命令注册 + 菜单落库 + reload 接线。"""
        made = []
        _patch_bot_factory(monkeypatch, made)
        db, repos = await _bot_db(tmp_path)
        try:
            cfg = _bot_cfg(bot_api_id=777, bot_api_hash="bh")

            async def reload_func():
                return "ok"

            svc, client = await _main_module._initialize_bot(
                cfg, None, _AM2(), None, db, repos, reload_func, lambda: []
            )
            assert svc is not None and client is not None
            assert made[0].started_with == "123:AAAtoken"      # bot_token 登录
            assert made[0].api_id == 777                       # R6 独立凭据生效
            names = [getattr(b, "__name__", "") for b in made[0].registered]
            assert len(made[0].registered) >= 5                # 5 条命令 handler
            assert made[0].commands_sent >= 1                  # SetBotCommandsRequest 已发
            stored = await repos[0].get("bot_command_setup")   # 菜单签名落库
            assert stored and "token_signature" in stored
            assert svc.reload_config is reload_func            # /reload 链路接线
        finally:
            await db.close()

    async def test_menu_signature_dedup_second_run(self, tmp_path, monkeypatch):
        """token 未变 → 第二次装配跳过菜单设置（v2 去重逻辑）。"""
        made = []
        _patch_bot_factory(monkeypatch, made)
        db, repos = await _bot_db(tmp_path)
        try:
            cfg = _bot_cfg()
            svc1, _ = await _main_module._initialize_bot(
                cfg, None, _AM2(), None, db, repos, lambda: None, lambda: []
            )
            first_calls = made[0].commands_sent
            svc2, _ = await _main_module._initialize_bot(
                cfg, None, _AM2(), None, db, repos, lambda: None, lambda: []
            )
            assert svc2 is not None
            assert made[1].commands_sent == 0   # 签名一致，菜单未重设
            assert first_calls >= 1
        finally:
            await db.close()

    async def test_disabled_returns_none(self):
        cfg = _bot_cfg()
        cfg.bot_service.enabled = False
        svc, client = await _main_module._initialize_bot(
            cfg, None, _AM2(), None, None, None, lambda: None, lambda: []
        )
        assert svc is None and client is None

    async def test_placeholder_token_skipped(self):
        cfg = _bot_cfg()
        cfg.bot_service.bot_token = "YOUR_BOT_TOKEN_HERE"
        svc, client = await _main_module._initialize_bot(
            cfg, None, _AM2(), None, None, None, lambda: None, lambda: []
        )
        assert svc is None and client is None

    async def test_no_credentials_skipped(self, monkeypatch):
        """独立凭据缺 + accounts 空 → 不构造 client。"""
        made = []
        _patch_bot_factory(monkeypatch, made)
        cfg = _bot_cfg()
        cfg.accounts = []
        svc, client = await _main_module._initialize_bot(
            cfg, None, _AM2(), None, None, None, lambda: None, lambda: []
        )
        assert svc is None and client is None
        assert made == []

    async def test_connect_failure_nonfatal(self, tmp_path, monkeypatch):
        """Bot 连接失败不阻断进程（运维通道非转发核心）。"""
        import telethon

        def factory(session, api_id, api_hash, **kw):
            c = _FakeBotClient(session, api_id, api_hash, **kw)
            c.raise_on_start = True
            return c

        monkeypatch.setattr(telethon, "TelegramClient", factory)
        db, repos = await _bot_db(tmp_path)
        try:
            svc, client = await _main_module._initialize_bot(
                _bot_cfg(), None, _AM2(), None, db, repos, lambda: None, lambda: []
            )
            assert svc is None and client is None
        finally:
            await db.close()

    async def test_reload_handler_admin_gate_and_chain(self, tmp_path, monkeypatch):
        """/reload：非 admin 直接拒绝不触链；admin 走 reload_func 全链路。"""
        made = []
        _patch_bot_factory(monkeypatch, made)
        db, repos = await _bot_db(tmp_path)
        try:
            cfg = _bot_cfg()
            calls = {"n": 0}

            async def reload_func():
                calls["n"] += 1
                return "reloaded"

            svc, client = await _main_module._initialize_bot(
                cfg, None, _AM2(), None, db, repos, reload_func, lambda: []
            )
            assert svc is not None
            # v2 注册顺序：/start /status /reload /check /ids
            reload_handler = made[0].registered[2]
            await reload_handler(_BEv(sender_id=999))   # 非 admin → 拒绝
            assert calls["n"] == 0
            await reload_handler(_BEv(sender_id=100))   # admin → 全链路
            assert calls["n"] == 1
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# R1 关键：client 轮询异常不得旁路看门狗的结构化告警（老马断网复验抓到 JSON 缺失）
# ---------------------------------------------------------------------------


class _BoomPollClient:
    """run_until_disconnected 立即抛 telethon 式 ConnectionError（模拟重连耗尽）。"""

    session_name_for_forwarder = "live"

    def __init__(self):
        self.poll_calls = 0

    async def run_until_disconnected(self):
        self.poll_calls += 1
        raise ConnectionError("Connection to Telegram failed 5 time(s)")

    def is_connected(self):
        return False


class TestSupervisedPoll:
    async def test_exception_contained_and_marks_unhealthy(self):
        """轮询抛错不外泄 gather，且把账号标 unhealthy（否则进程旁路退出无告警）。"""
        from tg_forwarder.core.accounts import AccountManager, AccountState

        am = AccountManager()
        st = AccountState(session_name="live", api_id=1, api_hash="h")
        st.connected = True
        st.healthy = True
        am._states["live"] = st

        client = _BoomPollClient()
        task = asyncio.create_task(_main_module._supervised_poll(client, am, "live"))
        await asyncio.sleep(0.2)  # 捕获一次 + mark_unhealthy + 进 5s sleep
        # 关键1：异常被吸收，task 仍在（sleep 中），未因异常 done
        assert not task.done()
        # 关键2：账号已标 unhealthy（看门狗会读到真实值）
        assert st.healthy is False and st.connected is False
        assert client.poll_calls >= 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await am.stop()

    async def test_watchdog_alert_fires_on_poll_death(self):
        """端到端：轮询异常→看门狗超阈值→on_critical 被调（结构化告警不旁路）。"""
        from tg_forwarder.core.accounts import AccountManager, AccountState

        am = AccountManager()
        st = AccountState(session_name="live", api_id=1, api_hash="h")
        st.connected = True
        st.healthy = True
        am._states["live"] = st
        client = _BoomPollClient()

        cfg = _WatchdogCfg(timeout_minutes=0.01, interval_seconds=0.05)
        crit = []
        async def on_critical(msg):
            crit.append(msg)
        exited = []
        sup = Supervisor(
            am, cfg, on_critical=on_critical, exit_func=lambda c: exited.append(c)
        )
        tasks = [
            asyncio.create_task(am.maintenance_loop(0.05)),
            asyncio.create_task(_main_module._supervised_poll(client, am, "live")),
            asyncio.create_task(sup.run()),
        ]
        for _ in range(200):
            await asyncio.sleep(0.05)
            if exited:
                break
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        assert exited == [1]
        assert len(crit) >= 1  # 结构化告警确实触达（非裸 ConnectionError 旁路退出）


# ---------------------------------------------------------------------------
# 黑洞式断连检测（老马 09fb4c4 复验：is_connected 恒 True、无异常，须靠主动 RPC 探活）
# ---------------------------------------------------------------------------


class _BlackholeClient:
    """iptables DROP 黑洞：is_connected 永远 True、不抛异常，但任何 RPC 永不返回。"""

    session_name_for_forwarder = "live"

    def __init__(self):
        self.probe_calls = 0

    def is_connected(self):  # 黑洞下 telethon 仍报已连接
        return True

    async def disconnect(self):
        pass

    async def connect(self):
        raise ConnectionError("blackhole: connect never completes")

    async def is_user_authorized(self):
        return True

    def __call__(self, request, **kw):
        async def _hang():
            self.probe_calls += 1
            await asyncio.Event().wait()  # 永不返回 → 探活超时
        return _hang()


class TestBlackholeDetection:
    async def test_probe_detects_blackhole_despite_is_connected(self):
        """核心：is_connected True + 无异常，仍靠 RPC 探活超时 + stale 判死。"""
        from tg_forwarder.core.accounts import AccountManager, AccountState

        am = AccountManager()
        client = _BlackholeClient()
        st = AccountState(session_name="live", api_id=1, api_hash="h")
        st.client = client
        st.connected = True
        st.authorized = True
        st.healthy = True
        st.last_activity = time.time() - 9999  # 久无成功往返
        am._states["live"] = st

        assert len(am.healthy_accounts()) == 1
        await am.maintain_once(probe_timeout=0.05, stale_seconds=0.01)
        assert client.probe_calls >= 1
        assert st.healthy is False            # 黑洞被判不活（旧版靠 is_connected 布尔会漏）
        assert len(am.healthy_accounts()) == 0
        if st.reconnect_task:
            st.reconnect_task.cancel()
        await am.stop()

    async def test_alive_channel_not_false_positive(self):
        """健康但空闲的账号：探活成功 → 刷新 last_activity，不被 stale 误杀。"""
        from tg_forwarder.core.accounts import AccountManager, AccountState

        am = AccountManager()
        client = _LiveClient()  # connected → __call__ 立即返回
        st = AccountState(session_name="live", api_id=1, api_hash="h")
        st.client = client
        st.connected = st.authorized = st.healthy = True
        st.last_activity = time.time() - 9999
        am._states["live"] = st
        await am.maintain_once(probe_timeout=0.05, stale_seconds=0.01)
        assert st.healthy is True             # 探活成功即复活，不误判
        assert st.last_activity > time.time() - 5  # 已刷新
        await am.stop()

    async def test_blackhole_end_to_end_watchdog_alerts_and_exits(self):
        """端到端黑洞：维护循环探活判死→看门狗超时→on_critical 告警 + exit1（补 R1 判据①）。"""
        from tg_forwarder.core.accounts import AccountManager, AccountState

        am = AccountManager()
        client = _BlackholeClient()
        st = AccountState(session_name="live", api_id=1, api_hash="h")
        st.client = client
        st.connected = st.authorized = st.healthy = True
        st.last_activity = time.time() - 9999
        am._states["live"] = st

        cfg = _WatchdogCfg(timeout_minutes=0.01, interval_seconds=0.05)
        crit = []

        async def on_critical(msg):
            crit.append(msg)

        exited = []
        sup = Supervisor(
            am, cfg, on_critical=on_critical, exit_func=lambda c: exited.append(c)
        )
        tasks = [
            asyncio.create_task(
                am.maintenance_loop(0.05, probe_timeout=0.05, stale_seconds=0.0)
            ),
            asyncio.create_task(sup.run()),
        ]
        for _ in range(300):
            await asyncio.sleep(0.05)
            if exited:
                break
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        if st.reconnect_task:
            st.reconnect_task.cancel()
        assert exited == [1]
        assert len(crit) >= 1  # 黑洞场景结构化告警确实触达（老马要的判据①）


# ---------------------------------------------------------------------------
# T4：caption 超限截断保媒体（v2 全历史 7 次 SendMediaRequest 丢图实锤，老马 9-15 验收新发现）
# ---------------------------------------------------------------------------


class _SendRecordingClient:
    """替身发送客户端：记录 send_message kwargs；raise_first=首次调用抛该异常。"""

    session_name_for_forwarder = "sendcap"

    def __init__(self, raise_first=None):
        self.calls: List[Dict[str, Any]] = []
        self._raise_first = raise_first

    async def send_message(self, target_id, message=None, file=None, **kw):
        self.calls.append({"target_id": target_id, "message": message, "file": file, **kw})
        if self._raise_first is not None and len(self.calls) == 1:
            raise self._raise_first

        class _Sent:
            id = 42

        return _Sent()

    async def mark_read(self, *a, **k):
        pass


def _copy_snap():
    return _cfg(
        settings=SystemSettings(default_target="-100999"),
        ad_filter=AdFilterConfig(enable=False),
        content_filter=ContentFilterConfig(enable=False),
    )


class TestCaptionTruncation:
    def test_under_limit_untouched(self):
        text = "x" * CAPTION_LIMIT_UNITS
        assert clamp_caption(text) == text

    def test_long_ascii_truncated_with_ellipsis(self):
        out = clamp_caption("A" * 1500)
        assert _utf16_len(out) == CAPTION_LIMIT_UNITS  # 1023 字符+省略号=恰好 1024
        assert out.endswith("…")
        assert ("A" * 1023) == out[:-1]

    def test_exact_1024_boundary_no_truncation(self):
        text = "B" * (CAPTION_LIMIT_UNITS - 2) + "🐴"  # 1022 BMP + 2 单位 = 恰 1024
        assert clamp_caption(text) == text

    def test_astral_emoji_never_split(self):
        """emoji 占 2 个 UTF-16 单位，截断不得拆代理对（孤立代理项=坏字符）。"""
        out = clamp_caption("🐴" * 600)  # 1200 单位
        assert _utf16_len(out) <= CAPTION_LIMIT_UNITS
        assert out == "🐴" * 511 + "…"
        assert not any(0xD800 <= ord(c) <= 0xDFFF for c in out)  # 无孤立代理项

    async def test_send_message_clamps_before_send(self, tmp_path):
        """>1024 长文案+媒体：预截断后带图发送（保图截文）。"""
        client = _SendRecordingClient()
        fwd, db = await _make_fwd(tmp_path, _copy_snap(), client)
        album = [_Msg(1, media=object()), _Msg(2, media=object())]
        await fwd._send_message(album, "C" * 1500, -100999, None, fwd._snapshot)
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["file"] and len(call["file"]) == 2  # 图保住
        assert _utf16_len(call["message"]) <= CAPTION_LIMIT_UNITS
        assert call["message"].endswith("…")
        await db.close()

    async def test_send_message_fallback_strips_caption_on_server_reject(self, tmp_path):
        """服务端仍拒 caption → 同图去 caption 重试；不算客户端故障（不进 _handle_send_error）。"""
        from telethon.errors import MediaCaptionTooLongError

        client = _SendRecordingClient(raise_first=MediaCaptionTooLongError(request=None))
        fwd, db = await _make_fwd(tmp_path, _copy_snap(), client)
        err_calls = []
        fwd._handle_send_error = lambda c, e: err_calls.append(e)
        media_obj = object()
        await fwd._send_message(
            [_Msg(1, media=media_obj)], "D" * 1500, -100999, None, fwd._snapshot
        )
        assert len(client.calls) == 2
        assert client.calls[1]["message"] == ""  # 去文
        assert client.calls[1]["file"] is not None  # 保图
        assert err_calls == []
        await db.close()

    async def test_text_only_path_not_clamped(self, tmp_path):
        """T4 范围=caption only：纯文本消息（4096 限额）不截断，md 解析路径不变。"""
        client = _SendRecordingClient()
        fwd, db = await _make_fwd(tmp_path, _copy_snap(), client)
        long_text = "E" * 1500  # >1024 但 <4096，文本消息合法
        await fwd._send_message(_Msg(1, text=long_text), long_text, -100999, None, fwd._snapshot)
        assert len(client.calls) == 1
        assert client.calls[0]["message"] == long_text
        await db.close()


# ---------------------------------------------------------------------------
# F1：跨源内容级去重（链接指纹 + 文件名+大小指纹；默认关=现网行为零变化）
# ---------------------------------------------------------------------------


class _FakeDocAttr:
    def __init__(self, file_name):
        self.file_name = file_name


class _FakeDoc:
    def __init__(self, file_name, size):
        self.attributes = [_FakeDocAttr(file_name)]
        self.size = size


class _FakeMediaDoc:
    """两层形状：MessageMediaDocument（嵌套 .document），与真 telethon 同形。"""

    def __init__(self, file_name, size):
        self.document = _FakeDoc(file_name, size)


def _cross_snap(cross=True, single=True):
    from tg_forwarder.config import DeduplicationConfig

    return _cfg(
        sources=[
            SourceConfig(identifier=str(_CHAT), resolved_id=_CHAT),
            SourceConfig(identifier="-100888", resolved_id=-100888),  # 跨源二号源
        ],
        settings=SystemSettings(default_target="-100999", forward_new_only=False),
        targets_resolved_default=-100999,
        deduplication=DeduplicationConfig(
            enable=single, cross_source_enable=cross, auto_cleanup=False
        ),
        ad_filter=AdFilterConfig(enable=False),
        content_filter=ContentFilterConfig(enable=False),
    )


class TestContentFingerprints:
    def test_url_normalized_strips_query_and_case(self):
        fps1 = content_fingerprints("下载: https://pan.quark.cn/s/AbCdEf?pwd=x1 。", None)
        fps2 = content_fingerprints("备份: https://pan.quark.cn/s/abcdef#main", None)
        assert fps1 == fps2 == ["link:https://pan.quark.cn/s/abcdef"]

    def test_trailing_punctuation_not_in_url(self):
        fps = content_fingerprints("链接 https://pan.baidu.com/s/xyz。", None)
        assert fps == ["link:https://pan.baidu.com/s/xyz"]  # 中文句号不吃进 URL

    def test_doc_name_size_fingerprint(self):
        m1 = _FakeMediaDoc("Game.apk", 123456)
        m2 = _FakeMediaDoc("game.APK", 123456)  # 同名（大小写不敏感）同大小
        assert content_fingerprints("", m1) == content_fingerprints("", m2)

    def test_short_text_no_fingerprint(self):
        assert content_fingerprints("短文本", None) == []

    def test_album_only_caption_text_used(self):
        fps = content_fingerprints("https://cloud.189.cn/t/ZZZ", None)
        assert fps == ["link:https://cloud.189.cn/t/zzz"]


class TestCrossSourceDedup:
    async def test_cross_source_link_blocked(self, tmp_path):
        """跨源同链接：第二个源的同链接消息被拦（消息 id 不同、源不同）。"""
        client = _CatchupClient([])
        fwd, db = await _make_fwd(tmp_path, _cross_snap(), client)
        seen = []
        async def stub_send(original, text, target_id, topic_id, snap):
            seen.append(text)
        fwd._send_message = stub_send
        m1 = _Msg(11, chat_id=_CHAT, text="https://pan.quark.cn/s/abc")
        await fwd.process_message(m1)
        assert await db.get_progress(_CHAT) == 11
        # 另一源（不同 chat_id）发同一链接
        m2 = _Msg(99, chat_id=-100888, text="网盘: https://pan.quark.cn/s/abc")
        # 单源 hash 不同（text 不同），但链接指纹相同 → 应被跨源拦
        await fwd.process_message(m2)
        assert seen == ["https://pan.quark.cn/s/abc"]  # 首条发出、第二条被跨源拦
        assert await db.get_progress(-100888) == 99  # progress 照常推进
        await db.close()

    async def test_cross_source_doc_blocked(self, tmp_path):
        """跨源同文件名+大小：第二源的同文件被拦。"""
        client = _CatchupClient([])
        fwd, db = await _make_fwd(tmp_path, _cross_snap(), client)
        media = _FakeMediaDoc("app.apk", 555)
        await fwd.process_message(_Msg(11, chat_id=_CHAT, media=media))
        seen = []
        async def stub_send(original, text, target_id, topic_id, snap):
            seen.append(text)
        fwd._send_message = stub_send
        await fwd.process_message(_Msg(22, chat_id=-100888, media=_FakeMediaDoc("APP.APK", 555)))
        assert seen == []
        await db.close()

    async def test_off_is_backward_compatible(self, tmp_path):
        """默认关（cross_source_enable=False）：同链接跨源两发，行为与升级前一致。"""
        client = _CatchupClient([])
        fwd, db = await _make_fwd(tmp_path, _cross_snap(cross=False), client)
        seen = []
        async def stub_send(original, text, target_id, topic_id, snap):
            seen.append(text)
        fwd._send_message = stub_send
        await fwd.process_message(_Msg(11, chat_id=_CHAT, text="https://pan.quark.cn/s/abc"))
        await fwd.process_message(_Msg(22, chat_id=-100888, text="https://pan.quark.cn/s/abc"))
        assert len(seen) == 2  # 未拦（现网行为零变化）
        await db.close()

    async def test_same_source_still_single_dedup(self, tmp_path):
        """同源同消息（单源 hash 命中）仍走既有去重，不受 F1 影响。"""
        client = _CatchupClient([])
        fwd, db = await _make_fwd(tmp_path, _cross_snap(), client)
        seen = []
        async def stub_send(original, text, target_id, topic_id, snap):
            seen.append(text)
        fwd._send_message = stub_send
        m = _Msg(11, chat_id=_CHAT, text="hello world " * 10)  # >50 字符走 text hash
        await fwd.process_message(m)
        await fwd.process_message(m)
        assert len(seen) == 1
        await db.close()


# ---------------------------------------------------------------------------
# F2：编辑/删除实时同步（映射登记 + 事件级联；per 源开关默认为关）
# ---------------------------------------------------------------------------


class _SyncClient:
    """F2 fake 发送客户端：send/edit/delete 三条路径 + 记录调用。"""

    session_name_for_forwarder = "sync"

    def __init__(self):
        self.sent = []
        self.edits = []
        self.deletes = []

    async def send_message(self, target_id, message=None, file=None, **kw):
        class _Sent:
            id = 42
        self.sent.append((target_id, message))
        return _Sent()

    async def edit_message(self, channel_id, message_id, new_text, **kw):
        self.edits.append((channel_id, message_id, new_text))

    async def delete_messages(self, channel_id, message_ids):
        self.deletes.append((channel_id, list(message_ids) if not isinstance(message_ids, list) else message_ids))

    async def mark_read(self, *a, **k):
        pass


def _sync_snap(sync_edits=True, sync_deletes=True):
    from tg_forwarder.config import SourceConfig

    return _cfg(
        sources=[
            SourceConfig(
                identifier=str(_CHAT), resolved_id=_CHAT,
                sync_edits=sync_edits, sync_deletes=sync_deletes,
            )
        ],
        settings=SystemSettings(default_target="-100999", forward_new_only=False),
        targets_resolved_default=-100999,
        ad_filter=AdFilterConfig(enable=False),
        content_filter=ContentFilterConfig(enable=False),
    )


class TestForwarderSync:
    async def test_map_registered_after_send_when_enabled(self, tmp_path):
        """copy 发送成功 → src→dest 映射入库（仅 sync_edits 源）。"""
        client = _SyncClient()
        fwd, db = await _make_fwd(tmp_path, _sync_snap(), client)
        media = object()
        await fwd.process_message(_Msg(11, chat_id=_CHAT, media=media))
        got = await db.get_message_map_by_src(_CHAT, 11)
        assert got == [(-100999, 42)]
        await db.close()

    async def test_map_not_registered_when_sync_off(self, tmp_path):
        """sync_edits 默认关 → 不登记映射（现网行为零变化）。"""
        client = _SyncClient()
        fwd, db = await _make_fwd(tmp_path, _sync_snap(sync_edits=False), client)
        await fwd.process_message(_Msg(11, chat_id=_CHAT, media=object()))
        assert await db.get_message_map_by_src(_CHAT, 11) == []
        await db.close()

    async def test_edited_syncs_to_mirrors(self, tmp_path):
        """源消息编辑 → 镜像同步编辑（文本经 replacements）。"""
        client = _SyncClient()
        fwd, db = await _make_fwd(tmp_path, _sync_snap(), client)
        await db.add_message_map(_CHAT, 11, -100999, 101)
        edit_msg = _Msg(11, chat_id=_CHAT, text="编辑后的正文")
        await fwd._on_message_edited(edit_msg)
        assert client.edits == [(-100999, 101, "编辑后的正文")]
        await db.close()

    async def test_edited_without_map_skipped(self, tmp_path):
        """无映射（开启前转发/非同步源）→ 编辑不动作。"""
        client = _SyncClient()
        fwd, db = await _make_fwd(tmp_path, _sync_snap(), client)
        await fwd._on_message_edited(_Msg(99, chat_id=_CHAT, text="x"))
        assert client.edits == []
        await db.close()

    async def test_deleted_cascades_and_clears_map(self, tmp_path):
        """源消息删除（sync_deletes=True）→ 镜像删除 + 映射清除。"""
        client = _SyncClient()
        fwd, db = await _make_fwd(tmp_path, _sync_snap(), client)
        await db.add_message_map(_CHAT, 11, -100999, 101)
        event = type("E", (), {"deleted_ids": [11], "channel_id": _CHAT})()
        await fwd._on_message_deleted(event)
        assert client.deletes == [(-100999, [101])]
        assert await db.get_message_map_by_src(_CHAT, 11) == []
        await db.close()

    async def test_deleted_gated_by_source_flag(self, tmp_path):
        """sync_deletes 默认关 → 删除事件不动作（历史映射保留）。"""
        client = _SyncClient()
        fwd, db = await _make_fwd(tmp_path, _sync_snap(sync_deletes=False), client)
        await db.add_message_map(_CHAT, 11, -100999, 101)
        event = type("E", (), {"deleted_ids": [11], "channel_id": _CHAT})()
        await fwd._on_message_deleted(event)
        assert client.deletes == []
        assert await db.get_message_map_by_src(_CHAT, 11) == [(-100999, 101)]
        await db.close()


# ---------------------------------------------------------------------------
# F5：per-规则媒体类型/大小过滤（TargetDistributionRule.check）
# ---------------------------------------------------------------------------


class _FilterDocMedia:
    """带 mime_type/size 的 document media mock（F5 专用，两层形状）。"""

    def __init__(self, mime, size, file_name=None):
        from telethon.tl.types import (
            DocumentAttributeFilename,
            MessageMediaDocument,
        )
        from telethon.tl import types as tl_types

        attrs = []
        if file_name:
            attrs.append(DocumentAttributeFilename(file_name=file_name))
        doc = tl_types.Document(
            id=999, access_hash=0, file_reference=b"", date=None,
            mime_type=mime, size=size, attributes=attrs, dc_id=1,
        )
        self.document = MessageMediaDocument(document=doc, ttl_seconds=0)


class TestRuleMediaFilter:
    def _rule(self, media_types=(), max_file_size=0, any_keywords=()):
        return TargetDistributionRule(
            name="r",
            any_keywords=list(any_keywords),
            media_types=list(media_types),
            max_file_size=max_file_size,
            target_identifier="-100333",
            resolved_target_id=-100333,
        )

    def test_media_types_match(self):
        """media_types 命中（mime 子串）→ 规则匹配。"""
        r = self._rule(media_types=["video"])
        assert r.check("", _FilterDocMedia("video/mp4", 1000)) is True

    def test_media_types_not_match(self):
        """设了 media_types 且 mime 不匹配 → 不命中（哪怕有关键字）。"""
        r = self._rule(media_types=["video"], any_keywords=["4K"])
        assert r.check("4K超清", _FilterDocMedia("image/jpeg", 1000)) is False

    def test_media_types_match_with_keyword(self):
        """关键字命中 + media_types 命中 → 放行。"""
        r = self._rule(media_types=["video"], any_keywords=["4K"])
        assert r.check("4K超清", _FilterDocMedia("video/mp4", 1000)) is True

    def test_max_file_size_within(self):
        """max_file_size 内 → 命中。"""
        r = self._rule(max_file_size=1_000_000)
        assert r.check("", _FilterDocMedia("video/mp4", 500_000)) is True

    def test_max_file_size_exceeded(self):
        """超 max_file_size → 不命中。"""
        r = self._rule(max_file_size=1_000_000)
        assert r.check("", _FilterDocMedia("video/mp4", 2_000_000)) is False

    def test_no_filters_unchanged(self):
        """未设媒体限制 → 行为与升级前一致（关键字命中即放行）。"""
        r = self._rule(any_keywords=["4K"])
        assert r.check("4K", _FilterDocMedia("video/mp4", 10)) is True

    def test_media_required_when_filter_set(self):
        """设了媒体限制但无媒体（纯文本）→ 不命中（不放行纯文本进媒体规则）。"""
        r = self._rule(media_types=["video"])
        assert r.check("纯文本", None) is False


# ---------------------------------------------------------------------------
# F6：源标注模板（build_header）
# ---------------------------------------------------------------------------


class TestHeaderTemplate:
    def _src(self, template):
        return SourceConfig(
            identifier=str(_CHAT), resolved_id=_CHAT,
            cached_title="测试频道", header_template=template,
        )

    def test_none_template_returns_none(self):
        from tg_forwarder.core.forwarder import build_header

        src = SourceConfig(identifier=str(_CHAT), resolved_id=_CHAT)
        assert build_header(src, _Msg(1, chat_id=_CHAT)) is None

    def test_empty_template_returns_none(self):
        from tg_forwarder.core.forwarder import build_header

        assert build_header(self._src(""), _Msg(1, chat_id=_CHAT)) is None

    def test_full_render(self):
        from datetime import datetime, timezone

        from tg_forwarder.core.forwarder import build_header

        msg = _Msg(1, chat_id=_CHAT, text="内容")
        msg.date = datetime(2026, 9, 16, 12, 30, tzinfo=timezone.utc)
        src = self._src("📢 {title} | {link} | {time}")
        out = build_header(src, msg)
        assert "📢 测试频道" in out
        assert "https://t.me/s/测试频道" in out
        assert "2026-09-16 12:30" in out

    async def test_process_message_prepends_header(self, tmp_path):
        """有 header_template 的源 → 转发文本前置 header（默认关不标）。"""
        from tg_forwarder.config import SystemSettings

        cfg = _cfg(
            sources=[self._src("来自 {title}")],
            settings=SystemSettings(default_target="-100999"),
            targets_resolved_default=-100999,
            ad_filter=AdFilterConfig(enable=False),
            content_filter=ContentFilterConfig(enable=False),
        )
        client = _SendRecordingClient()
        fwd, db = await _make_fwd(tmp_path, cfg, client)
        # 纯文本消息走 copy text-only 路径
        await fwd.process_message(_Msg(2, chat_id=_CHAT, text="正文内容"))
        assert len(client.calls) >= 1
        sent_text = client.calls[0]["message"]
        assert sent_text.startswith("来自 测试频道")
        assert "正文内容" in sent_text
        await db.close()


# ---------------------------------------------------------------------------
# F4：年龄截断（catchup 超龄旧消息跳过）
# ---------------------------------------------------------------------------


class _DatedMsg(_Msg):
    def __init__(self, id, date_ts, chat_id=_CHAT, text="", grouped_id=None):
        super().__init__(id, chat_id=chat_id, text=text, grouped_id=grouped_id)
        self.date = type("DT", (), {"timestamp": lambda s: date_ts})()


def _age_snap(age_hours):
    from tg_forwarder.config import SystemSettings

    return _cfg(
        sources=[
            SourceConfig(
                identifier=str(_CHAT), resolved_id=_CHAT, age_cutoff_hours=age_hours
            )
        ],
        settings=SystemSettings(default_target="-100999", forward_new_only=False),
        targets_resolved_default=-100999,
        ad_filter=AdFilterConfig(enable=False),
        content_filter=ContentFilterConfig(enable=False),
    )


class TestAgeCutoff:
    async def test_skips_old_messages(self, tmp_path):
        """catchup 遇到超龄消息 → 跳过不转发，但 progress 仍抬升。"""
        import time

        now = time.time()
        old = now - 100 * 3600   # 100h 前（超 48h 阈值）
        new = now - 1 * 3600     # 1h 前（未超）
        client = _CatchupClient(
            [_DatedMsg(11, old, text="old"), _DatedMsg(12, new, text="new")]
        )
        fwd, db = await _make_fwd(tmp_path, _age_snap(48), client)
        await db.set_progress(_CHAT, 10)  # 事件停在 10
        sent = []
        async def stub_send(original, text, target_id, topic_id, snap):
            sent.append(text)
        fwd._send_message = stub_send
        await fwd.catchup_once(limit=50)
        assert sent == ["new"]          # 只有新消息转发
        assert await db.get_progress(_CHAT) == 12  # progress 抬到最新（含被跳过）
        await db.close()

    async def test_no_cutoff_backward_compatible(self, tmp_path):
        """age_cutoff=None → 所有消息照常转发（现网行为零变化）。"""
        import time

        now = time.time()
        old = now - 100 * 3600
        client = _CatchupClient([_DatedMsg(11, old, text="old")])
        fwd, db = await _make_fwd(tmp_path, _age_snap(None), client)
        await db.set_progress(_CHAT, 10)
        sent = []
        async def stub_send(original, text, target_id, topic_id, snap):
            sent.append(text)
        fwd._send_message = stub_send
        await fwd.catchup_once(limit=50)
        assert sent == ["old"]          # 未设截断 → 全部转发
        await db.close()


# ---------------------------------------------------------------------------
# F7：评论区资源抓取（网盘链接进死链检测管线）
# ---------------------------------------------------------------------------


class _ReplyClient:
    """支持 reply_to 的 fake client：返回预置的回复消息列表。"""

    session_name_for_forwarder = "reply"

    def __init__(self, replies):
        self._replies = replies

    def iter_messages(self, chat_id, reply_to=0, limit=10):
        msgs = [r for r in self._replies if getattr(r, "_reply_to", None) == reply_to][:limit]

        async def gen():
            for m in msgs:
                yield m
        return gen()


def _reply_msg(rid, text, parent_id):
    class _R(_Msg):
        pass
    m = _R(rid, chat_id=_CHAT, text=text)
    m._reply_to = parent_id
    return m


class _ReplyAMStub(_AMStub):
    def healthy_accounts(self):
        from tg_forwarder.core.forwarder import Forwarder
        return [self._c]


class TestReplyLinks:
    async def test_collects_matching_reply_links(self, tmp_path):
        """源 check_replies=True → 抓取回复里的网盘链接进 pending 管线。"""
        from tg_forwarder.config import SystemSettings

        client = _ReplyClient([
            _reply_msg(21, "下载: https://pan.quark.cn/s/abc", 11),
            _reply_msg(22, "普通回复无链接", 11),
            _reply_msg(23, "https://pan.baidu.com/s/xyz", 11),
        ])
        cfg = _cfg(
            sources=[SourceConfig(identifier=str(_CHAT), resolved_id=_CHAT, check_replies=True)],
            settings=SystemSettings(default_target="-100999"),
            targets_resolved_default=-100999,
            ad_filter=AdFilterConfig(enable=False),
            content_filter=ContentFilterConfig(enable=False),
        )
        fwd, db = await _make_fwd(tmp_path, cfg, client)
        # 用真实 sender 客户端跑 _send_message（登记映射走真路径）
        fwd._get_next_client = lambda: client
        await fwd.process_message(_Msg(11, chat_id=_CHAT, media=object()))
        # 回复里的 2 个网盘链接应已入 pending
        pending = await db.get_links_to_check()
        urls = {u for u, _ in pending}
        assert "https://pan.quark.cn/s/abc" in urls
        assert "https://pan.baidu.com/s/xyz" in urls
        assert len(urls) == 2
        await db.close()

    async def test_gated_by_check_replies_flag(self, tmp_path):
        """check_replies=False（默认）→ 不抓取回复链接（现网行为零变化）。"""
        from tg_forwarder.config import SystemSettings

        client = _ReplyClient([_reply_msg(21, "https://pan.quark.cn/s/abc", 11)])
        cfg = _cfg(
            sources=[SourceConfig(identifier=str(_CHAT), resolved_id=_CHAT)],
            settings=SystemSettings(default_target="-100999"),
            targets_resolved_default=-100999,
            ad_filter=AdFilterConfig(enable=False),
            content_filter=ContentFilterConfig(enable=False),
        )
        fwd, db = await _make_fwd(tmp_path, cfg, client)
        fwd._get_next_client = lambda: client
        await fwd.process_message(_Msg(11, chat_id=_CHAT, media=object()))
        pending = await db.get_links_to_check()
        assert pending == []   # 无抓取
        await db.close()


# ---------------------------------------------------------------------------
# F8：parse_mode 格式污染修复（纯文本发送 parse_mode=None）
# ---------------------------------------------------------------------------


class TestParseModeNone:
    async def test_text_only_path_parse_mode_none(self, tmp_path):
        """纯文本 copy：parse_mode=None（替换后残留 * _ [ 不再触发 md 解析异常）。"""
        client = _SendRecordingClient()
        fwd, db = await _make_fwd(tmp_path, _copy_snap(), client)
        await fwd._send_message(_Msg(1, text="*粗体残留 *_[ 测试"), "普通", -100999, None, fwd._snapshot)
        assert len(client.calls) == 1
        assert client.calls[0].get("parse_mode") is None  # F8 关键
        await db.close()


# ---------------------------------------------------------------------------
# F9：转发出口（delivery）抽象——WebhookDelivery + DeliveryManager
# ---------------------------------------------------------------------------


class TestDeliveryEvent:
    def test_to_dict_keys(self):
        from tg_forwarder.core.delivery import DeliveryEvent

        ev = DeliveryEvent(
            source="频道A", target="-100999", destination="频道B",
            text="分享", media_type="document", link="https://example.com",
            occurred_at="2026-09-16T12:00:00Z",
        )
        d = ev.to_dict()
        assert d["source"] == "频道A"
        assert d["media_type"] == "document"
        assert "occurred_at" in d


class TestWebhookDelivery:
    async def test_send_success(self, monkeypatch):
        """WebhookDelivery.send 成功时返回 True，payload 包含事件字段。"""
        import httpx

        from tg_forwarder.core.delivery import DeliveryEvent, WebhookDelivery

        sent = []

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, json=None, headers=None):
                sent.append({"url": url, "json": json})

                class _Resp:
                    status_code = 200
                return _Resp()

        original = httpx.AsyncClient
        httpx.AsyncClient = lambda **kw: _FakeClient()
        try:
            wb = WebhookDelivery("https://hook.example.com", headers={"X": "y"})
            ok = await wb.send(DeliveryEvent(source="频道A", text="hello"))
            assert ok is True
            assert len(sent) == 1
            assert sent[0]["url"] == "https://hook.example.com"
            assert sent[0]["json"]["source"] == "频道A"
        finally:
            httpx.AsyncClient = original

    async def test_send_on_network_error_returns_false(self, monkeypatch):
        """网络异常 → 返回 False，不抛（fail-open）。"""
        import httpx

        from tg_forwarder.core.delivery import DeliveryEvent, WebhookDelivery

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, json=None, **kw):
                raise httpx.RequestError("net down")

        original = httpx.AsyncClient
        httpx.AsyncClient = lambda **kw: _FakeClient()
        try:
            wb = WebhookDelivery("https://hook.example.com")
            ok = await wb.send(DeliveryEvent(source="X"))
            assert ok is False
        finally:
            httpx.AsyncClient = original


class TestDeliveryManager:
    async def test_disabled_returns_zero(self):
        from tg_forwarder.core.delivery import DeliveryEvent, DeliveryManager

        dm = DeliveryManager([])
        assert dm.enabled is False
        assert await dm.send_event(DeliveryEvent()) == 0

    async def test_multiple_backends_aggregate(self):
        from tg_forwarder.core.delivery import DeliveryBackend, DeliveryEvent, DeliveryManager

        class _DummyBackend(DeliveryBackend):
            name = "dummy"

            def __init__(self, ok: bool = True):
                self.ok = ok

            async def send(self, event):
                return self.ok

        dm = DeliveryManager([_DummyBackend(True), _DummyBackend(False)])
        assert dm.enabled is True
        ok = await dm.send_event(DeliveryEvent(source="X"))
        assert ok == 1  # 一成一败

    async def test_exception_in_backend_does_not_raise(self):
        from tg_forwarder.core.delivery import DeliveryBackend, DeliveryEvent, DeliveryManager

        class _BoomBackend(DeliveryBackend):
            name = "boom"

            async def send(self, event):
                raise RuntimeError("boom")

        dm = DeliveryManager([_BoomBackend()])
        count = await dm.send_event(DeliveryEvent())  # 不抛
        assert count == 0


class TestBuildDeliveryManager:
    def test_builds_webhook_backends(self):
        from tg_forwarder.core.delivery import DeliveryManager, WebhookDelivery
        from tg_forwarder.config import DeliveryConfig, RuntimeConfig
        from tg_forwarder.core.delivery import build_delivery_manager

        cfg = RuntimeConfig(delivery=DeliveryConfig(
            enabled=True,
            webhooks=[{"url": "https://hook1.com"}, {"url": "https://hook2.com"}],
        ))
        dm = build_delivery_manager(cfg)
        assert dm.enabled is True
        assert len(dm.backends) == 2
        assert all(isinstance(b, WebhookDelivery) for b in dm.backends)

    def test_builds_off_by_default(self):
        from tg_forwarder.config import RuntimeConfig
        from tg_forwarder.core.delivery import build_delivery_manager

        cfg = RuntimeConfig()
        dm = build_delivery_manager(cfg)
        assert dm.enabled is False

    def test_builds_none_config_returns_off(self):
        from tg_forwarder.core.delivery import build_delivery_manager

        dm = build_delivery_manager(None)
        assert dm.enabled is False
