import json
import aiosqlite
from typing import Optional, Dict, Any
from config import config  # предполагается, что config.DATABASE_URL содержит sqlite путь

DB_PATH = config.DATABASE_URL  # например "KingStoreV/bot_database.sqlite3"

async def init_posts_table() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        message_id INTEGER,
        inline_message_id TEXT,
        text TEXT,
        caption TEXT,
        media_type TEXT,
        media_file_id TEXT,
        reply_markup TEXT,
        created_by INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql)
        await db.commit()

async def save_post(chat_id: str, message_id: Optional[int] = None,
                    inline_message_id: Optional[str] = None,
                    text: Optional[str] = None, caption: Optional[str] = None,
                    media_type: Optional[str] = None, media_file_id: Optional[str] = None,
                    reply_markup: Optional[Dict[str, Any]] = None,
                    created_by: Optional[int] = None) -> int:
    rm = json.dumps(reply_markup) if reply_markup is not None else None
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO posts (chat_id, message_id, inline_message_id, text, caption, media_type, media_file_id, reply_markup, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(chat_id), message_id, inline_message_id, text, caption, media_type, media_file_id, rm, created_by)
        )
        await db.commit()
        rowid = cur.lastrowid
        return rowid

async def update_reply_markup_by_chat_message(chat_id: str, message_id: int, reply_markup: Dict[str, Any]) -> None:
    rm = json.dumps(reply_markup) if reply_markup is not None else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET reply_markup = ?, updated_at = datetime('now') WHERE chat_id = ? AND message_id = ?",
            (rm, str(chat_id), message_id)
        )
        await db.commit()

async def get_post_by_chat_message(chat_id: str, message_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM posts WHERE chat_id = ? AND message_id = ?", (str(chat_id), message_id))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("reply_markup"):
            d["reply_markup"] = json.loads(d["reply_markup"])
        return d

async def get_post_by_id(post_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("reply_markup"):
            d["reply_markup"] = json.loads(d["reply_markup"])
        return d