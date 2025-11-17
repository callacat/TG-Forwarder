# database.py
import logging
import aiosqlite
import asyncio
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DB_PATH = "/app/data/forwarder.sqlite"
db_lock = asyncio.Lock() # 确保数据库初始化只运行一次

_db_conn = None # 全局连接池（简化版）

async def get_db():
    """获取一个数据库连接"""
    global _db_conn
    if _db_conn is None:
        raise ConnectionError("数据库未初始化。请先调用 init_db。")
    return _db_conn

async def init_db():
    """
    初始化数据库连接和表结构。
    这是 v9.0 蓝图的核心。
    """
    global _db_conn
    async with db_lock:
        if _db_conn:
            return # 已经初始化

        try:
            # 1. 创建连接
            _db_conn = await aiosqlite.connect(DB_PATH)
            # 开启 WAL 模式 (Write-Ahead Logging)，大幅提高并发读写性能
            await _db_conn.execute("PRAGMA journal_mode=WAL;")
            
            logger.info(f"✅ 数据库连接已建立: {DB_PATH}")

            # 2. 创建表 (如果不存在)
            
            # (v9.0) 替换 dedup_db.json
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS dedup_hashes (
              hash TEXT PRIMARY KEY,
              timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """)
            
            # (v9.0) 替换 forwarder_progress.json
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS forward_progress (
              channel_id INTEGER PRIMARY KEY,
              message_id INTEGER
            )
            """)
            
            # (v9.0) 替换 link_checker_db.json
            await _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS link_checker (
              url TEXT PRIMARY KEY,
              message_id INTEGER,
              status TEXT,
              last_checked DATETIME
            )
            """)
            
            await _db_conn.commit()
            logger.info("✅ 所有数据库表均已初始化。")

        except Exception as e:
            logger.critical(f"❌ 数据库初始化失败: {e}")
            _db_conn = None
            raise

# --- 去重 (Dedup) API ---

async def check_hash(hash_str: str) -> bool:
    """检查一个哈希是否存在 (替换 _is_duplicate)"""
    try:
        db = await get_db()
        async with db.execute("SELECT 1 FROM dedup_hashes WHERE hash = ?", (hash_str,)) as cursor:
            return await cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"检查哈希失败: {e}")
        return True # 安全起见，发生错误时假定为重复

async def add_hash(hash_str: str):
    """添加一个新哈希 (替换 _mark_as_processed)"""
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
    """(v9.0 蓝图) 清理旧的哈希记录"""
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
    """获取频道进度 (替换 _get_channel_progress)"""
    try:
        db = await get_db()
        async with db.execute("SELECT message_id FROM forward_progress WHERE channel_id = ?", (channel_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"获取进度失败: {e}")
        return 0

async def set_progress(channel_id: int, message_id: int):
    """设置频道进度 (替换 _set_channel_progress)"""
    try:
        db = await get_db()
        # INSERT OR REPLACE (UPSERT) 确保了原子性
        await db.execute(
            "INSERT OR REPLACE INTO forward_progress (channel_id, message_id) VALUES (?, ?)", 
            (channel_id, message_id)
        )
        await db.commit()
    except Exception as e:
        logger.error(f"设置进度失败: {e}")

# --- 链接检测 (Link Checker) API (v9.0 蓝图) ---
# (我们将在重构 link_checker.py 时实现这些)
# async def get_links_to_check(): ...
# async def update_link_status(...): ...
# async def get_link_checker_progress(): ...
# async def set_link_checker_progress(...): ...