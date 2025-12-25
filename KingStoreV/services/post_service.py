import json
from typing import Optional, Dict, Any, List
from database import db

async def get_post_row_by_id(post_id: int) -> Optional[Dict[str, Any]]:
    row = await db.fetchone("SELECT * FROM posts WHERE id = ?", [post_id])
    if not row:
        return None
    d = dict(row)
    try:
        d["keyboard"] = json.loads(d.get("keyboard_json") or "[]")
    except Exception:
        d["keyboard"] = []
    return d

async def get_post_row_by_chat_message(chat_id: str, message_id: int) -> Optional[Dict[str, Any]]:
    row = await db.fetchone(
        "SELECT * FROM posts WHERE (published_channel = ? OR published_link = ?) AND published_message_id = ?",
        [str(chat_id), str(chat_id), message_id],
    )
    if not row:
        # Попробуем упростить поиск: иногда chat_id хранится как строка или как @username
        row = await db.fetchone("SELECT * FROM posts WHERE published_message_id = ?", [message_id])
        if not row:
            return None
    d = dict(row)
    try:
        d["keyboard"] = json.loads(d.get("keyboard_json") or "[]")
    except Exception:
        d["keyboard"] = []
    return d

async def save_post_minimal(chat_id: str, message_id: int, text: Optional[str] = None,
                            photo_file_id: Optional[str] = None, keyboard_obj: Optional[List[Any]] = None,
                            created_by: Optional[int] = None) -> int:
    kjson = json.dumps(keyboard_obj or [])
    cur = await db.execute(
        "INSERT INTO posts (chat_id, published_message_id, text, photo_file_id, keyboard_json, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [str(chat_id), message_id, text, photo_file_id, kjson, created_by]
    )
    return cur.lastrowid

async def update_post_text(post_id: int, new_text: str) -> None:
    await db.execute("UPDATE posts SET text = ?, updated_at = datetime('now') WHERE id = ?", [new_text, post_id])

async def update_post_photo(post_id: int, new_file_id: str) -> None:
    await db.execute("UPDATE posts SET photo_file_id = ?, updated_at = datetime('now') WHERE id = ?", [new_file_id, post_id])

async def update_post_keyboard(post_id: int, keyboard_obj: Any) -> None:
    kjson = json.dumps(keyboard_obj or [])
    await db.execute("UPDATE posts SET keyboard_json = ?, updated_at = datetime('now') WHERE id = ?", [kjson, post_id])

async def update_reply_markup_by_chat_message(chat_id: str, message_id: int, reply_markup_obj: Any) -> None:
    # Если нужно обновлять запись по chat+message (когда post_id неизвестен)
    kjson = json.dumps(reply_markup_obj or [])
    await db.execute(
        "UPDATE posts SET keyboard_json = ?, updated_at = datetime('now') WHERE chat_id = ? AND published_message_id = ?",
        [kjson, str(chat_id), message_id]
    )