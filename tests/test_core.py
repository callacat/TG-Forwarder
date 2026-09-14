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
    Forwarder,
    apply_replacements,
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
