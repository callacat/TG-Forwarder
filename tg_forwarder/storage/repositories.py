# -*- coding: utf-8 -*-
"""仓储层：收编 v2 分散的表操作（sources/rules/app_config）。

dict 进出，序列化与 v2 表结构一致（R3 向后兼容）；损坏 JSON 容错
（记录 warning 返回 default，不抛）。
"""
import json
from typing import Any, Dict, List, Optional

from loguru import logger

from tg_forwarder.storage.db import Database


class SourceRepository:
    """sources 表仓储：get_all()/save(data)/remove(identifier)。"""

    def __init__(self, db: Database):
        self.db = db

    async def get_all(self) -> List[Dict[str, Any]]:
        try:
            cur = await self.db._require_conn().execute("SELECT * FROM sources")
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description or []]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as e:
            logger.error(f"读取源列表失败: {e}")
            return []

    async def save(self, data: Dict[str, Any]) -> None:
        try:
            await self.db._require_conn().execute(
                """
                INSERT OR REPLACE INTO sources
                (identifier, check_replies, replies_limit, forward_new_only,
                 resolved_id, cached_title, sync_edits, sync_deletes,
                 age_cutoff_hours, header_template, digest_enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(data.get("identifier")),
                    data.get("check_replies", False),
                    data.get("replies_limit", 5),
                    data.get("forward_new_only"),
                    data.get("resolved_id"),
                    data.get("cached_title"),
                    data.get("sync_edits", False),
                    data.get("sync_deletes", False),
                    data.get("age_cutoff_hours"),
                    data.get("header_template"),
                    data.get("digest_enabled", False),
                ),
            )
            await self.db._require_conn().commit()
        except Exception as e:
            logger.error(f"保存源失败: {e}")
            raise

    async def remove(self, identifier: str) -> None:
        try:
            await self.db._require_conn().execute(
                "DELETE FROM sources WHERE identifier = ?", (str(identifier),)
            )
            await self.db._require_conn().commit()
        except Exception as e:
            logger.error(f"删除源失败: {e}")
            raise


class RuleRepository:
    """rules 表仓储：get_all()/save(data)/remove(name)/clear()。

    JSON List 字段（all_keywords 等）与 v2 序列化一致。
    """

    _JSON_FIELDS = (
        "all_keywords",
        "any_keywords",
        "file_types",
        "file_name_patterns",
        "media_types",
    )

    def __init__(self, db: Database):
        self.db = db

    async def get_all(self) -> List[Dict[str, Any]]:
        try:
            cur = await self.db._require_conn().execute("SELECT * FROM rules")
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description or []]
            result = []
            for row in rows:
                d = dict(zip(cols, row))
                for field in self._JSON_FIELDS:
                    raw = d.get(field)
                    if raw is None or raw == "":
                        d[field] = []
                        continue
                    try:
                        d[field] = json.loads(raw)
                    except (TypeError, ValueError) as e:
                        logger.warning(f"规则 {d.get('name')} 字段 {field} 损坏，已置空: {e}")
                        d[field] = []
                result.append(d)
            return result
        except Exception as e:
            logger.error(f"读取规则列表失败: {e}")
            return []

    async def save(self, data: Dict[str, Any]) -> None:
        try:
            await self.db._require_conn().execute(
                """
                INSERT OR REPLACE INTO rules
                (name, target_identifier, topic_id, all_keywords, any_keywords,
                 file_types, file_name_patterns, media_types, max_file_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("name"),
                    str(data.get("target_identifier")),
                    data.get("topic_id"),
                    json.dumps(data.get("all_keywords", []), ensure_ascii=False),
                    json.dumps(data.get("any_keywords", []), ensure_ascii=False),
                    json.dumps(data.get("file_types", []), ensure_ascii=False),
                    json.dumps(data.get("file_name_patterns", []), ensure_ascii=False),
                    json.dumps(data.get("media_types", []), ensure_ascii=False),
                    data.get("max_file_size", 0),
                ),
            )
            await self.db._require_conn().commit()
        except Exception as e:
            logger.error(f"保存规则失败: {e}")
            raise

    async def remove(self, name: str) -> None:
        try:
            await self.db._require_conn().execute(
                "DELETE FROM rules WHERE name = ?", (name,)
            )
            await self.db._require_conn().commit()
        except Exception as e:
            logger.error(f"删除规则失败: {e}")
            raise

    async def clear(self) -> None:
        """清空规则表（重排顺序时的全量覆盖用）。"""
        try:
            await self.db._require_conn().execute("DELETE FROM rules")
            await self.db._require_conn().commit()
        except Exception as e:
            logger.error(f"清空规则失败: {e}")
            raise

    async def replace_all(self, rules: List[Dict[str, Any]]) -> None:
        """事务化全量重写规则表（C5 修复：重排 clear+rewrite 原子，
        中途失败整体回滚，不留半新半旧）。"""
        conn = self.db._require_conn()
        try:
            # 默认非 autocommit：首条 DML 隐式开事务，末尾单次 commit 保证原子
            await conn.execute("DELETE FROM rules")
            for data in rules:
                await conn.execute(
                    """
                    INSERT OR REPLACE INTO rules
                    (name, target_identifier, topic_id, all_keywords, any_keywords,
                     file_types, file_name_patterns, media_types, max_file_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data.get("name"),
                        str(data.get("target_identifier")),
                        data.get("topic_id"),
                        json.dumps(data.get("all_keywords", []), ensure_ascii=False),
                        json.dumps(data.get("any_keywords", []), ensure_ascii=False),
                        json.dumps(data.get("file_types", []), ensure_ascii=False),
                        json.dumps(
                            data.get("file_name_patterns", []), ensure_ascii=False
                        ),
                        json.dumps(data.get("media_types", []), ensure_ascii=False),
                        data.get("max_file_size", 0),
                    ),
                )
            await conn.commit()
        except Exception as e:
            await conn.rollback()
            logger.error(f"规则全量重写失败（已回滚）: {e}")
            raise


class ConfigRepository:
    """app_config 表仓储：get(key, default)/save(key, data)。

    app_config 表是 v3 单一配置源（R4）；v2 遗留 key
    （system_settings/ad_filter/whitelist/content_filter/replacements）直接可读。
    """

    def __init__(self, db: Database):
        self.db = db

    async def get(self, key: str, default: Any = None) -> Any:
        try:
            cur = await self.db._require_conn().execute(
                "SELECT value FROM app_config WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
            if not row or row[0] is None:
                return default
            try:
                return json.loads(row[0])
            except (TypeError, ValueError) as e:
                logger.warning(f"配置 {key} JSON 损坏，返回默认值: {e}")
                return default
        except Exception as e:
            logger.error(f"读取配置 {key} 失败: {e}")
            return default

    async def save(self, key: str, data: Any) -> None:
        try:
            await self.db._require_conn().execute(
                "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
                (key, json.dumps(data, ensure_ascii=False)),
            )
            await self.db._require_conn().commit()
        except Exception as e:
            logger.error(f"保存配置 {key} 失败: {e}")
            raise

    async def remove(self, key: str) -> None:
        try:
            await self.db._require_conn().execute(
                "DELETE FROM app_config WHERE key = ?", (key,)
            )
            await self.db._require_conn().commit()
        except Exception as e:
            logger.error(f"删除配置 {key} 失败: {e}")
            raise

    async def keys(self) -> List[str]:
        """列出全部 key（bootstrap 判空用）。"""
        try:
            cur = await self.db._require_conn().execute("SELECT key FROM app_config")
            rows = await cur.fetchall()
            return [r[0] for r in rows]
        except Exception as e:
            logger.error(f"列出配置 key 失败: {e}")
            return []
