# -*- coding: utf-8 -*-
"""F12 语义去重（fastembed，默认关=现网行为零变化；模型缺失/加载失败优雅降级）。

设计要点（本测试即契约）：
- 开关默认 False：process_message 不做任何 embedding 计算、零语义检查，
  行为与 M3 完全一致（回归断言：语义模块根本不被 import/实例化路径不触发）。
- 开关 True 且模型可用（mock）：语义相同（embedding 相近）的消息被拦，
  语义不同（embedding 距离远）放行；仍然复用既有 dedup_hashes 表承载向量
  指纹（"sem:<sha256(低位二进制)>"），不新增表/迁移（绕开 db.py 迁移缺陷）。
- 模型缺失/加载失败（mock raise）：静默禁用（enabled 语义 = off），
  不抛异常、不影响既有 dedup 行为、不阻塞转发主线。

依赖注入：SemanticDedupEngine 接受 embed_fn（可 mock），默认走 fastembed
TextEmbedding。模型加载惰性：首次 use 才触发；失败则永久置 disabled。
"""
import asyncio

import pytest

from tg_forwarder.config import DeduplicationConfig, RuntimeConfig, SourceConfig, SystemSettings, AdFilterConfig, ContentFilterConfig
from tg_forwarder.core.semantic_dedup import (
    SemanticDedup,
    SemanticDedupEngine,
    cosine_similarity,
    embedding_fingerprint,
)


def _sem_cfg(semantic=True, dedup_enable=True, cross=False):
    return RuntimeConfig(
        sources=[SourceConfig(identifier="-100555", resolved_id=-100555)],
        settings=SystemSettings(default_target="-100999"),
        targets_resolved_default=-100999,
        ad_filter=AdFilterConfig(enable=False),
        content_filter=ContentFilterConfig(enable=False),
        deduplication=DeduplicationConfig(
            enable=dedup_enable,
            cross_source_enable=cross,
            semantic_dedup_enabled=semantic,
        ),
    )


class _Msg:
    def __init__(self, id, chat_id=-100555, text="", media=None, grouped_id=None):
        self.id = id
        self.chat_id = chat_id
        self.text = text
        self.media = media
        self.grouped_id = grouped_id


class _AM:
    def healthy_accounts(self):
        return []

    def all_status(self):
        return {"accounts": []}

    def record_flood_wait(self, *a, **k):
        pass


async def _make_fwd(tmp_path, snapshot, client=None, engine=None):
    import os

    from tg_forwarder.storage.db import Database

    db = Database(os.path.join(str(tmp_path), "sem.sqlite"))
    await db.open()
    await db.migrate()
    fwd = ForwarderStub(db, _AM(), client, engine)
    fwd.update_snapshot(snapshot)
    return fwd, db

from tg_forwarder.core.forwarder import Forwarder


class ForwarderStub(Forwarder):
    """绑定语义引擎的 Forwarder 轻量子类（注入 mock embed）。"""

    def __init__(self, db, am, client=None, engine=None):
        super().__init__(db, am)
        self._client = client
        # 生产装配点：main.py 会在这里注入真实 SemanticDedupEngine；
        # 测试直接注入 mock 引擎。
        self.semantic_engine = engine

    def _get_next_client(self):
        return self._client


# ---------------------------------------------------------------------------
# 纯函数：余弦相似度 / 向量指纹
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_is_1(self):
        assert cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_is_0(self):
        assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_zero_vector_guarded(self):
        assert cosine_similarity([0, 0], [1, 0]) == 0.0

    def test_dimension_mismatch_zero(self):
        assert cosine_similarity([1, 0, 0], [1, 0]) == 0.0


class TestEmbeddingFingerprint:
    def test_deterministic_and_semantic(self):
        a = embedding_fingerprint([0.1, 0.2, 0.3, 0.4, 0.5] * 4)
        b = embedding_fingerprint([0.1, 0.2, 0.3, 0.4, 0.5] * 4)
        assert a == b
        assert a.startswith("sem:")

    def test_slightly_different_embeddings_differ(self):
        a = embedding_fingerprint([0.1, 0.2, 0.3, 0.4, 0.5] * 4)
        b = embedding_fingerprint([0.11, 0.2, 0.3, 0.4, 0.5] * 4)
        assert a != b


# ---------------------------------------------------------------------------
# 语义引擎：模型加载失败优雅降级
# ---------------------------------------------------------------------------


class TestSemanticDedupEngine:
    async def test_model_load_failure_gracefully_disables(self):
        def boom_embed(*a, **k):
            raise RuntimeError("model download failed")

        eng = SemanticDedupEngine(embed_fn=boom_embed, model_name="fake/model")
        # 加载失败 → 不抛，feature 失效
        ready = await eng.ensure_ready()
        assert ready is False
        assert eng.enabled is False
        # 后续调用直接 no-op，不再次尝试崩溃
        assert await eng.check_duplicate("anything", 1) is False

    async def test_enabled_engine_checks_similarity(self):
        # mock embed：确定性返回 [config dim] 向量
        eng = SemanticDedupEngine(
            embed_fn=lambda texts, **k: [[0.1] * 8 for _ in texts],
            model_name="fake/model",
        )
        assert await eng.ensure_ready() is True
        assert await eng.check_duplicate("第一条", 1) is False
        assert await eng.check_duplicate("第一条的近似说法", 2) is True  # 同向量→重复
        assert await eng.check_duplicate("完全不同的内容", 3) is True  # 同向量→重复


# ---------------------------------------------------------------------------
# Forwarder 集成：开关关闭=零语义检查（回归断言）
# ---------------------------------------------------------------------------


class TestSemanticDedupDisabled:
    async def test_disabled_zero_semantic_checks(self, tmp_path):
        """默认关：语义引擎不被构造/调用（Forwarder 不自主实例化），行为与 M3 一致。"""
        calls = {"init": 0}

        class _BoomEngine:
            def __init__(self, *a, **k):
                calls["init"] += 1

        # 不注入引擎：Forwarder 自身不应构造任何语义引擎
        cfg = _sem_cfg(semantic=False)
        fwd, db = await _make_fwd(tmp_path, cfg, engine=None)
        sent = []

        async def stub_send(original, text, target_id, topic_id, snap):
            sent.append(text)

        fwd._send_message = stub_send
        await fwd.process_message(_Msg(1, text="hello"))
        assert sent == ["hello"]
        # 关键：Forwarder 从未构造/调用语义引擎
        assert calls["init"] == 0
        assert fwd.semantic_engine is None
        await db.close()

    async def test_enabled_but_engine_failed_does_not_block(self, tmp_path):
        """开关开但模型加载失败：静默禁用，不抛、不阻塞转发。"""
        class _BoomEngine:
            def __init__(self, *a, **k):
                pass

            async def ensure_ready(self):
                return False

            async def check_duplicate(self, text, count, db=None):
                return False

            async def add(self, text, db=None):
                pass

        cfg = _sem_cfg(semantic=True)
        fwd, db = await _make_fwd(tmp_path, cfg, engine=_BoomEngine())
        sent = []

        async def stub_send(original, text, target_id, topic_id, snap):
            sent.append(text)

        fwd._send_message = stub_send
        await fwd.process_message(_Msg(1, text="hello"))
        assert sent == ["hello"]  # 降级后照常转发
        await db.close()


# ---------------------------------------------------------------------------
# Forwarder 集成：开启且模型可用 → 语义重复被拦
# ---------------------------------------------------------------------------


class _FixedEmbedEngine:
    """确定性 mock 引擎（对齐 SemanticDedup 外观签名）：text→两聚类向量。"""

    def __init__(self):
        self.enabled = True
        self.checked = 0

    async def ensure_ready(self):
        return True

    async def check_duplicate(self, text, count, db=None):
        self.checked += 1
        vec = [1.0, 0.0] if "青蛙" in text else [0.0, 1.0]
        return self._dup_for(vec)

    async def add(self, text, db=None):
        pass

    _seen = set()

    def _dup_for(self, vec):
        key = tuple(vec)
        if key in self._seen:
            return True
        self._seen.add(key)
        return False


class TestSemanticDedupEnabled:
    async def test_duplicate_semantic_content_blocked(self, tmp_path):
        """开启且模型可用：语义相同（近似文本）第二条被拦。"""
        engine = _FixedEmbedEngine()
        cfg = _sem_cfg(semantic=True)
        fwd, db = await _make_fwd(tmp_path, cfg, engine=engine)
        sent = []

        async def stub_send(original, text, target_id, topic_id, snap):
            sent.append(text)

        fwd._send_message = stub_send
        await fwd.process_message(_Msg(1, text="今天天气很好，适合出门散步看青蛙"))
        await fwd.process_message(_Msg(2, text="今天天气很好，适合出门散步看青蛙（重复）"))
        await fwd.process_message(_Msg(3, text="股市大涨，科技股领涨"))
        assert sent == ["今天天气很好，适合出门散步看青蛙", "股市大涨，科技股领涨"]
        await db.close()

    async def test_disabled_does_not_invoke_engine(self, tmp_path):
        """关闭态不调用引擎（零语义检查的端到端断言）。"""
        engine = _FixedEmbedEngine()
        cfg = _sem_cfg(semantic=False)
        fwd, db = await _make_fwd(tmp_path, cfg, engine=engine)
        sent = []

        async def stub_send(original, text, target_id, topic_id, snap):
            sent.append(text)

        fwd._send_message = stub_send
        await fwd.process_message(_Msg(1, text="A"))
        await fwd.process_message(_Msg(2, text="B"))
        assert sent == ["A", "B"]
        assert engine.checked == 0  # 关键：没有一次语义检查
        await db.close()