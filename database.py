# database.py
import logging
import aiosqlite
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Union

logger = logging.getLogger(__name__)

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
            await _db_conn.execute("PRAGMA journal_mode=WAL;")
            
            logger.info(f"✅ 数据库连接已建立: {DB_PATH}")

            # --- v9.0 ---
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
            
            # (新) v9.1：设置表
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT
            )
            """)
            
            # (新) v9.1：初始化默认去重天数
            await _db_conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                ('dedup_retention_days', '30')
            )
            
            await _db_conn.commit()
            logger.info("✅ 所有数据库表均已初始化。")

        except Exception as e:
            logger.critical(f"❌ 数据库初始化失败: {e}")
            _db_conn = None
            raise

# --- 去重 (Dedup) API ---

async def check_hash(hash_str: str) -> bool:
    """检查一个哈希是否存在"""
    try:
        db = await get_db()
        async with db.execute("SELECT 1 FROM dedup_hashes WHERE hash = ?", (hash_str,)) as cursor:
            return await cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"检查哈希失败: {e}")
        return True 

async def add_hash(hash_str: str):
    """添加一个新哈希"""
    try:
        db = await get_db()
        await db.execute(
            "INSERT OR REPLACE INTO dedup_hashes (hash, timestamp) VALUES (?, ?)", 
            (hash_str, datetime.now())
        )
        await db.commit()
    except Exception as e:
        logger.error(f"添加哈希失败: {e}")

async def prune_old_hashes(days: int = 30):
    """清理旧的哈希记录"""
    try:
        cutoff_date = datetime.now() - timedelta(days=days)
        db = await get_db()
        cursor = await db.execute("DELETE FROM dedup_hashes WHERE timestamp < ?", (cutoff_date,))
        await db.commit()
        logger.info(f"✅ 已清理 {cursor.rowcount} 条 {days} 天前的旧哈希记录。")
    except Exception as e:
        logger.error(f"清理哈希记录失败: {e}")

# --- 进度 (Progress) API ---

async def get_progress(channel_id: int) -> int:
    """获取频道进度"""
    try:
        db = await get_db()
        async with db.execute("SELECT message_id FROM forward_progress WHERE channel_id = ?", (channel_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"获取进度失败: {e}")
        return 0

async def set_progress(channel_id: int, message_id: int):
    """设置频道进度"""
    try:
        db = await get_db()
        await db.execute(
            "INSERT OR REPLACE INTO forward_progress (channel_id, message_id) VALUES (?, ?)", 
            (channel_id, message_id)
        )
        await db.commit()
    except Exception as e:
        logger.error(f"设置进度失败: {e}")

# --- (新) v9.1：统计 API ---

async def get_db_stats() -> dict:
    """获取 SQLite 数据库的统计信息"""
    try:
        db = await get_db()
        
        async with db.execute("SELECT COUNT(*) FROM dedup_hashes") as cursor:
            dedup_count = (await cursor.fetchone() or [0])[0]
            
        async with db.execute("SELECT COUNT(*) FROM forward_progress") as cursor:
            progress_count = (await cursor.fetchone() or [0])[0]
            
        async with db.execute("SELECT COUNT(*) FROM link_checker WHERE status = 'invalid'") as cursor:
            invalid_links = (await cursor.fetchone() or [0])[0]
            
        return {
            "dedup_hashes": dedup_count,
            "forward_progress_channels": progress_count,
            "invalid_links": invalid_links
        }
    except Exception as e:
        logger.error(f"获取数据库统计失败: {e}")
        return { "dedup_hashes": "错误", "forward_progress_channels": "错误", "invalid_links": "错误" }

# --- (新) v9.1：设置 API ---

async def get_dedup_retention() -> int:
    """获取去重记录保留天数"""
    try:
        db = await get_db()
        async with db.execute("SELECT value FROM settings WHERE key = ?", ('dedup_retention_days',)) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 30
    except Exception as e:
        logger.error(f"获取去重天数失败: {e}")
        return 30

async def set_dedup_retention(days: int):
    """设置去重记录保留天数"""
    if not (1 <= days <= 30):
        raise ValueError("天数必须在 1 到 30 之间")
    try:
        db = await get_db()
        await db.execute(
            "UPDATE settings SET value = ? WHERE key = ?", 
            (str(days), 'dedup_retention_days')
        )
        await db.commit()
    except Exception as e:
        logger.error(f"设置去重天数失败: {e}")
        raise

# --- 链接检测 (Link Checker) API ---

async def get_link_checker_progress() -> int:
    """获取 link_checker 扫描到的最后 message_id"""
    try:
        db = await get_db()
        async with db.execute("SELECT message_id FROM link_checker WHERE url = ?", ("_meta_",)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"获取链接检测进度失败: {e}")
        return 0

async def set_link_checker_progress(message_id: int):
    """设置 link_checker 的最后 message_id"""
    try:
        db = await get_db()
        await db.execute(
            "INSERT OR REPLACE INTO link_checker (url, message_id, status) VALUES (?, ?, ?)", 
            ("_meta_", message_id, "progress")
        )
        await db.commit()
    except Exception as e:
        logger.error(f"设置链接检测进度失败: {e}")

async def add_pending_link(url: str, message_id: int):
    """添加一个新的待检测链接"""
    try:
        db = await get_db()
        await db.execute(
            "INSERT OR IGNORE INTO link_checker (url, message_id, status) VALUES (?, ?, ?)",
            (url, message_id, "pending")
        )
        await db.commit()
    except Exception as e:
        logger.error(f"添加待检测链接 {url} 失败: {e}")

async def get_links_to_check() -> list:
    """获取所有待检测 (pending) 或无效 (invalid) 的链接"""
    try:
        db = await get_db()
        async with db.execute("SELECT url, message_id FROM link_checker WHERE status != 'valid' AND url != '_meta_'") as cursor:
            return await cursor.fetchall()
    except Exception as e:
        logger.error(f"获取待检测链接列表失败: {e}")
        return []

async def update_link_status(url: str, status: str):
    """更新一个链接的状态 ('valid', 'invalid')"""
    try:
        db = await get_db()
        await db.execute(
            "UPDATE link_checker SET status = ?, last_checked = ? WHERE url = ?",
            (status, datetime.now(), url)
        )
        await db.commit()
    except Exception as e:
        logger.error(f"更新链接状态 {url} 失败: {e}")