from typing import List, Optional
import logging
from database import db
import datetime

logger = logging.getLogger(__name__)


async def create_channel(chat_id: str, title: Optional[str], added_by: Optional[int]) -> dict:
    now = datetime.datetime.utcnow().isoformat()
    query = """
    INSERT INTO channels (chat_id, title, added_by, created_at)
    VALUES (?, ?, ?, ?)
    """
    cur = await db.execute(query, [chat_id, title, added_by, now])
    return {"id": cur.lastrowid, "chat_id": chat_id, "title": title, "added_by": added_by, "created_at": now}


async def list_channels() -> List[dict]:
    rows = await db.fetchall("SELECT * FROM channels ORDER BY id DESC", [])
    return [dict(r) for r in rows]


async def get_channel_by_chat_id(chat_id: str) -> Optional[dict]:
    row = await db.fetchone("SELECT * FROM channels WHERE chat_id = ?", [chat_id])
    if not row:
        return None
    return dict(row)


async def get_channel_by_id(idx: int) -> Optional[dict]:
    row = await db.fetchone("SELECT * FROM channels WHERE id = ?", [idx])
    if not row:
        return None
    return dict(row)


async def delete_channel(chat_id: str) -> None:
    await db.execute("DELETE FROM channels WHERE chat_id = ?", [chat_id])
    logger.debug("Deleted channel %s", chat_id)