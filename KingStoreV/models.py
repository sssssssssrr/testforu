from dataclasses import dataclass, field
from typing import Optional, List, Any
import json
from datetime import datetime


@dataclass
class Post:
    id: Optional[int] = None
    author_id: int = 0
    text: Optional[str] = None
    photo_file_id: Optional[str] = None
    keyboard: List[List[dict]] = field(default_factory=list)
    status: str = "draft"  # draft or published
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    published_message_id: Optional[int] = None
    published_link: Optional[str] = None
    published_channel: Optional[str] = None  # new: channel where published

    def to_row(self) -> dict:
        now = datetime.utcnow().isoformat()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now
        return {
            "author_id": self.author_id,
            "text": self.text,
            "photo_file_id": self.photo_file_id,
            "keyboard_json": json.dumps(self.keyboard or []),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "published_message_id": self.published_message_id,
            "published_link": self.published_link,
            "published_channel": self.published_channel,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Post":
        if row is None:
            raise ValueError("row is None")
        # sqlite3.Row doesn't have .get — convert to dict first
        try:
            d = dict(row)
        except Exception:
            # fallback: try to build dict from keys (defensive)
            try:
                d = {k: row[k] for k in row.keys()}
            except Exception:
                raise ValueError("Unsupported row type for Post.from_row")
        keyboard = []
        try:
            keyboard = json.loads(d.get("keyboard_json") or "[]")
        except Exception:
            keyboard = []
        return cls(
            id=int(d.get("id")) if d.get("id") is not None else None,
            author_id=int(d.get("author_id") or 0),
            text=d.get("text"),
            photo_file_id=d.get("photo_file_id"),
            keyboard=keyboard,
            status=d.get("status") or "draft",
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            published_message_id=d.get("published_message_id"),
            published_link=d.get("published_link"),
            published_channel=d.get("published_channel"),
        )