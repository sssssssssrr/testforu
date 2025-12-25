from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv
import os
import logging

load_dotenv()
logger = logging.getLogger(__name__)


@dataclass
class Config:
    BOT_TOKEN: str
    CHANNEL_ID: str
    DATABASE_URL: str

    @classmethod
    def from_env(cls) -> "Config":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        channel_id = os.getenv("CHANNEL_ID", "").strip()
        database_url = os.getenv("DATABASE_URL", "").strip()

        if not bot_token:
            logger.error("BOT_TOKEN is not set in .env")
            raise RuntimeError("BOT_TOKEN is required")
        if not channel_id:
            logger.error("CHANNEL_ID is not set in .env")
            raise RuntimeError("CHANNEL_ID is required")
        if not database_url:
            logger.error("DATABASE_URL is not set in .env")
            raise RuntimeError("DATABASE_URL is required")

        return cls(
            BOT_TOKEN=bot_token,
            CHANNEL_ID=channel_id,
            DATABASE_URL=database_url,
        )


config = Config.from_env()