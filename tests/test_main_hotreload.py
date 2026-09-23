# -*- coding: utf-8 -*-
"""main.py 热重载链路测试。

一、实验功能对齐（Codex major：F10/F12 装配期一次性组件重建）——
覆盖 `_reconcile_ai_features`：
- F12 语义引擎：开启注入 / 关闭摘除 / 阈值变化重建 / 无变化保留（不丢已加载模型/窗口）；
- F10 digest 管线：开启装配 / 关闭摘除 / 无变化保留（不丢滚动窗口缓冲）；
- 两者默认关闭时保持不装配（现网行为零变化）。

二、目标重解析（rc.6 回归根治）——覆盖 `_apply_hot_reload` 与 `_resolve_targets_on_reload`：
- ★接线回归：真实热重载链路必须重解析目标（rc.6 事故即接线缺失），验收 1；
- 连续两次保存不清空目标、热重载后新消息命中规则可正常转发，验收 2；
- 无健康账号 / 解析失败时降级沿用上次解析值；标识符变了不沿用。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (  # noqa: E402
    _apply_hot_reload,
    _reconcile_ai_features,
    _resolve_targets_on_reload,
)
from tg_forwarder.config import (  # noqa: E402
    AdFilterConfig,
    ContentFilterConfig,
    RuntimeConfig,
    SourceConfig,
    SystemSettings,
    TargetDistributionRule,
)
from tg_forwarder.core.forwarder import Forwarder, find_target  # noqa: E402


def _fwd():
    return SimpleNamespace(semantic_engine=None, digest_pipeline=None)


def _with_dedup(enabled, threshold=0.85):
    cfg = RuntimeConfig()
    cfg.deduplication.semantic_dedup_enabled = enabled
    cfg.deduplication.semantic_dedup_threshold = threshold
    return cfg


def _with_digest(enabled, interval=1800):
    cfg = RuntimeConfig()
    cfg.digest.enabled = enabled
    cfg.digest.interval_seconds = interval
    return cfg


class TestReconcileSemanticEngine:
    def test_enable_from_none_builds_engine_with_threshold(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_dedup(True, 0.9))
        assert f.semantic_engine is not None
        assert f.semantic_engine.engine.threshold == 0.9

    def test_disable_removes_engine(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_dedup(True))
        assert f.semantic_engine is not None
        _reconcile_ai_features(f, _with_dedup(False))
        assert f.semantic_engine is None

    def test_threshold_change_rebuilds(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_dedup(True, 0.8))
        old = f.semantic_engine
        _reconcile_ai_features(f, _with_dedup(True, 0.9))
        assert f.semantic_engine is not old
        assert f.semantic_engine.engine.threshold == 0.9

    def test_unchanged_keeps_engine_identity(self):
        """无变化不重建：保留已加载模型与内存窗口（避免每次普通设置保存重载模型）。"""
        f = _fwd()
        c = _with_dedup(True, 0.85)
        _reconcile_ai_features(f, c)
        old = f.semantic_engine
        _reconcile_ai_features(f, c)
        assert f.semantic_engine is old


class TestReconcileDigest:
    def test_enable_from_none_builds_pipeline(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_digest(True, 600))
        assert f.digest_pipeline is not None
        assert f.digest_pipeline._fwd is f

    def test_disable_removes_pipeline(self):
        f = _fwd()
        _reconcile_ai_features(f, _with_digest(True))
        assert f.digest_pipeline is not None
        _reconcile_ai_features(f, _with_digest(False))
        assert f.digest_pipeline is None

    def test_unchanged_keeps_pipeline_identity(self):
        """无变化保留原管线：不丢已缓冲的滚动窗口摘要进度。"""
        f = _fwd()
        c = _with_digest(True)
        _reconcile_ai_features(f, c)
        old = f.digest_pipeline
        _reconcile_ai_features(f, c)
        assert f.digest_pipeline is old


class TestReconcileCombined:
    def test_both_features_reconcile(self):
        f = _fwd()
        cfg = RuntimeConfig()
        cfg.digest.enabled = True
        cfg.digest.interval_seconds = 900
        cfg.deduplication.semantic_dedup_enabled = True
        cfg.deduplication.semantic_dedup_threshold = 0.88
        _reconcile_ai_features(f, cfg)
        assert f.semantic_engine is not None
        assert f.semantic_engine.engine.threshold == 0.88
        assert f.digest_pipeline is not None

    def test_defaults_off_leave_uninjected(self):
        f = _fwd()
        _reconcile_ai_features(f, RuntimeConfig())
        assert f.semantic_engine is None
        assert f.digest_pipeline is None


# ---------------------------------------------------------------------------
# rc.6 回归：热重载后目标重解析（面板保存不得把已解析目标清空）
# ---------------------------------------------------------------------------


class _Ent:
    def __init__(self, id, title="t"):
        self.id = id
        self.title = title


class _ResolveClient:
    """假 TelegramClient：get_entity 按映射命中，未命中视为解析失败。"""

    session_name_for_forwarder = "resolver"

    def __init__(self, mapping):
        self.mapping = mapping

    async def get_entity(self, key):
        key = str(key)  # normalize_target 会把纯数字标识符转 int
        if key in self.mapping:
            return _Ent(self.mapping[key])
        raise ValueError(f"no entity: {key}")

    def iter_dialogs(self, limit=None):
        async def gen():
            return
            yield  # pragma: no cover —— 空异步生成器（刷新缓存路径）

        return gen()


class _AMStub:
    """假 AccountManager：healthy_accounts 可控（模拟账号掉线降级）。"""

    def __init__(self, clients):
        self.clients = list(clients)

    def healthy_accounts(self):
        return list(self.clients)


class _Msg:
    def __init__(self, id, chat_id, text="", media=None, grouped_id=None):
        self.id = id
        self.chat_id = chat_id
        self.text = text
        self.media = media
        self.grouped_id = grouped_id


def _rule(name, keyword, target):
    return TargetDistributionRule(
        name=name, any_keywords=[keyword], target_identifier=target
    )


def _new_fwd(clients, db=None):
    return Forwarder(db or type("DB", (), {})(), _AMStub(clients))


class TestReloadTargetResolution:
    async def test_apply_hot_reload_wires_resolution(self):
        """★接线回归：真实热重载链路 `_apply_hot_reload` 必须重解析目标。

        rc.6 事故是回调漏调 resolve_targets（接线缺失），故这里打真实链路而非
        辅助函数——若有人删掉 _apply_hot_reload 里的重解析调用，本用例必红。
        """
        client = _ResolveClient({"-100111": -100111})
        fwd = _new_fwd([client])

        cfg = RuntimeConfig()
        cfg.distribution_rules = [_rule("r1", "电影", "-100111")]
        cfg.settings.default_target = "-100111"
        await _apply_hot_reload(fwd, fwd._am, cfg)

        snap = fwd.get_snapshot()
        assert snap.distribution_rules[0].resolved_target_id == -100111
        assert snap.targets_resolved_default == -100111

    async def test_apply_hot_reload_second_save_keeps_targets(self):
        """连续两次保存（模拟现网 08:33/08:35）：第二次也不得把目标清空。"""
        client = _ResolveClient({"-100111": -100111})
        fwd = _new_fwd([client])

        first = RuntimeConfig()
        first.distribution_rules = [_rule("r1", "电影", "-100111")]
        first.settings.default_target = "-100111"
        await _apply_hot_reload(fwd, fwd._am, first)
        assert fwd.get_snapshot().distribution_rules[0].resolved_target_id == -100111

        second = RuntimeConfig()  # 全新对象，规则 resolved_target_id=None
        second.distribution_rules = [_rule("r1", "电影", "-100111")]
        second.settings.default_target = "-100111"
        await _apply_hot_reload(fwd, fwd._am, second)
        assert fwd.get_snapshot().distribution_rules[0].resolved_target_id == -100111
        assert fwd.get_snapshot().targets_resolved_default == -100111

    async def test_reload_reresolves_all_rule_targets(self):
        """验收 1：面板保存换新规则对象后重解析 → resolved_target_id 全有值。"""
        client = _ResolveClient({"-100111": -100111, "chan2": -100222})
        fwd = _new_fwd([client])

        cfg = RuntimeConfig()
        cfg.distribution_rules = [_rule("r1", "电影", "-100111")]
        cfg.settings.default_target = "-100111"
        fwd.update_snapshot(cfg)
        await _resolve_targets_on_reload(fwd, fwd._am)  # 启动路径等价
        assert fwd.get_snapshot().distribution_rules[0].resolved_target_id == -100111

        # 面板保存现场：load_runtime_config 重建 → update_snapshot，规则对象全新、
        # resolved_target_id 默认 None（rc.6 时热重载到此为止 → 转发全停）
        new_cfg = RuntimeConfig()
        new_cfg.distribution_rules = [
            _rule("r1", "电影", "-100111"),
            _rule("r2", "音乐", "chan2"),
        ]
        new_cfg.settings.default_target = "chan2"
        prev_snap = fwd.get_snapshot()  # 与 update_settings_cb 同序：替换前取
        fwd.update_snapshot(new_cfg)
        assert [
            r.resolved_target_id for r in fwd.get_snapshot().distribution_rules
        ] == [None, None]

        await _resolve_targets_on_reload(fwd, fwd._am, prev_snap)
        snap = fwd.get_snapshot()
        assert [r.resolved_target_id for r in snap.distribution_rules] == [
            -100111,
            -100222,
        ]
        assert snap.targets_resolved_default == -100222

    async def test_message_forwarded_after_hot_reload(self, tmp_path):
        """验收 2：热重载后新消息命中规则 → 发送成功（不再「无有效目标」）。"""
        from tg_forwarder.storage.db import Database

        db = Database(str(tmp_path / "hotreload.sqlite"))
        await db.open()
        await db.migrate()
        try:
            client = _ResolveClient({"-100777": -100777})
            fwd = _new_fwd([client], db=db)
            cfg = RuntimeConfig(
                sources=[SourceConfig(identifier="-100555", resolved_id=-100555)],
                settings=SystemSettings(default_target="-100777"),
                distribution_rules=[_rule("r", "电影", "-100777")],
                ad_filter=AdFilterConfig(enable=False),
                content_filter=ContentFilterConfig(enable=False),
            )
            fwd.update_snapshot(cfg)
            # rc.6 回归现场：新快照规则 resolved 为 None → 命中规则也拿不到目标
            assert find_target("电影下载", None, fwd.get_snapshot()) == (None, None)

            await _resolve_targets_on_reload(fwd, fwd._am)

            sent = []

            async def stub_send(original, text, target_id, topic_id, snap):
                sent.append((text, target_id))

            fwd._send_message = stub_send
            await fwd.process_message(_Msg(11, chat_id=-100555, text="电影下载"))
            assert sent == [("电影下载", -100777)]
            assert fwd._msg_stats["failed"] == 0
        finally:
            await db.close()

    async def test_no_healthy_account_keeps_previous_targets(self):
        """降级：重载时无健康账号 → 沿用上次解析值，不把可用目标清空。"""
        client = _ResolveClient({"-100111": -100111})
        am = _AMStub([client])
        fwd = Forwarder(type("DB", (), {})(), am)

        cfg = RuntimeConfig()
        cfg.distribution_rules = [_rule("r1", "电影", "-100111")]
        cfg.settings.default_target = "-100111"
        fwd.update_snapshot(cfg)
        await _resolve_targets_on_reload(fwd, am)

        am.clients = []  # 账号全部掉线
        new_cfg = RuntimeConfig()
        new_cfg.distribution_rules = [_rule("r1", "电影", "-100111")]
        new_cfg.settings.default_target = "-100111"
        prev_snap = fwd.get_snapshot()
        fwd.update_snapshot(new_cfg)
        await _resolve_targets_on_reload(fwd, am, prev_snap)

        snap = fwd.get_snapshot()
        assert snap.distribution_rules[0].resolved_target_id == -100111
        assert snap.targets_resolved_default == -100111

    async def test_changed_identifier_not_backfilled_from_old(self):
        """标识符改了的规则解析失败 → 保持 None（不张冠李戴沿用旧目标）。"""
        client = _ResolveClient({"-100111": -100111})
        am = _AMStub([client])
        fwd = Forwarder(type("DB", (), {})(), am)

        cfg = RuntimeConfig()
        cfg.distribution_rules = [_rule("r1", "电影", "-100111")]
        fwd.update_snapshot(cfg)
        await _resolve_targets_on_reload(fwd, am)

        new_cfg = RuntimeConfig()
        new_cfg.distribution_rules = [_rule("r1", "电影", "-100999")]  # 改到不可解析目标
        prev_snap = fwd.get_snapshot()
        fwd.update_snapshot(new_cfg)
        await _resolve_targets_on_reload(fwd, am, prev_snap)

        assert fwd.get_snapshot().distribution_rules[0].resolved_target_id is None