import re
from typing import Tuple, Optional
from urllib.parse import urlparse

TELEGRAM_MESSAGE_URL_RE = re.compile(r"^https?://t\.me/([^/]+)/(\d+)$", re.IGNORECASE)


def validate_button_url(url: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that URL is valid HTTP(S) or a Telegram message link (https://t.me/channel/123).
    Returns (is_valid, normalized_url_or_none).
    """
    if not url:
        return False, None
    url = url.strip()
    # Telegram message url
    m = TELEGRAM_MESSAGE_URL_RE.match(url)
    if m:
        normalized = f"https://t.me/{m.group(1)}/{m.group(2)}"
        return True, normalized
    # Generic URL check
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, None
        if not parsed.netloc:
            return False, None
        return True, url
    except Exception:
        return False, None