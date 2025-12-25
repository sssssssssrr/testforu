import aiosqlite
import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str) -> None:
        # Accept formats: sqlite:///./db.sqlite3 or ./db.sqlite3
        if db_path.startswith("sqlite:///"):
            self._path = db_path.replace("sqlite:///", "")
        else:
            self._path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        logger.info("Connecting to SQLite at %s", self._path)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        await self._conn.commit()

    async def execute(self, query: str, params: Optional[List[Any]] = None) -> aiosqlite.Cursor:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        params = params or []
        try:
            cur = await self._conn.execute(query, params)
            await self._conn.commit()
            return cur
        except Exception:
            logger.exception("DB execution failed: %s | params=%s", query, params)
            raise

    async def fetchone(self, query: str, params: Optional[List[Any]] = None) -> Optional[aiosqlite.Row]:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        params = params or []
        try:
            cur = await self._conn.execute(query, params)
            row = await cur.fetchone()
            return row
        except Exception:
            logger.exception("DB fetchone failed: %s | params=%s", query, params)
            raise

    async def fetchall(self, query: str, params: Optional[List[Any]] = None) -> List[aiosqlite.Row]:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        params = params or []
        try:
            cur = await self._conn.execute(query, params)
            rows = await cur.fetchall()
            return rows
        except Exception:
            logger.exception("DB fetchall failed: %s | params=%s", query, params)
            raise

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None


# Global database instance placeholder; will be set by bot.py after Database(...) creation.
# Important: define and export `db` at module import time to avoid ImportError in other modules.
db: Optional[Database] = None