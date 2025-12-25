import asyncio
import logging
import os
from typing import List

from database import Database
from config import config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Create the Database instance early and export it into database module
db = Database(config.DATABASE_URL)

# Ensure database.db is set before importing handlers/services so they can import `db` name
import database as database_module  # type: ignore
database_module.db = db

# Now import handlers (they can safely import services which import `database.db`)
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from handlers import common, posts, forward_channel_id, edit_posts, post_edit_flow  # noqa: E402

async def init_db() -> None:
    await db.connect()
    # Ensure callback_payloads table exists (short payload storage for callback_data)
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS callback_payloads (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
    except Exception:
        logger.exception("Failed to ensure callback_payloads table exists")

    # If you have a sql/schema.sql file, try to execute it safely (non-blocking)
    schema_path = os.path.join(os.path.dirname(__file__), "sql", "schema.sql")
    if os.path.exists(schema_path):
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                sql = f.read()
            # Split into statements and execute individually (safe fallback)
            statements: List[str] = [s.strip() for s in sql.split(";") if s.strip()]
            for stmt in statements:
                try:
                    await db.execute(stmt + ";")
                except Exception:
                    logger.exception("Failed to execute statement from schema.sql: %s", stmt)
        except FileNotFoundError:
            logger.warning("SQL schema file not found: %s", schema_path)
    else:
        logger.info("No schema.sql found; skipped schema file execution")

async def main() -> None:
    await init_db()

    bot = Bot(token=config.BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher(storage=MemoryStorage())

    # register routers
    dp.include_router(forward_channel_id.router)
    dp.include_router(common.router)
    dp.include_router(edit_posts.router)
    dp.include_router(post_edit_flow.router)
    dp.include_router(posts.router)

    try:
        logger.info("Starting polling...")
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down...")
        await bot.session.close()
        await db.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped by user")