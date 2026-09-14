# -*- coding: utf-8 -*-
"""storage 层测试（R3 迁移对账 + R4 单源 + 仓储序列化）。

R3 对账基准（现网 v2 data/ 快照）：forward_progress 15 源、dedup_hashes 13727、
rules 9、sources 3（测试按同规模构造，校验迁移前后行数零变化）。
"""
import asyncio
import os
import sqlite3

import pytest

from tg_forwarder.storage.db import CURRENT_SCHEMA_VERSION, Database
from tg_forwarder.storage.repositories import (
    ConfigRepository,
    RuleRepository,
    SourceRepository,
)


def _build_v2_db(path: str) -> None:
    """手工构造 v2 现网库（表结构对齐 v2 database.py，无 user_version）。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE dedup_hashes (
          hash TEXT PRIMARY KEY,
          timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE forward_progress (
          channel_id INTEGER PRIMARY KEY,
          message_id INTEGER
        );
        CREATE TABLE link_checker (
          url TEXT PRIMARY KEY,
          message_id INTEGER,
          status TEXT,
          last_checked DATETIME
        );
        CREATE TABLE sources (
          identifier TEXT PRIMARY KEY,
          check_replies BOOLEAN DEFAULT 0,
          replies_limit INTEGER DEFAULT 5,
          forward_new_only BOOLEAN,
          resolved_id INTEGER,
          cached_title TEXT
        );
        CREATE TABLE rules (
          name TEXT PRIMARY KEY,
          target_identifier TEXT,
          topic_id INTEGER,
          all_keywords TEXT,
          any_keywords TEXT,
          file_types TEXT,
          file_name_patterns TEXT
        );
        CREATE TABLE app_config (
          key TEXT PRIMARY KEY,
          value TEXT
        );
        """
    )
    # 对账基线：progress 15 / dedup 13727 / rules 9 / sources 3 / app_config 5
    conn.executemany(
        "INSERT INTO forward_progress (channel_id, message_id) VALUES (?, ?)",
        [(-1009000000 + i, 100 + i) for i in range(15)],
    )
    conn.executemany(
        "INSERT INTO dedup_hashes (hash, timestamp) VALUES (?, datetime('now'))",
        [(f"h{i}",) for i in range(13727)],
    )
    conn.executemany(
        "INSERT INTO rules VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (f"rule{i}", "-100999", None, "[]", '["kw"]', "[]", "[]")
            for i in range(9)
        ],
    )
    conn.executemany(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)",
        [(f"-100123{i}", 0, 5, None, None, f"频道{i}") for i in range(3)],
    )
    for key in ("system_settings", "ad_filter", "whitelist", "content_filter", "replacements"):
        conn.execute("INSERT INTO app_config VALUES (?, ?)", (key, "{}"))
    conn.commit()
    conn.close()


def _counts(path: str) -> dict:
    conn = sqlite3.connect(path)
    out = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("forward_progress", "dedup_hashes", "rules", "sources", "app_config")
    }
    conn.close()
    return out


@pytest.fixture
def v2_db_path(tmp_path):
    p = str(tmp_path / "forwarder.sqlite")
    _build_v2_db(p)
    return p


# ---------------------------------------------------------------------------
# R3：v2 现网库迁移
# ---------------------------------------------------------------------------


class TestV2Migration:
    async def test_v2_migration_row_counts_preserved(self, v2_db_path):
        """迁移前后行数零变化（R3 对账核心）。"""
        before = _counts(v2_db_path)
        db = Database(v2_db_path)
        await db.open()
        await db.migrate()
        after = _counts(v2_db_path)
        assert before == after
        assert after["forward_progress"] == 15
        assert after["dedup_hashes"] == 13727
        assert after["rules"] == 9
        assert after["sources"] == 3
        assert after["app_config"] == 5
        await db.close()

    async def test_v2_migration_sets_user_version(self, v2_db_path):
        db = Database(v2_db_path)
        await db.open()
        assert await db._user_version() == 0
        await db.migrate()
        assert await db._user_version() == CURRENT_SCHEMA_VERSION
        await db.close()

    async def test_v2_migration_data_intact(self, v2_db_path):
        """迁移后数据内容不变（抽查 progress + sources 标题）。"""
        db = Database(v2_db_path)
        await db.open()
        try:
            await db.migrate()
            assert await db.get_progress(-1009000000) == 100
            assert await db.get_progress(-1008999986) == 114
            src_repo = SourceRepository(db)
            sources = await src_repo.get_all()
            assert len(sources) == 3
            assert sources[0]["cached_title"] == "频道0"
            # rules JSON 反序列化
            rule_repo = RuleRepository(db)
            rules = await rule_repo.get_all()
            assert len(rules) == 9
            assert rules[0]["any_keywords"] == ["kw"]
        finally:
            await db.close()

    async def test_v2_migration_creates_schema_meta_and_index(self, v2_db_path):
        db = Database(v2_db_path)
        await db.open()
        await db.migrate()
        conn = sqlite3.connect(v2_db_path)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        assert "schema_meta" in tables
        assert "idx_dedup_ts" in indexes
        await db.close()

    async def test_migration_idempotent(self, v2_db_path):
        """重复 migrate 幂等（user_version 已到位直接返回）。"""
        db = Database(v2_db_path)
        await db.open()
        await db.migrate()
        counts_first = _counts(v2_db_path)
        await db.migrate()  # 第二次应无操作
        assert _counts(v2_db_path) == counts_first
        await db.close()

    async def test_dedup_hash_ops_after_migration(self, v2_db_path):
        """迁移后去重读写正常。"""
        db = Database(v2_db_path)
        await db.open()
        await db.migrate()
        assert await db.check_hash("h0") is True
        assert await db.check_hash("nonexistent") is False
        await db.add_hash("newhash")
        assert await db.check_hash("newhash") is True
        removed = await db.prune_old_hashes(days=0)
        assert removed >= 13727
        await db.close()


# ---------------------------------------------------------------------------
# 全新空库
# ---------------------------------------------------------------------------


class TestFreshDb:
    async def test_fresh_db_creates_v3_schema(self, tmp_path):
        p = str(tmp_path / "fresh.sqlite")
        db = Database(p)
        await db.open()
        await db.migrate()
        assert await db._user_version() == CURRENT_SCHEMA_VERSION
        stats = await db.get_db_stats()
        assert stats == {"dedup_hashes": 0, "invalid_links": 0}
        await db.close()


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------


class TestRepositories:
    async def test_config_repo_roundtrip(self, tmp_path):
        db = Database(str(tmp_path / "a.sqlite"))
        await db.open()
        await db.migrate()
        repo = ConfigRepository(db)
        await repo.save("k", {"x": 1, "y": ["a"]})
        assert await repo.get("k") == {"x": 1, "y": ["a"]}
        assert await repo.get("missing", "dft") == "dft"
        await repo.remove("k")
        assert await repo.get("k") is None
        await db.close()

    async def test_config_repo_corrupt_json_tolerated(self, tmp_path):
        db = Database(str(tmp_path / "a.sqlite"))
        await db.open()
        await db.migrate()
        await db.execute("INSERT INTO app_config VALUES ('bad', '{not json')")
        repo = ConfigRepository(db)
        assert await repo.get("bad", {}) == {}
        await db.close()

    async def test_source_repo_crud(self, tmp_path):
        db = Database(str(tmp_path / "a.sqlite"))
        await db.open()
        await db.migrate()
        repo = SourceRepository(db)
        await repo.save({"identifier": "-100123", "cached_title": "测试"})
        await repo.save({"identifier": 123456789, "cached_title": "数字"})
        all_sources = await repo.get_all()
        assert len(all_sources) == 2
        assert {s["identifier"] for s in all_sources} == {"-100123", "123456789"}
        await repo.remove("-100123")
        assert len(await repo.get_all()) == 1
        await db.close()

    async def test_rule_repo_crud_and_json(self, tmp_path):
        db = Database(str(tmp_path / "a.sqlite"))
        await db.open()
        await db.migrate()
        repo = RuleRepository(db)
        await repo.save(
            {
                "name": "r1",
                "target_identifier": "-100999",
                "topic_id": 5,
                "all_keywords": ["a", "b"],
                "any_keywords": ["c"],
                "file_types": ["video/"],
                "file_name_patterns": ["*.mp4"],
            }
        )
        rules = await repo.get_all()
        assert rules[0]["all_keywords"] == ["a", "b"]
        assert rules[0]["topic_id"] == 5
        await repo.remove("r1")
        await repo.clear()
        assert await repo.get_all() == []
        await db.close()

    async def test_link_checker_meta_convention(self, tmp_path):
        db = Database(str(tmp_path / "a.sqlite"))
        await db.open()
        await db.migrate()
        assert await db.get_link_checker_progress() == 0
        await db.set_link_checker_progress(777)
        assert await db.get_link_checker_progress() == 777
        await db.add_pending_link("https://pan.baidu.com/s/1", 42)
        await db.add_pending_link("https://pan.baidu.com/s/1", 42)  # OR IGNORE
        await db.update_link_status("https://pan.baidu.com/s/1", "invalid")
        links = await db.get_links_to_check()
        assert links == [("https://pan.baidu.com/s/1", 42)]
        stats = await db.get_db_stats()
        assert stats["invalid_links"] == 1
        await db.close()
