# -*- coding: utf-8 -*-
"""测试辅助：真实 tmp sqlite + 符合接口约定的最小仓储实现。

这些测试辅助类模拟并行子代理交付的 tg_forwarder/storage 接口约定：
- Database: get_db_stats() 等异步方法
- ConfigRepository/SourceRepository/RuleRepository: get/save/remove，dict 进出

实现说明：用同步 sqlite3 + asyncio.to_thread 而非 aiosqlite——
aiosqlite 连接绑定创建时的事件循环，跨 loop（TestClient portal loop 与
验证用 loop）调用会导致 future 永不解析而挂起；to_thread 对 loop 无关，
同一个真实 sqlite 文件在多个事件循环下均可安全读写。

若 tg_forwarder.storage 已就绪，应优先使用真实实现；
这里保证 web/bot 层测试在 storage/core 未交付时也能跑通。
"""
import asyncio
import json
import sqlite3

# storage 子代理是否已交付（import 探测）
try:
    from tg_forwarder.storage.db import Database  # noqa: F401
    from tg_forwarder.storage.repositories import (  # noqa: F401
        ConfigRepository,
        RuleRepository,
        SourceRepository,
    )

    STORAGE_READY = True
except ImportError:
    STORAGE_READY = False


# ---------------------------------------------------------------------------
# 最小 Database 实现（表结构与 v2 database.py 一致）
# ---------------------------------------------------------------------------

class MiniDatabase:
    """真实 sqlite（tmp 文件）+ v2 表结构的最小 Database 实现（loop 无关）。"""

    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None

    async def connect(self):
        """初始化连接与表结构（幂等，可多次调用）。"""
        if self._conn is None:
            self._conn = await asyncio.to_thread(
                sqlite3.connect, self.path, check_same_thread=False
            )
        await asyncio.to_thread(self._create_tables)
        return self

    async def _ensure(self):
        """惰性连接：首次操作时自动初始化（避免跨 loop 绑定问题）。"""
        if self._conn is None:
            await self.connect()
        return self._conn

    def _create_tables(self):
        c = self._conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS dedup_hashes (
              hash TEXT PRIMARY KEY,
              timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS link_checker (
              url TEXT PRIMARY KEY,
              message_id INTEGER,
              status TEXT,
              last_checked DATETIME
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS sources (
              identifier TEXT PRIMARY KEY,
              check_replies BOOLEAN DEFAULT 0,
              replies_limit INTEGER DEFAULT 5,
              forward_new_only BOOLEAN,
              resolved_id INTEGER,
              cached_title TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS rules (
              name TEXT PRIMARY KEY,
              target_identifier TEXT,
              topic_id INTEGER,
              all_keywords TEXT,
              any_keywords TEXT,
              file_types TEXT,
              file_name_patterns TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
              key TEXT PRIMARY KEY,
              value TEXT
            )
        """)
        c.commit()

    async def get_db_stats(self) -> dict:
        await self._ensure()
        c = self._conn

        def _q():
            dedup = c.execute("SELECT COUNT(*) FROM dedup_hashes").fetchone()[0]
            invalid = c.execute(
                "SELECT COUNT(*) FROM link_checker WHERE status = 'invalid'"
            ).fetchone()[0]
            return {"dedup_hashes": dedup, "invalid_links": invalid}

        return await asyncio.to_thread(_q)

    async def get_config_json(self, key: str):
        await self._ensure()
        c = self._conn
        row = await asyncio.to_thread(
            c.execute, "SELECT value FROM app_config WHERE key = ?", (key,)
        )
        fetched = await asyncio.to_thread(row.fetchone)
        return fetched[0] if fetched else None

    async def close(self):
        if self._conn:
            await asyncio.to_thread(self._conn.close)
            self._conn = None


# ---------------------------------------------------------------------------
# 最小仓储实现（dict 进出，v2 语义：sources/rules 表 + app_config 表）
# ---------------------------------------------------------------------------

class MiniConfigRepository:
    """app_config 表仓储：get(key)/save(key, data)/remove(key)。"""

    def __init__(self, db: MiniDatabase):
        self.db = db

    async def get(self, key: str):
        raw = await self.db.get_config_json(key)
        return json.loads(raw) if raw else None

    async def save(self, key: str, data: dict):
        conn = await self.db._ensure()

        def _s():
            conn.execute(
                "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
                (key, json.dumps(data, ensure_ascii=False)),
            )
            conn.commit()

        await asyncio.to_thread(_s)

    async def remove(self, key: str):
        conn = await self.db._ensure()

        def _r():
            conn.execute("DELETE FROM app_config WHERE key = ?", (key,))
            conn.commit()

        await asyncio.to_thread(_r)


class MiniSourceRepository:
    """sources 表仓储：get_all()/save(data)/remove(identifier)。"""

    def __init__(self, db: MiniDatabase):
        self.db = db

    async def get_all(self):
        conn = await self.db._ensure()
        conn.row_factory = sqlite3.Row

        def _q():
            rows = conn.execute("SELECT * FROM sources").fetchall()
            return [dict(r) for r in rows]

        return await asyncio.to_thread(_q)

    async def save(self, data: dict):
        conn = await self.db._ensure()

        def _s():
            conn.execute(
                """
                INSERT OR REPLACE INTO sources
                (identifier, check_replies, replies_limit, forward_new_only, resolved_id, cached_title)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(data.get("identifier")),
                    data.get("check_replies", False),
                    data.get("replies_limit", 5),
                    data.get("forward_new_only"),
                    data.get("resolved_id"),
                    data.get("cached_title"),
                ),
            )
            conn.commit()

        await asyncio.to_thread(_s)

    async def remove(self, identifier: str):
        conn = await self.db._ensure()

        def _r():
            conn.execute("DELETE FROM sources WHERE identifier = ?", (str(identifier),))
            conn.commit()

        await asyncio.to_thread(_r)


class MiniRuleRepository:
    """rules 表仓储：get_all()/save(data)/remove(name)/clear()。"""

    def __init__(self, db: MiniDatabase):
        self.db = db

    async def get_all(self):
        conn = await self.db._ensure()
        conn.row_factory = sqlite3.Row

        def _q():
            rows = conn.execute("SELECT * FROM rules").fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["all_keywords"] = json.loads(d["all_keywords"] or "[]")
                d["any_keywords"] = json.loads(d["any_keywords"] or "[]")
                d["file_types"] = json.loads(d["file_types"] or "[]")
                d["file_name_patterns"] = json.loads(d["file_name_patterns"] or "[]")
                result.append(d)
            return result

        return await asyncio.to_thread(_q)

    async def save(self, data: dict):
        conn = await self.db._ensure()

        def _s():
            conn.execute(
                """
                INSERT OR REPLACE INTO rules
                (name, target_identifier, topic_id, all_keywords, any_keywords, file_types, file_name_patterns)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name"),
                    str(data.get("target_identifier")),
                    data.get("topic_id"),
                    json.dumps(data.get("all_keywords", []), ensure_ascii=False),
                    json.dumps(data.get("any_keywords", []), ensure_ascii=False),
                    json.dumps(data.get("file_types", []), ensure_ascii=False),
                    json.dumps(data.get("file_name_patterns", []), ensure_ascii=False),
                ),
            )
            conn.commit()

        await asyncio.to_thread(_s)

    async def remove(self, name: str):
        conn = await self.db._ensure()

        def _r():
            conn.execute("DELETE FROM rules WHERE name = ?", (name,))
            conn.commit()

        await asyncio.to_thread(_r)

    async def clear(self):
        conn = await self.db._ensure()

        def _r():
            conn.execute("DELETE FROM rules")
            conn.commit()

        await asyncio.to_thread(_r)
