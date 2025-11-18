import aiosqlite
import asyncio
from datetime import datetime, timedelta
from loguru import logger  # 使用 Loguru

DB_PATH = "/app/data/forwarder.sqlite"
_db_conn = None
_db_lock = None  # 移除全局实例化

def get_lock():
    """获取或创建数据库锁 (解决 asyncio.run 事件循环不一致问题)"""
    global _db_lock
    if _db_lock is None:
        _db_lock = asyncio.Lock()
    return _db_lock

async def get_db():
    """获取一个数据库连接"""
    global _db_conn
    if _db_conn is None:
        raise ConnectionError("数据库未初始化。请先调用 init_db。")
    return _db_conn

async def init_db():
    """初始化数据库连接和表结构"""
    global _db_conn
    # 使用动态获取的锁
    async with get_lock():
        if _db_conn:
            return 

        try:
            _db_conn = await aiosqlite.connect(DB_PATH)
            await _db_conn.execute("PRAGMA journal_mode=WAL;")
            
            logger.info(f"✅ 数据库连接已建立: {DB_PATH}")

            # 建表逻辑
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
            
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT
            )
            """)
            
            # 初始化默认设置
            await _db_conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                ('dedup_retention_days', '30')
            )
            
            await _db_conn.commit()
            logger.success("数据库表初始化完成。")

        except Exception as e:
            logger.critical(f"数据库初始化失败: {e}")
            _db_conn = None
            raise

# --- 去重 (Dedup) API ---

async def check_hash(hash_str: str) -> bool:
    try:
        db = await get_db()
        async with db.execute("SELECT 1 FROM dedup_hashes WHERE hash = ?", (hash_str,)) as cursor:
            return await cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"检查哈希失败: {e}")
        return True 

async def add_hash(hash_str: str):
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
    try:
        cutoff_date = datetime.now() - timedelta(days=days)
        db = await get_db()
        cursor = await db.execute("DELETE FROM dedup_hashes WHERE timestamp < ?", (cutoff_date,))
        await db.commit()
        logger.info(f"已清理 {cursor.rowcount} 条 {days} 天前的旧哈希记录。")
    except Exception as e:
        logger.error(f"清理哈希记录失败: {e}")

# --- 进度 (Progress) API ---

async def get_progress(channel_id: int) -> int:
    try:
        db = await get_db()
        async with db.execute("SELECT message_id FROM forward_progress WHERE channel_id = ?", (channel_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"获取进度失败: {e}")
        return 0

async def set_progress(channel_id: int, message_id: int):
    try:
        db = await get_db()
        await db.execute(
            "INSERT OR REPLACE INTO forward_progress (channel_id, message_id) VALUES (?, ?)", 
            (channel_id, message_id)
        )
        await db.commit()
    except Exception as e:
        logger.error(f"设置进度失败: {e}")

# --- 统计 API ---

async def get_db_stats() -> dict:
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
        return {}

# --- 设置 API ---

async def get_dedup_retention() -> int:
    try:
        db = await get_db()
        async with db.execute("SELECT value FROM settings WHERE key = ?", ('dedup_retention_days',)) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 30
    except Exception as e:
        logger.error(f"获取去重天数失败: {e}")
        return 30

async def set_dedup_retention(days: int):
    if not (1 <= days <= 30):
        raise ValueError("天数必须在 1 到 30 之间")
    try:
        db = await get_db()
        await db.execute("UPDATE settings SET value = ? WHERE key = ?", (str(days), 'dedup_retention_days'))
        await db.commit()
    except Exception as e:
        logger.error(f"设置去重天数失败: {e}")
        raise

# --- 链接检测 API ---

async def get_link_checker_progress() -> int:
    try:
        db = await get_db()
        async with db.execute("SELECT message_id FROM link_checker WHERE url = ?", ("_meta_",)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"获取链接检测进度失败: {e}")
        return 0

async def set_link_checker_progress(message_id: int):
    try:
        db = await get_db()
        await db.execute("INSERT OR REPLACE INTO link_checker (url, message_id, status) VALUES (?, ?, ?)", ("_meta_", message_id, "progress"))
        await db.commit()
    except Exception as e:
        logger.error(f"设置链接检测进度失败: {e}")

async def add_pending_link(url: str, message_id: int):
    try:
        db = await get_db()
        await db.execute("INSERT OR IGNORE INTO link_checker (url, message_id, status) VALUES (?, ?, ?)", (url, message_id, "pending"))
        await db.commit()
    except Exception as e:
        logger.error(f"添加待检测链接 {url} 失败: {e}")

async def get_links_to_check() -> list:
    try:
        db = await get_db()
        async with db.execute("SELECT url, message_id FROM link_checker WHERE status != 'valid' AND url != '_meta_'") as cursor:
            return await cursor.fetchall()
    except Exception as e:
        logger.error(f"获取待检测链接列表失败: {e}")
        return []

async def update_link_status(url: str, status: str):
    try:
        db = await get_db()
        await db.execute("UPDATE link_checker SET status = ?, last_checked = ? WHERE url = ?", (status, datetime.now(), url))
        await db.commit()
    except Exception as e:
        logger.error(f"更新链接状态 {url} 失败: {e}")