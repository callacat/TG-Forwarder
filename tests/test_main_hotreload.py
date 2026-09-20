# -*- coding: utf-8 -*-
"""main.py 热重载实验功能对齐（Codex major：F10/F12 装配期一次性组件重建）。

覆盖 `_reconcile_ai_features`：
- F12 语义引擎：开启注入 / 关闭摘除 / 阈值变化重建 / 无变化保留（不丢已加载模型/窗口）；
- F10 digest 管线：开启装配 / 关闭摘除 / 无变化保留（不丢滚动窗口缓冲）；
- 两者默认关闭时保持不装配（现网行为零变化）。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _reconcile_ai_features  # noqa: E402
from tg_forwarder.config import RuntimeConfig  # noqa: E402


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