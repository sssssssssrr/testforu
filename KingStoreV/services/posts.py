from typing import List, Optional
import logging
from database import db
from models import Post
import datetime

logger = logging.getLogger(__name__)


async def create_post(post: Post) -> Post:
    row = post.to_row()
    query = """
    INSERT INTO posts (author_id, text, photo_file_id, keyboard_json, status, created_at, updated_at, published_message_id, published_link)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = [
        row["author_id"],
        row["text"],
        row["photo_file_id"],
        row["keyboard_json"],
        row["status"],
        row["created_at"],
        row["updated_at"],
        row["published_message_id"],
        row["published_link"],
    ]
    cur = await db.execute(query, params)
    post.id = cur.lastrowid
    logger.debug("Created post with id=%s", post.id)
    return post


async def update_post(post_id: int, **fields) -> Optional[Post]:
    if not fields:
        return await get_post(post_id)
    allowed = {"text", "photo_file_id", "keyboard_json", "status", "published_message_id", "published_link"}
    set_parts = []
    params = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        set_parts.append(f"{k} = ?")
        params.append(v)
    # always update updated_at
    set_parts.append("updated_at = ?")
    params.append(datetime.datetime.utcnow().isoformat())
    params.append(post_id)
    set_sql = ", ".join(set_parts)
    query = f"UPDATE posts SET {set_sql} WHERE id = ?"
    await db.execute(query, params)
    return await get_post(post_id)


async def get_post(post_id: int) -> Optional[Post]:
    row = await db.fetchone("SELECT * FROM posts WHERE id = ?", [post_id])
    if row:
        return Post.from_row(row)
    return None


async def list_posts(author_id: Optional[int] = None, status: Optional[str] = None) -> List[Post]:
    q = "SELECT * FROM posts"
    params = []
    conds = []
    if author_id is not None:
        conds.append("author_id = ?")
        params.append(author_id)
    if status is not None:
        conds.append("status = ?")
        params.append(status)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY updated_at DESC"
    rows = await db.fetchall(q, params)
    return [Post.from_row(r) for r in rows]


async def delete_post(post_id: int) -> None:
    await db.execute("DELETE FROM posts WHERE id = ?", [post_id])
    logger.debug("Deleted post id=%s", post_id)