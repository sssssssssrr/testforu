from typing import List, Dict, Tuple, Optional, Any
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from utils import validate_button_url
import logging
import asyncio

# Импорт сервиса для сохранения длинных callback'ов
from services import callback_store  # type: ignore

logger = logging.getLogger(__name__)

KeyboardType = List[List[Dict[str, Any]]]

MAX_CALLBACK_BYTES = 64

async def build_inline_markup(keyboard: KeyboardType) -> InlineKeyboardMarkup:
    """
    Асинхронно строит InlineKeyboardMarkup из структуры keyboard.
    Если callback_data длиннее 64 байт, сохраняет оригинал в БД и заменяет callback_data на 'kb_payload:<id>'.
    Кнопки с 'url' оставляются как url-кнопки.
    """
    rows = []
    for row in (keyboard or []):
        buttons = []
        for btn in row:
            text = btn.get("text", "Button") if isinstance(btn, dict) else str(btn)
            cb = None
            url = None
            if isinstance(btn, dict):
                cb = btn.get("callback_data") or btn.get("callback") or btn.get("data")
                url = btn.get("url")
            # нормализуем url, если есть
            if url:
                ok, norm = validate_button_url(url)
                if not ok:
                    # если URL некорректен — логируем и пропускаем url
                    logger.warning("Invalid URL in keyboard button, using no-url fallback: %s", url)
                    url = None
                else:
                    url = norm
            if url:
                buttons.append(InlineKeyboardButton(text=text, url=url))
            elif cb:
                try:
                    cb_bytes = len(str(cb).encode("utf-8"))
                except Exception:
                    cb_bytes = len(str(cb))
                if cb_bytes <= MAX_CALLBACK_BYTES:
                    buttons.append(InlineKeyboardButton(text=text, callback_data=str(cb)))
                else:
                    # Сохраняем длинный callback в БД и подставляем короткую ссылку
                    try:
                        payload_id = await callback_store.store_payload({"callback": str(cb)})
                        short_cb = f"kb_payload:{payload_id}"
                        buttons.append(InlineKeyboardButton(text=text, callback_data=short_cb))
                    except Exception:
                        logger.exception("Failed to store long callback payload; fallback to truncated callback")
                        # fallback: обрезаем безопасно по байтам
                        truncated = str(cb).encode("utf-8")[:MAX_CALLBACK_BYTES].decode("utf-8", errors="ignore")
                        buttons.append(InlineKeyboardButton(text=text, callback_data=truncated))
            else:
                # fallback: короткий noop callback
                fb = f"btn:{text[:20]}"
                if len(fb.encode("utf-8")) > MAX_CALLBACK_BYTES:
                    fb = fb.encode("utf-8")[:MAX_CALLBACK_BYTES].decode("utf-8", errors="ignore")
                buttons.append(InlineKeyboardButton(text=text, callback_data=fb))
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows, row_width=1)


def validate_keyboard_structure(keyboard: KeyboardType) -> Tuple[bool, str]:
    """
    Validate structure and urls / callback_data.
    Normalizes valid URLs in-place.
    Accepts keyboard as list of rows; each row is list of dicts.
    Each button must have "text" and at least one of ("url" or "callback_data").
    callback_data byte length <= 64 (Telegram limit).
    """
    if keyboard is None:
        return True, "OK"
    if not isinstance(keyboard, list):
        return False, "Keyboard must be a list of rows"
    for ridx, row in enumerate(keyboard):
        if not isinstance(row, list):
            return False, f"Row {ridx} must be a list"
        for cidx, btn in enumerate(row):
            if not isinstance(btn, dict):
                return False, f"Button at {ridx}:{cidx} must be a dict"
            if "text" not in btn:
                return False, f"Button at {ridx}:{cidx} missing text"
            has_url = "url" in btn and bool(btn.get("url"))
            has_cb = ("callback_data" in btn and bool(btn.get("callback_data"))) or ("callback" in btn and bool(btn.get("callback")))
            if not (has_url or has_cb):
                return False, f"Button at {ridx}:{cidx} must have url or callback_data"
            if has_url:
                ok, norm = validate_button_url(btn["url"])
                if not ok:
                    return False, f"Button at {ridx}:{cidx} has invalid url"
                btn["url"] = norm  # normalize in-place
            if has_cb:
                cb_val = btn.get("callback_data") or btn.get("callback") or ""
                try:
                    if len(str(cb_val).encode("utf-8")) > MAX_CALLBACK_BYTES:
                        return False, f"callback_data at {ridx}:{cidx} too long (max 64 bytes)"
                except Exception:
                    if len(str(cb_val)) > MAX_CALLBACK_BYTES:
                        return False, f"callback_data at {ridx}:{cidx} too long (max 64 bytes)"
    return True, "OK"


# Helper operations for editing keyboard structure (unchanged)
def add_row(keyboard: KeyboardType) -> KeyboardType:
    keyboard = keyboard or []
    keyboard.append([])
    return keyboard

def add_button_to_row(
    keyboard: KeyboardType,
    row_index: Optional[int],
    text: str,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
) -> KeyboardType:
    keyboard = keyboard or []
    btn: Dict[str, Any] = {"text": text}
    if callback_data:
        btn["callback_data"] = callback_data
    if url:
        btn["url"] = url
    if row_index is None or row_index < 0 or row_index >= len(keyboard):
        keyboard.append([btn])
    else:
        keyboard[row_index].append(btn)
    return keyboard

def delete_button(keyboard: KeyboardType, row_index: int, col_index: int) -> KeyboardType:
    if not keyboard:
        return keyboard
    if 0 <= row_index < len(keyboard):
        row = keyboard[row_index]
        if 0 <= col_index < len(row):
            row.pop(col_index)
            if not row:
                keyboard.pop(row_index)
    return keyboard

def move_button(keyboard: KeyboardType, from_r: int, from_c: int, to_r: int, to_c: int) -> KeyboardType:
    if not keyboard:
        return keyboard
    if not (0 <= from_r < len(keyboard)):
        return keyboard
    row = keyboard[from_r]
    if not (0 <= from_c < len(row)):
        return keyboard
    btn = row.pop(from_c)
    if not row:
        keyboard.pop(from_r)
        if from_r < to_r:
            to_r -= 1
    while len(keyboard) <= to_r:
        keyboard.append([])
    target_row = keyboard[to_r]
    if to_c < 0:
        to_c = 0
    if to_c > len(target_row):
        to_c = len(target_row)
    target_row.insert(to_c, btn)
    return keyboard

def reformat_columns(keyboard: KeyboardType, cols: int) -> KeyboardType:
    if not keyboard:
        return keyboard
    flat = [b for r in keyboard for b in r]
    if cols <= 0:
        cols = 1
    new: KeyboardType = []
    for i in range(0, len(flat), cols):
        new.append(flat[i:i + cols])
    return new