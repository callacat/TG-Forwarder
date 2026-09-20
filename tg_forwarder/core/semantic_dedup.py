# -*- coding: utf-8 -*-
"""F12 语义去重（默认关；模型缺失/加载失败优雅降级为不启用）。

设计契约（对齐 tests/test_semantic_dedup.py）：
- 惰性加载：fastembed 只在首次真正需要时才 import + 拉模型；
  import/构造失败 → enabled=False，绝不抛异常、绝不阻塞转发主线。
- 向量指纹复用 dedup_hashes 表（"sem:<sha256>"），不建新表、无 schema 变更
  （db._migrate_6_forward 已在 4ff1b05 修复并入迁移链，无需绕开）。
- 相似判定：与"最近窗口"内既有向量做余弦相似度，>= 阈值视为重复。
  窗口为内存 deque（上限 _WINDOW_SIZE），随消息数自然滚动；
  跨重启去重依赖 dedup_hashes 里已落的 sem: 指纹（精确命中）。
- embed_fn 依赖注入便于单测（生产默认走 fastembed.TextEmbedding）。
"""
import asyncio
import hashlib
import math
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Sequence

from loguru import logger

# 默认 embedding 模型（fastembed 支持的中文小模型，~90MB 级别）
DEFAULT_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

# 语义相似度阈值：>= 视为重复（1.0 完全相同；bge 同类文本通常 >0.75）
SIMILARITY_THRESHOLD = 0.85

# 内存窗口：保留最近 N 条消息的向量用于近似比对（防同义改写的逃逸）。
WINDOW_SIZE = 64


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度；零向量/维度不一致 → 0.0（保守：不判重）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def embedding_fingerprint(vector: Sequence[float]) -> str:
    """向量指纹（存 dedup_hashes）：低位量化 + sha256，稳定且省空间。"""
    quantized = []
    for v in vector:
        # 保持 ± 符号与 ~3 位有效精度，抑制浮点抖动
        quantized.append(f"{v:+.3e}" if abs(v) >= 1e-4 else "0")
    raw = ",".join(quantized)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sem:{digest[:24]}"


class SemanticDedupEngine:
    """语义去重引擎：封装模型加载 + 最近窗口相似度比对。

    用法（Forwarder 持有实例）：
        engine = SemanticDedupEngine()          # 惰性，不触发模型
        if await engine.ensure_ready():         # 首次触发加载
            dup = await engine.check_duplicate(text, count_marker)
    """

    def __init__(
        self,
        embed_fn: Optional[Callable[[Sequence[str]], Sequence[List[float]]]] = None,
        model_name: str = DEFAULT_MODEL_NAME,
        cache_dir: Optional[str] = None,
        threshold: float = SIMILARITY_THRESHOLD,
    ) -> None:
        self._embed_fn = embed_fn
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.threshold = threshold
        self.enabled = False
        self._load_error: Optional[str] = None
        self._recent: Deque[List[float]] = deque(maxlen=WINDOW_SIZE)
        # 并发预热/消息触发的就绪互斥（首条消息在预热加载未完成时会等待而非重复加载）
        self._ready_lock: Optional[asyncio.Lock] = None

    async def _embed_async(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        """线程内执行 embed（不阻塞事件循环）；未加载/异常 → None。"""
        if not self.enabled or self._embed_fn is None:
            return None
        try:
            return await asyncio.to_thread(self._embed_fn, list(texts))
        except Exception as e:  # noqa: BLE001 —— 运行期异常也降级放行
            logger.warning(f"F12 embed 异常（放行）: {e}")
            return None

    async def ensure_ready(self) -> bool:
        """加载模型（幂等，线程内执行不阻塞事件循环）。失败 → enabled=False + 记录原因，不抛。

        首次启用时的模型构造/下载（fastembed，~90MB）在 executor 线程完成，
        避免装配后首条消息在事件循环内同步加载阻塞转发主循环。
        并发调用（启动预热 + 首条消息）经 _ready_lock 互斥，只加载一次。
        """
        if self.enabled:
            return True
        if self._load_error is not None:
            return False
        if self._ready_lock is None:
            self._ready_lock = asyncio.Lock()
        async with self._ready_lock:
            if self.enabled:
                return True
            if self._load_error is not None:
                return False
            try:
                if self._embed_fn is None:
                    # 惰性 import fastembed；缺包时 ImportError → 静默降级
                    from fastembed import TextEmbedding

                    def _init() -> Callable[[Sequence[str]], List[List[float]]]:
                        kwargs = {}
                        if self.cache_dir:
                            kwargs["cache_dir"] = self.cache_dir
                        model = TextEmbedding(self.model_name, **kwargs)

                        def _embed(texts: Sequence[str]) -> List[List[float]]:
                            return [list(v) for v in model.embed(list(texts))]

                        return _embed

                    self._embed_fn = await asyncio.to_thread(_init)
                # 探活：真正调用一次（若模型文件缺失会在 embed 时暴露）
                await asyncio.to_thread(self._embed_fn, ["探活"])
                self.enabled = True
                logger.info(
                    f"F12 语义去重已启用（model={self.model_name}）"
                )
            except Exception as e:  # noqa: BLE001 —— 任何加载失败都降级
                self._load_error = f"{type(e).__name__}: {e}"
                self.enabled = False
                logger.warning(
                    f"F12 语义去重不可用，已降级为不启用（不影响既有 dedup）: {self._load_error}"
                )
        return self.enabled

    async def check_duplicate(self, text: str, marker: object) -> bool:
        """判定 text 是否与窗口内既有内容语义重复。

        仅在 enabled 时有效；未启用 → False（降级放行）。
        marker 仅用于日志/回调标识，不参与判定。
        """
        if not self.enabled or not text:
            return False
        try:
            emb = await self._embed_async([text])
            if not emb:
                return False
            return self.check_window(list(emb[0]))
        except Exception as e:  # noqa: BLE001 —— 运行期异常也降级放行
            logger.warning(f"F12 语义比对异常（放行）: {e}")
            return False

    def check_window(self, vec: List[float]) -> bool:
        """窗口内相似比对（复用调用方已算向量，避免二次 embed）。

        命中 → True；未命中 → 追加进窗口（随消息数自然滚动）。
        """
        for prev in self._recent:
            if prev and cosine_similarity(vec, prev) >= self.threshold:
                return True
        self._recent.append(vec)
        return False


class SemanticDedup:
    """Forwarder 侧的语义去重外观：始终安全，失败静默禁用。

    提供与现有 dedup 相同的「check / add」语义，但全部可选。
    未启用/加载失败 → check 恒 False、add 恒 no-op。
    生产装配点：main.py 注入本外观（内部含 SemanticDedupEngine）。
    """

    def __init__(self, engine: Optional[SemanticDedupEngine] = None):
        self.engine = engine or SemanticDedupEngine()
        # 最近 check 阶段已算向量按消息 id 暂存，add 复用避免同消息二次 embed
        self._vec_cache: Dict[object, List[float]] = {}

    @property
    def enabled(self) -> bool:
        """镜像引擎状态（forwarder 读取；未启用即 False）。"""
        return self.engine.enabled

    async def ensure_ready(self) -> bool:
        return await self.engine.ensure_ready()

    async def check_duplicate(self, text: str, marker: object, db) -> bool:
        """check：先查 dedup_hashes 精确指纹，再查窗口相似（复用单次 embed）。"""
        if not self.engine.enabled:
            return False
        try:
            emb = await self._embed_one(text)
            if emb is None:
                return False
            fp = embedding_fingerprint(emb)
            if await db.check_hash(fp):
                return True
            if len(self._vec_cache) >= 256:
                self._vec_cache.clear()
            self._vec_cache[marker] = emb
            # 窗口查询：复用已算向量，不再二次 embed（Codex minor #2）
            return self.engine.check_window(emb)
        except Exception:
            return False

    async def add(self, text: str, marker: object, db) -> None:
        """add：登记向量指纹（复用 check 阶段已算向量；未 check 过则重算）。

        走现有 dedup_hashes 表，无 schema 变更。
        """
        if not self.engine.enabled or not text:
            return
        try:
            emb = self._vec_cache.pop(marker, None)
            if emb is None:
                emb = await self._embed_one(text)
            if emb is None:
                return
            fp = embedding_fingerprint(emb)
            await db.add_hash(fp)
        except Exception:
            pass

    async def _embed_one(self, text: str):
        if not self.engine.enabled:
            return None
        # 补 await：缺省会让 ensure_ready 协程从未执行（Codex minor #2）
        await self.engine.ensure_ready()
        if not self.engine.enabled:
            return None
        try:
            emb = await self.engine._embed_async([text])
            return list(emb[0]) if emb else None
        except Exception:
            return None