import json
import time
import secrets
from typing import Optional

# Ожидается, что database.db установится в bot.py до использования этого модуля
import database as database_module  # type: ignore

def make_short_id(nbytes: int = 6) -> str:
    """
    Возвращает короткий hex id. Для nbytes=6 -> 12 hex символов.
    """
    return secrets.token_hex(nbytes)

async def store_payload(payload: dict) -> str:
    """
    Сохраняет payload в таблицу callback_payloads и возвращает payload_id.
    """
    db = database_module.db
    if db is None:
        raise RuntimeError("Database object is not initialized")
    payload_id = make_short_id(6)
    data = json.dumps(payload, ensure_ascii=False)
    ts = int(time.time())
    await db.execute(
        "INSERT INTO callback_payloads (id, data, created_at) VALUES (?, ?, ?)",
        (payload_id, data, ts),
    )
    return payload_id

async def get_payload(payload_id: str) -> Optional[dict]:
    """
    Возвращает payload по id или None если не найдено.
    """
    db = database_module.db
    if db is None:
        raise RuntimeError("Database object is not initialized")
    row = await db.fetchone("SELECT data FROM callback_payloads WHERE id = ?", (payload_id,))
    if not row:
        return None
    try:
        data = row["data"]
    except Exception:
        data = row[0]
    try:
        return json.loads(data)
    except Exception:
        return None

async def delete_payload(payload_id: str) -> None:
    db = database_module.db
    if db is None:
        raise RuntimeError("Database object is not initialized")
    await db.execute("DELETE FROM callback_payloads WHERE id = ?", (payload_id,))