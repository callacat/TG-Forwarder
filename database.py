# database.py
import logging
import aiosqlite
import asyncio
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Union

from loguru import logger

DB_PATH = "/app/data/forwarder.sqlite"
db_lock = asyncio.Lock() 
_db_conn = None 

async def get_db():
    """获取一个数据库连接"""
    global _db_conn
    if _db_conn is None:
        raise ConnectionError("数据库未初始化。请先调用 init_db。")
    return _db_conn

async def init_db():
    """
    初始化数据库连接和表结构。
    """
    global _db_conn
    async with db_lock:
        if _db_conn:
            return 

        try:
            _db_conn = await aiosqlite.connect(DB_PATH)
            # 启用 WAL 模式提高并发性能
            await _db_conn.execute("PRAGMA journal_mode=WAL;")
            
            logger.info(f"✅ 数据库连接已建立: {DB_PATH}")

            # 1. 基础表 (原有)
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS dedup_hashes (
              hash TEXT PRIMARY KEY,
              timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)
            
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS forward_progress (
              channel_id INTEGER PRIMARY KEY,
              message_id INTEGER
            )
            """)
            
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS link_checker (
              url TEXT PRIMARY KEY,
              message_id INTEGER,
              status TEXT,
              last_checked DATETIME
            )
            """)
            
            # 2. 规则与配置表 (新 - 替代 rules_db.json)
            
            # 监控源表
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
              identifier TEXT PRIMARY KEY,
              check_replies BOOLEAN DEFAULT 0,
              replies_limit INTEGER DEFAULT 5,
              forward_new_only BOOLEAN,
              resolved_id INTEGER,
              cached_title TEXT
            )
            """)
            
            # 分发规则表
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS rules (
              name TEXT PRIMARY KEY,
              target_identifier TEXT,
              topic_id INTEGER,
              all_keywords TEXT,      -- JSON List
              any_keywords TEXT,      -- JSON List
              file_types TEXT,        -- JSON List
              file_name_patterns TEXT -- JSON List
            )
            """)
            
            # 全局配置表 (存储 AdFilter, Whitelist, Settings 等单例对象)
            # key: 'ad_filter', 'whitelist', 'content_filter', 'replacements', 'system_settings'
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
              key TEXT PRIMARY KEY,
              value TEXT -- JSON Object
            )
            """)
            
            await _db_conn.commit()
            logger.info("✅ 所有数据库表均已初始化 (SQLite Rules Ready)。")

        except Exception as e:
            logger.critical(f"❌ 数据库初始化失败: {e}")
            _db_conn = None
            raise

# --- 通用 JSON 配置存储 ---

async def save_config_json(key: str, data: Dict[str, Any]):
    """保存配置对象到 app_config 表"""
    try:
        db = await get_db()
        json_str = json.dumps(data, ensure_ascii=False)
        await db.execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
            (key, json_str)
        )
        await db.commit()
    except Exception as e:
        logger.error(f"保存配置 {key} 失败: {e}")

async def get_config_json(key: str) ->  Dict[str, Any]:
    """读取配置对象"""
    try:
        db = await get_db()
        async with db.execute("SELECT value FROM app_config WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
            return {}
    except Exception as e:
        logger.error(f"读取配置 {key} 失败: {e}")
        return {}

# --- 监控源 (Sources) 操作 ---

async def get_all_sources() -> List[Dict[str, Any]]:
    try:
        db = await get_db()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM sources") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"读取源列表失败: {e}")
        return []

async def save_source(data: Dict[str, Any]):
    try:
        db = await get_db()
        await db.execute("""
            INSERT OR REPLACE INTO sources 
            (identifier, check_replies, replies_limit, forward_new_only, resolved_id, cached_title)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(data.get('identifier')), 
            data.get('check_replies', False),
            data.get('replies_limit', 5),
            data.get('forward_new_only'),
            data.get('resolved_id'),
            data.get('cached_title')
        ))
        await db.commit()
    except Exception as e:
        logger.error(f"保存源失败: {e}")

async def remove_source(identifier: str):
    try:
        db = await get_db()
        await db.execute("DELETE FROM sources WHERE identifier = ?", (str(identifier),))
        await db.commit()
    except Exception as e:
        logger.error(f"删除源失败: {e}")

# --- 分发规则 (Rules) 操作 ---

async def get_all_rules() -> List[Dict[str, Any]]:
    try:
        db = await get_db()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM rules") as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                # 反序列化 JSON 字段
                d['all_keywords'] = json.loads(d['all_keywords'] or '[]')
                d['any_keywords'] = json.loads(d['any_keywords'] or '[]')
                d['file_types'] = json.loads(d['file_types'] or '[]')
                d['file_name_patterns'] = json.loads(d['file_name_patterns'] or '[]')
                result.append(d)
            return result
    except Exception as e:
        logger.error(f"读取规则列表失败: {e}")
        return []

async def save_rule(data: Dict[str, Any]):
    try:
        db = await get_db()
        await db.execute("""
            INSERT OR REPLACE INTO rules 
            (name, target_identifier, topic_id, all_keywords, any_keywords, file_types, file_name_patterns)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('name'),
            str(data.get('target_identifier')),
            data.get('topic_id'),
            json.dumps(data.get('all_keywords', []), ensure_ascii=False),
            json.dumps(data.get('any_keywords', []), ensure_ascii=False),
            json.dumps(data.get('file_types', []), ensure_ascii=False),
            json.dumps(data.get('file_name_patterns', []), ensure_ascii=False)
        ))
        await db.commit()
    except Exception as e:
        logger.error(f"保存规则失败: {e}")

async def remove_rule(name: str):
    try:
        db = await get_db()
        await db.execute("DELETE FROM rules WHERE name = ?", (name,))
        await db.commit()
    except Exception as e:
        logger.error(f"删除规则失败: {e}")

async def clear_rules():
    """清空规则表（用于重排顺序时的全量覆盖）"""
    try:
        db = await get_db()
        await db.execute("DELETE FROM rules")
        await db.commit()
    except Exception as e:
        logger.error(f"清空规则失败: {e}")

# --- 原有 API (保留) ---

async def check_hash(hash_str: str) -> bool:
    try:
        db = await get_db()
        async with db.execute("SELECT 1 FROM dedup_hashes WHERE hash = ?", (hash_str,)) as cursor:
            return await cursor.fetchone() is not None
    except Exception as e:
        return True 

async def add_hash(hash_str: str):
    try:
        db = await get_db()
        await db.execute("INSERT OR REPLACE INTO dedup_hashes (hash, timestamp) VALUES (?, ?)", (hash_str, datetime.now()))
        await db.commit()
    except Exception: pass

async def prune_old_hashes(days: int = 30):
    try:
        cutoff = datetime.now() - timedelta(days=days)
        db = await get_db()
        await db.execute("DELETE FROM dedup_hashes WHERE timestamp < ?", (cutoff,))
        await db.commit()
    except Exception: pass

async def get_progress(channel_id: int) -> int:
    try:
        db = await get_db()
        async with db.execute("SELECT message_id FROM forward_progress WHERE channel_id = ?", (channel_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except Exception: return 0

async def set_progress(channel_id: int, message_id: int):
    try:
        db = await get_db()
        await db.execute("INSERT OR REPLACE INTO forward_progress (channel_id, message_id) VALUES (?, ?)", (channel_id, message_id))
        await db.commit()
    except Exception: pass

async def get_db_stats() -> dict:
    try:
        db = await get_db()
        async with db.execute("SELECT COUNT(*) FROM dedup_hashes") as c: dedup = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM link_checker WHERE status = 'invalid'") as c: invalid = (await c.fetchone())[0]
        return { "dedup_hashes": dedup, "invalid_links": invalid }
    except Exception: return {}

# Link Checker 相关
async def get_link_checker_progress() -> int:
    try:
        db = await get_db()
        async with db.execute("SELECT message_id FROM link_checker WHERE url = '_meta_'") as c: return (await c.fetchone() or [0])[0]
    except: return 0

async def set_link_checker_progress(mid: int):
    try:
        db = await get_db()
        await db.execute("INSERT OR REPLACE INTO link_checker (url, message_id, status) VALUES (?, ?, ?)", ("_meta_", mid, "progress"))
        await db.commit()
    except: pass

async def add_pending_link(url: str, mid: int):
    try:
        db = await get_db()
        await db.execute("INSERT OR IGNORE INTO link_checker (url, message_id, status) VALUES (?, ?, ?)", (url, mid, "pending"))
        await db.commit()
    except: pass

async def get_links_to_check() -> list:
    try:
        db = await get_db()
        async with db.execute("SELECT url, message_id FROM link_checker WHERE status != 'valid' AND url != '_meta_'") as c: return await c.fetchall()
    except: return []

async def update_link_status(url: str, status: str):
    try:
        db = await get_db()
        await db.execute("UPDATE link_checker SET status = ?, last_checked = ? WHERE url = ?", (status, datetime.now(), url))
        await db.commit()
    except: pass