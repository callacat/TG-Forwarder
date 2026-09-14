# -*- coding: utf-8 -*-
"""sqlite 封装 + schema 版本迁移框架（P8/R3）。

设计要点：
- v2 现网库（user_version=0 且已有表）直接挂载 → 自动迁移到 v3 schema；
- 全新空库 → 直接建 v3 最新 schema 并把 user_version 设为 CURRENT_SCHEMA_VERSION；
- 迁移保持既有表结构/数据不变（列不删不改名），只做增量（新表 + 索引），
  迁移前后行数对账（progress/dedup/rules/sources），不一致抛 RuntimeError 回滚。
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

from loguru import logger

# 当前 schema 版本：2（v2 现网库视为版本 1 语义 —— 表已存在但 user_version=0）
CURRENT_SCHEMA_VERSION = 2

# 迁移对账表（v2 基线表，缺表视为 0 行）
_AUDIT_TABLES = ("forward_progress", "dedup_hashes", "rules", "sources")


class Database:
    """aiosqlite 连接封装（WAL），单实例部署（单进程 asyncio）足够。"""

    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    # --- 生命周期 ---

    async def open(self) -> None:
        """建立连接并启用 WAL。幂等。"""
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        logger.info(f"✅ 数据库连接已建立: {self.path}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise ConnectionError("数据库未初始化。请先调用 open()。")
        return self._conn

    # --- schema 迁移（R3）---

    async def _table_exists(self, name: str) -> bool:
        cur = await self._require_conn().execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return await cur.fetchone() is not None

    async def _table_count(self, name: str) -> int:
        if not await self._table_exists(name):
            return 0
        cur = await self._require_conn().execute(f"SELECT COUNT(*) FROM {name}")
        row = await cur.fetchone()
        return row[0] if row else 0

    async def _user_version(self) -> int:
        cur = await self._require_conn().execute("PRAGMA user_version")
        row = await cur.fetchone()
        return row[0] if row else 0

    async def _set_user_version(self, version: int) -> None:
        await self._require_conn().execute(f"PRAGMA user_version = {int(version)}")

    async def migrate(self) -> None:
        """把库迁移到 CURRENT_SCHEMA_VERSION。

        - user_version == CURRENT：无需迁移；
        - user_version < CURRENT 且 v2 表已存在（含 user_version=0 的现网库）：
          逐版本迁移（当前只有 v2→v3 一级，即 1→2 / 0(有表)→2）；
        - 全新空库（无任何表）：直接建 v3 schema。
        """
        async with self._lock:
            conn = self._require_conn()
            version = await self._user_version()
            has_v2_tables = await self._table_exists("dedup_hashes")

            if version == CURRENT_SCHEMA_VERSION:
                return

            if not has_v2_tables and version == 0:
                # 全新空库：直接建 v3 schema
                await self._create_v3_schema()
                await self._set_user_version(CURRENT_SCHEMA_VERSION)
                await conn.commit()
                logger.info(f"✅ 全新库已初始化到 v{CURRENT_SCHEMA_VERSION} schema")
                return

            # v2 现网库（user_version 0 或 1）→ 迁移到 2
            logger.info(f"检测到 v2 库 (user_version={version})，开始迁移 v2→v3 schema...")
            before = {t: await self._table_count(t) for t in _AUDIT_TABLES}
            logger.info(
                f"迁移前行数对账: " + ", ".join(f"{t}={n}" for t, n in before.items())
            )

            try:
                # v2→v3：增量（不动既有表结构与数据）
                await self._migrate_2_forward()
            except Exception:
                await conn.rollback()
                raise

            after = {t: await self._table_count(t) for t in _AUDIT_TABLES}
            mismatched = {t for t in _AUDIT_TABLES if before[t] != after[t]}
            if mismatched:
                await conn.rollback()
                raise RuntimeError(
                    f"迁移对账失败（行数不一致）: "
                    + ", ".join(f"{t} {before[t]}→{after[t]}" for t in sorted(mismatched))
                )

            await self._set_user_version(CURRENT_SCHEMA_VERSION)
            await conn.commit()
            logger.info(
                f"✅ 迁移 v2→v3 完成，对账通过: "
                + ", ".join(f"{t}={n}" for t, n in after.items())
            )

    async def _migrate_2_forward(self) -> None:
        """版本 2 的迁移内容：schema_meta 表 + dedup 时间索引。数据零改动。"""
        conn = self._require_conn()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
              key TEXT PRIMARY KEY,
              value TEXT
            )
            """
        )
        await conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("migrated_at", datetime.now(timezone.utc).isoformat()),
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dedup_ts ON dedup_hashes (timestamp)"
        )

    async def _create_v3_schema(self) -> None:
        """全新库：建 v3 全量 schema（与 v2 表结构一致 + 增量）。"""
        conn = self._require_conn()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dedup_hashes (
              hash TEXT PRIMARY KEY,
              timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forward_progress (
              channel_id INTEGER PRIMARY KEY,
              message_id INTEGER
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS link_checker (
              url TEXT PRIMARY KEY,
              message_id INTEGER,
              status TEXT,
              last_checked DATETIME
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sources (
              identifier TEXT PRIMARY KEY,
              check_replies BOOLEAN DEFAULT 0,
              replies_limit INTEGER DEFAULT 5,
              forward_new_only BOOLEAN,
              resolved_id INTEGER,
              cached_title TEXT
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rules (
              name TEXT PRIMARY KEY,
              target_identifier TEXT,
              topic_id INTEGER,
              all_keywords TEXT,
              any_keywords TEXT,
              file_types TEXT,
              file_name_patterns TEXT
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_config (
              key TEXT PRIMARY KEY,
              value TEXT
            )
            """
        )
        await self._migrate_2_forward()

    # --- 基础操作（签名与 v2 database.py 对齐，实例方法，无全局变量）---

    async def check_hash(self, hash_str: str) -> bool:
        try:
            cur = await self._require_conn().execute(
                "SELECT 1 FROM dedup_hashes WHERE hash = ?", (hash_str,)
            )
            return await cur.fetchone() is not None
        except Exception as e:
            logger.error(f"check_hash 失败: {e}")
            return True  # 出错时按已见处理，避免重复转发

    async def add_hash(self, hash_str: str) -> None:
        try:
            await self._require_conn().execute(
                "INSERT OR REPLACE INTO dedup_hashes (hash, timestamp) VALUES (?, ?)",
                (hash_str, datetime.now(timezone.utc)),
            )
            await self._require_conn().commit()
        except Exception as e:
            logger.error(f"add_hash 失败: {e}")

    async def prune_old_hashes(self, days: int = 30) -> int:
        """TTL 清理（P8 修复：dedup 只增问题）。返回删除行数。"""
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
            cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
            cur = await self._require_conn().execute(
                "DELETE FROM dedup_hashes WHERE timestamp < ?", (cutoff_iso,)
            )
            await self._require_conn().commit()
            return cur.rowcount or 0
        except Exception as e:
            logger.error(f"prune_old_hashes 失败: {e}")
            return 0

    async def get_progress(self, channel_id: int) -> int:
        try:
            cur = await self._require_conn().execute(
                "SELECT message_id FROM forward_progress WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cur.fetchone()
            return row[0] if row else 0
        except Exception as e:
            logger.error(f"get_progress 失败: {e}")
            return 0

    async def set_progress(self, channel_id: int, message_id: int) -> None:
        try:
            await self._require_conn().execute(
                "INSERT OR REPLACE INTO forward_progress (channel_id, message_id) "
                "VALUES (?, ?)",
                (channel_id, message_id),
            )
            await self._require_conn().commit()
        except Exception as e:
            logger.error(f"set_progress 失败: {e}")

    async def get_db_stats(self) -> Dict[str, int]:
        """与 v2 对齐：{"dedup_hashes": n, "invalid_links": n}。"""
        try:
            conn = self._require_conn()
            cur = await conn.execute("SELECT COUNT(*) FROM dedup_hashes")
            dedup = (await cur.fetchone())[0]
            cur = await conn.execute(
                "SELECT COUNT(*) FROM link_checker WHERE status = 'invalid'"
            )
            invalid = (await cur.fetchone())[0]
            return {"dedup_hashes": dedup, "invalid_links": invalid}
        except Exception as e:
            logger.error(f"get_db_stats 失败: {e}")
            return {"dedup_hashes": 0, "invalid_links": 0}

    # --- Link Checker（_meta_ 行约定保留，与 v2 对齐）---

    async def get_link_checker_progress(self) -> int:
        try:
            cur = await self._require_conn().execute(
                "SELECT message_id FROM link_checker WHERE url = '_meta_'"
            )
            row = await cur.fetchone()
            return row[0] if row else 0
        except Exception as e:
            logger.error(f"get_link_checker_progress 失败: {e}")
            return 0

    async def set_link_checker_progress(self, mid: int) -> None:
        try:
            await self._require_conn().execute(
                "INSERT OR REPLACE INTO link_checker (url, message_id, status) "
                "VALUES (?, ?, ?)",
                ("_meta_", mid, "progress"),
            )
            await self._require_conn().commit()
        except Exception as e:
            logger.error(f"set_link_checker_progress 失败: {e}")

    async def add_pending_link(self, url: str, mid: int) -> None:
        try:
            await self._require_conn().execute(
                "INSERT OR IGNORE INTO link_checker (url, message_id, status) "
                "VALUES (?, ?, ?)",
                (url, mid, "pending"),
            )
            await self._require_conn().commit()
        except Exception as e:
            logger.error(f"add_pending_link 失败: {e}")

    async def get_links_to_check(self) -> List[Any]:
        try:
            cur = await self._require_conn().execute(
                "SELECT url, message_id FROM link_checker "
                "WHERE status != 'valid' AND url != '_meta_'"
            )
            return await cur.fetchall()
        except Exception as e:
            logger.error(f"get_links_to_check 失败: {e}")
            return []

    async def update_link_status(self, url: str, status: str) -> None:
        try:
            await self._require_conn().execute(
                "UPDATE link_checker SET status = ?, last_checked = ? WHERE url = ?",
                (status, datetime.now(timezone.utc), url),
            )
            await self._require_conn().commit()
        except Exception as e:
            logger.error(f"update_link_status 失败: {e}")

    async def execute(self, sql: str, params: tuple = ()) -> None:
        """透传执行（仓储层建表等内部用途）。"""
        await self._require_conn().execute(sql, params)
        await self._require_conn().commit()
