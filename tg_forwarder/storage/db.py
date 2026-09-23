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

# 当前 schema 版本：6（v2 现网库=1；v3 初版=2；F2 message_map=3；F4 age_cutoff=4；
# F5/F6 header+media=5；M4 F10 sources.digest_enabled=6）
CURRENT_SCHEMA_VERSION = 6

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
                # 全新空库：直接建最新 schema
                await self._create_v3_schema()
                await self._set_user_version(CURRENT_SCHEMA_VERSION)
                await conn.commit()
                logger.info(f"✅ 全新库已初始化到 v{CURRENT_SCHEMA_VERSION} schema")
                return

            # 逐版本前向迁移：v2 现网库(0/1)→2→3
            logger.info(
                f"检测到旧库 (user_version={version})，"
                f"开始迁移 v{version}→v{CURRENT_SCHEMA_VERSION} schema..."
            )
            before = {t: await self._table_count(t) for t in _AUDIT_TABLES}
            logger.info(
                f"迁移前行数对账: " + ", ".join(f"{t}={n}" for t, n in before.items())
            )

            try:
                # 增量逐版本（不动既有表结构与数据）
                await self._migrate_2_forward()
                await self._migrate_3_forward()
                await self._migrate_4_forward()
                await self._migrate_5_forward()
                await self._migrate_6_forward()
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
                f"✅ 迁移 v{version}→v{CURRENT_SCHEMA_VERSION} 完成，对账通过: "
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

    async def _migrate_3_forward(self) -> None:
        """版本 3 的迁移内容（F2）：
        - message_map 表（src→dest 消息映射，编辑/删除级联用）；
        - sources 表补 sync_edits/sync_deletes 列（F2 per 源开关）。
        历史消息无映射（级联只覆盖开启后的新转发）。"""
        conn = self._require_conn()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_map (
              src_channel_id INTEGER NOT NULL,
              src_message_id INTEGER NOT NULL,
              dest_channel_id INTEGER NOT NULL,
              dest_message_id INTEGER NOT NULL,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (src_channel_id, src_message_id, dest_channel_id)
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_map_dest "
            "ON message_map (dest_channel_id, dest_message_id)"
        )
        # v2 现网库的 sources 是旧表（缺 F2 新列）：防护性补列（幂等）。
        # 全新库 _create_v3_schema 已含这两列，ALTER 前查 PRAGMA 避免重复列错。
        cur = await conn.execute("PRAGMA table_info(sources)")
        existing = {row[1] for row in await cur.fetchall()}
        for col, ddl in (
            ("sync_edits", "INTEGER DEFAULT 0"),
            ("sync_deletes", "INTEGER DEFAULT 0"),
        ):
            if col not in existing:
                await conn.execute(f"ALTER TABLE sources ADD COLUMN {col} {ddl}")

    async def _migrate_4_forward(self) -> None:
        """版本 4 的迁移内容（F4 年龄截断）：sources 表补 age_cutoff_hours 列。

        幂等：PRAGMA 检查后 ALTER（旧库可能已是 v3/fresh）。
        """
        conn = self._require_conn()
        cur = await conn.execute("PRAGMA table_info(sources)")
        existing = {row[1] for row in await cur.fetchall()}
        if "age_cutoff_hours" not in existing:
            await conn.execute("ALTER TABLE sources ADD COLUMN age_cutoff_hours REAL")

    async def _migrate_5_forward(self) -> None:
        """版本 5 的迁移内容（F5/F6）：
        - sources 表补 header_template 列（F6 源标注模板，默认 NULL=不标注）；
        - rules 表补 media_types/max_file_size 列（F5 媒体类型/大小过滤，
          media_types TEXT 逗号分隔、max_file_size INTEGER 默认 0=不限制）。

        幂等：PRAGMA 检查后 ALTER 补列，缺啥补啥。
        """
        conn = self._require_conn()
        cur = await conn.execute("PRAGMA table_info(sources)")
        src_cols = {row[1] for row in await cur.fetchall()}
        if "header_template" not in src_cols:
            await conn.execute("ALTER TABLE sources ADD COLUMN header_template TEXT")

        rule_cols = {}
        try:
            rcur = await conn.execute("PRAGMA table_info(rules)")
            rule_cols = {row[1] for row in await rcur.fetchall()}
        except Exception:
            rule_cols = {}  # 旧库可能无 rules 表（bootstrap 前），CREATE 时已含
        if rule_cols:
            if "media_types" not in rule_cols:
                await conn.execute("ALTER TABLE rules ADD COLUMN media_types TEXT")
            if "max_file_size" not in rule_cols:
                await conn.execute("ALTER TABLE rules ADD COLUMN max_file_size INTEGER DEFAULT 0")

    async def _migrate_6_forward(self) -> None:
        """版本 6 的迁移内容（M4 F10）：sources 表补 digest_enabled 列（per 源 AI 摘要开关）。

        幂等：PRAGMA 检查后 ALTER 补列。
        """
        conn = self._require_conn()
        cur = await conn.execute("PRAGMA table_info(sources)")
        existing = {row[1] for row in await cur.fetchall()}
        if "digest_enabled" not in existing:
            await conn.execute("ALTER TABLE sources ADD COLUMN digest_enabled INTEGER DEFAULT 0")

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
              cached_title TEXT,
              sync_edits BOOLEAN DEFAULT 0,
              sync_deletes BOOLEAN DEFAULT 0,
              age_cutoff_hours REAL,
              header_template TEXT,
              digest_enabled INTEGER DEFAULT 0
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
              file_name_patterns TEXT,
              media_types TEXT,
              max_file_size INTEGER DEFAULT 0
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
        await self._migrate_3_forward()
        await self._migrate_4_forward()
        await self._migrate_5_forward()
        await self._migrate_6_forward()

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

    # --- Message Map（F2 编辑/删除级联：src→dest 映射）---

    async def add_message_map(
        self, src_channel_id: int, src_message_id: int,
        dest_channel_id: int, dest_message_id: int,
    ) -> None:
        """登记一条转发映射（发送成功后调用）。幂等（INSERT OR REPLACE）。"""
        try:
            await self._require_conn().execute(
                "INSERT OR REPLACE INTO message_map "
                "(src_channel_id, src_message_id, dest_channel_id, dest_message_id) "
                "VALUES (?, ?, ?, ?)",
                (src_channel_id, src_message_id, dest_channel_id, dest_message_id),
            )
            await self._require_conn().commit()
        except Exception as e:
            logger.error(f"add_message_map 失败: {e}")

    async def get_message_map_by_src(
        self, src_channel_id: int, src_message_id: int
    ) -> list:
        """按源消息查全部镜像（编辑级联）。返回 [(dest_channel_id, dest_message_id)]。"""
        try:
            cur = await self._require_conn().execute(
                "SELECT dest_channel_id, dest_message_id FROM message_map "
                "WHERE src_channel_id = ? AND src_message_id = ?",
                (src_channel_id, src_message_id),
            )
            return [tuple(r) for r in await cur.fetchall()]
        except Exception as e:
            logger.error(f"get_message_map_by_src 失败: {e}")
            return []

    async def delete_message_map_by_src(
        self, src_channel_id: int, src_message_id: int
    ) -> None:
        """源消息删除后清映射（删除级联后调用）。"""
        try:
            await self._require_conn().execute(
                "DELETE FROM message_map WHERE src_channel_id = ? AND src_message_id = ?",
                (src_channel_id, src_message_id),
            )
            await self._require_conn().commit()
        except Exception as e:
            logger.error(f"delete_message_map_by_src 失败: {e}")

    async def set_progress(self, channel_id: int, message_id: int) -> None:
        try:
            # 单调不倒退：空洞对账消息 id < progress，finally 无条件写入不得回拉水位
            cur = await self._require_conn().execute(
                "SELECT message_id FROM forward_progress WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cur.fetchone()
            if row is not None and int(row[0]) >= message_id:
                return
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
