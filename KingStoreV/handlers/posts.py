from typing import Optional, List, Dict, Any
import json
import logging
import traceback
import html as html_lib

from aiogram import exceptions as aiogram_exceptions
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram import exceptions

from services import posts as posts_service
from services import keyboard as keyboard_service
from services import channels as channels_service
from services import callback_store as callback_store_service
from models import Post
from config import config
from utils import validate_button_url

router = Router()
logger = logging.getLogger(__name__)

# In-memory interactive session storage per user.
_sessions: Dict[int, Dict[str, Any]] = {}


# --- UI helpers ---
def _mk_post_menu() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✏️ Редактировать текст", callback_data="edit_text"),
            InlineKeyboardButton(text="🗑️ Удалить текст", callback_data="delete_text"),
        ],
        [
            InlineKeyboardButton(text="🖼️ Редактировать фото", callback_data="edit_photo"),
            InlineKeyboardButton(text="🗑️ Удалить фото", callback_data="delete_photo"),
        ],
        [
            InlineKeyboardButton(text="🔧 Редактировать клавиатуру", callback_data="edit_keyboard"),
            InlineKeyboardButton(text="👁️ Предпросмотр", callback_data="preview"),
        ],
        [
            InlineKeyboardButton(text="📢 Выбрать канал", callback_data="choose_channel"),
            InlineKeyboardButton(text="💾 Сохранить черновик", callback_data="save_draft"),
        ],
        [
            InlineKeyboardButton(text="🚀 Опубликовать", callback_data="publish"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows, row_width=2)


def _mk_post_edit_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Редактировать текст", callback_data="edit_text")],
        [InlineKeyboardButton(text="Редактировать фото", callback_data="edit_photo")],
        [InlineKeyboardButton(text="Редактировать клавиатуру", callback_data="edit_keyboard")],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows, row_width=1)


def _mk_keyboard_editor_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Добавить строку", callback_data="kb_add_row")],
        [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="kb_add_button")],
        [InlineKeyboardButton(text="🗑️ Удалить кнопку", callback_data="kb_select_delete")],
        [InlineKeyboardButton(text="✏️ Редактировать кнопку", callback_data="kb_select_edit")],
        [InlineKeyboardButton(text="🔀 Переместить кнопку", callback_data="kb_select_move")],
        [InlineKeyboardButton(text="▦ Формат в N колонок", callback_data="kb_format")],
        [InlineKeyboardButton(text="👁️ Предпросмотр клавиатуры", callback_data="kb_preview")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="kb_back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows, row_width=1)


def _mk_preview_options() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🚀 Опубликовать", callback_data="publish")],
        [InlineKeyboardButton(text="💾 Сохранить черновик", callback_data="save_draft")],
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data="edit_post")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows, row_width=1)


def _normalize_empty_for_send(text: Optional[str], photo_file_id: Optional[str]) -> (str, Optional[str]):
    final_text = text or ""
    if not final_text and not photo_file_id:
        final_text = "\u200b"
    return final_text, photo_file_id


# --- Helpers to build markup safely and avoid long callback_data ---

async def _safe_build_markup_and_handle_validation(keyboard: List[List[Dict[str, Any]]]) -> InlineKeyboardMarkup:
    """
    Try validating keyboard; if validation fails only due to "too long" callback_data,
    we still proceed to build using keyboard_service (which will store long payloads and return short callbacks).
    For other validation errors we raise ValueError with message.
    """
    if not keyboard:
        return None
    try:
        ok, msg = keyboard_service.validate_keyboard_structure(keyboard)
    except Exception:
        # If validation itself fails unexpectedly, log and proceed to build (keyboard_service.build_inline_markup is robust)
        logger.exception("Keyboard validation raised exception; proceeding to build markup")
        return await keyboard_service.build_inline_markup(keyboard)

    if ok:
        return await keyboard_service.build_inline_markup(keyboard)

    # If validation failed, inspect message
    if "too long" in (msg or "").lower():
        logger.warning("Keyboard validation flagged long callback_data but builder will store/persist long payloads: %s", msg)
        return await keyboard_service.build_inline_markup(keyboard)

    # other validation error -> raise to caller
    raise ValueError(msg or "Invalid keyboard structure")


# Handler for payload-stored callbacks (kb_payload:<id>)
@router.callback_query(F.data.startswith("kb_payload:"))
async def cb_kb_payload(query: CallbackQuery) -> None:
    """
    Load stored original callback and dispatch for common known action prefixes.
    If you have custom long callback formats, add handling here.
    """
    await query.answer()
    _, payload_id = query.data.split(":", 1)
    try:
        payload = await callback_store_service.get_payload(payload_id)
    except Exception:
        logger.exception("Failed to load payload for id=%s", payload_id)
        await query.answer("Не удалось загрузить данные кнопки.")
        return

    if not payload:
        await query.answer("Данные устарели или не найдены.")
        return
    original = payload.get("callback") or payload.get("data")
    if not original:
        await query.answer("Неверные данные для кнопки.")
        return

    # For common prefix-based actions we know how to forward:
    try:
        if original.startswith("open_draft:"):
            query.data = original
            await cb_open_draft(query)
            return
        if original.startswith("delete_draft:"):
            query.data = original
            await cb_delete_draft(query)
            return
        if original.startswith("select_channel:"):
            query.data = original
            await cb_select_channel(query)
            return
        if original.startswith("delete_channel:"):
            query.data = original
            await cb_delete_channel(query)
            return
    except Exception:
        logger.exception("Error while dispatching stored callback payload")

    # If not handled above, reply with the original payload (best-effort)
    await query.answer("Нажата кнопка: " + str(original))


# --- Commands and handlers ---

async def _report_and_log_telegram_bad_request(query, exc: Exception):
    # Логируем полную трассировку на сервере
    logger.exception("TelegramBadRequest during publish: %s", exc)
    # Попробуем получить текст ошибки, который возвращает aiogram (в разных версиях поле может быть разным)
    err_text = ""
    try:
        err_text = getattr(exc, "message", "") or getattr(exc, "description", "") or str(exc)
    except Exception:
        err_text = str(exc)
    short = err_text if len(err_text) < 300 else err_text[:300] + "..."
    # Сообщаем пользователю кратко и предлагаем шаги
    try:
        await query.answer("Ошибка при отправке: " + short)
    except Exception:
        logger.debug("Failed to answer with error to user")
    # Редактируем сообщение менеджера/меню чтобы показать что произошло
    try:
        await query.message.edit_text(f"Ошибка при публикации: {short}\n\nПодробности в логах.")
    except Exception:
        # Игнорируем невозможность редактировать (сообщение могло быть удалено)
        pass


@router.message(Command("newpost"))
async def cmd_newpost(message: Message) -> None:
    logger.info("cmd_newpost called by %s", message.from_user.id)
    _sessions[message.from_user.id] = {"post": Post(author_id=message.from_user.id), "state": "await_text"}
    hint = (
        "Создаём новый черновик.\n\n"
        "Отправьте текст поста (или /cancel).\n\n"
        "Подсказки:\n"
        "- Пост без текста: не отправляйте текст и нажмите «Предпросмотр» → «Опубликовать» или используйте кнопку «Удалить текст».\n"
        "- Пост только с фото: отправьте фото и не добавляйте текст.\n"
        "- Полностью пустой пост: нажмите «Предпросмотр» → «Опубликовать».\n"
        "- Чтобы удалить текст/фото — используйте кнопки «Удалить текст» / «Удалить фото».\n"
        "- Для добавления канала используйте /addchannel. Список — /channels.\n"
    )
    await message.answer(hint)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    logger.info("cmd_cancel called by %s", message.from_user.id)
    if message.from_user.id in _sessions:
        _sessions.pop(message.from_user.id, None)
        await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer("У вас нет активных операций.")


@router.message(F.photo)
async def handle_photo(message: Message) -> None:
    user_id = message.from_user.id
    session = _sessions.get(user_id)
    if not session:
        return
    post: Post = session["post"]
    post.photo_file_id = message.photo[-1].file_id
    session["state"] = "idle"
    await message.answer("Фото сохранено в черновике.", reply_markup=_mk_post_menu())


# IMPORTANT: ignore commands at routing level so /addchannel etc. aren't swallowed.
# Only handle plain text messages that are not commands.
@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_and_state(message: Message) -> None:
    user = message.from_user
    session = _sessions.get(user.id)
    if not session:
        return
    state: str = session.get("state", "idle")
    post: Post = session["post"]

    # Channel addition flow
    if state == "await_new_channel":
        lines = (message.text or "").splitlines()
        if not lines:
            await message.answer("Пустой ввод. Попробуйте снова.")
            return
        chat_id = lines[0].strip()
        title = lines[1].strip() if len(lines) > 1 else None
        existing = await channels_service.get_channel_by_chat_id(chat_id)
        if existing:
            await message.answer("Такой канал уже добавлен.")
        else:
            await channels_service.create_channel(chat_id, title, message.from_user.id)
            await message.answer(f"Канал {chat_id} сохранён.")
        session["state"] = "idle"
        return

    # Keyboard flows
    if state == "await_kb_row_index_for_add":
        text = (message.text or "").strip()
        try:
            row_index = int(text)
        except Exception:
            await message.answer("Ожидался числовой индекс строки. Попробуйте снова.")
            return
        session["kb_edit_target"] = {"row_index": None if row_index == -1 else row_index}
        session["state"] = "await_button_text_url"
        await message.answer("Теперь отправьте кнопку в формате:\nТекст кнопки\nhttps://example.com или https://t.me/channel/123")
        return

    if state == "await_button_text_url":
        parts = (message.text or "").splitlines()
        if len(parts) < 2:
            await message.answer("Неверный формат. Требуется две строки:\nТекст кнопки\nURL")
            return
        btn_text = parts[0].strip()
        btn_url = parts[1].strip()
        ok, norm = validate_button_url(btn_url)
        if not ok:
            await message.answer("Неверный URL кнопки.")
            return
        row_index = session.get("kb_edit_target", {}).get("row_index")
        post.keyboard = keyboard_service.add_button_to_row(post.keyboard, row_index, btn_text, url=norm)
        session["state"] = "idle"
        session.pop("kb_edit_target", None)
        await message.answer("Кнопка добавлена.", reply_markup=_mk_post_menu())
        return

    if state == "await_delete_coords":
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Неверный формат. Используйте: row col")
            return
        try:
            r = int(parts[0]); c = int(parts[1])
        except ValueError:
            await message.answer("Индексы должны быть числами.")
            return
        post.keyboard = keyboard_service.delete_button(post.keyboard, r, c)
        session["state"] = "idle"
        await message.answer("Кнопка удалена (если координаты корректны).", reply_markup=_mk_post_menu())
        return

    if state == "await_edit_coords":
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Неверный формат. row col")
            return
        try:
            r = int(parts[0]); c = int(parts[1])
        except ValueError:
            await message.answer("Индексы должны быть числами.")
            return
        if r < 0 or r >= len(post.keyboard) or c < 0 or c >= len(post.keyboard[r]):
            session["state"] = "idle"
            await message.answer("Координаты вне диапазона.")
            return
        session["kb_edit_target"] = {"row_index": r, "col_index": c}
        session["state"] = "await_new_button_text_url"
        await message.answer("Отправьте новую кнопку в формате:\nТекст кнопки\nhttps://example.com")
        return

    if state == "await_new_button_text_url":
        parts = (message.text or "").splitlines()
        if len(parts) < 2:
            await message.answer("Неверный формат. Требуется текст и URL.")
            return
        text_btn = parts[0].strip()
        url_btn = parts[1].strip()
        ok, norm = validate_button_url(url_btn)
        if not ok:
            await message.answer("Неверный URL.")
            return
        target = session.get("kb_edit_target", {})
        r = target.get("row_index"); c = target.get("col_index")
        post.keyboard[r][c] = {"text": text_btn, "url": norm}
        session["state"] = "idle"
        session.pop("kb_edit_target", None)
        await message.answer("Кнопка обновлена.", reply_markup=_mk_post_menu())
        return

    if state == "await_move_source":
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Неверный формат. row col")
            return
        try:
            r = int(parts[0]); c = int(parts[1])
        except ValueError:
            await message.answer("Индексы должны быть числами.")
            return
        session["move_source"] = {"r": r, "c": c}
        session["state"] = "await_move_target"
        await message.answer("Теперь отправьте координаты целевой позиции в формате: row col")
        return

    if state == "await_move_target":
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Неверный формат. row col")
            return
        try:
            tr = int(parts[0]); tc = int(parts[1])
        except ValueError:
            await message.answer("Индексы должны быть числами.")
            return
        src = session.get("move_source")
        if not src:
            session["state"] = "idle"
            await message.answer("Источник не задан.")
            return
        post.keyboard = keyboard_service.move_button(post.keyboard, src["r"], src["c"], tr, tc)
        session["state"] = "idle"
        session.pop("move_source", None)
        await message.answer("Кнопка перемещена.", reply_markup=_mk_post_menu())
        return

    if state == "await_format_cols":
        try:
            cols = int((message.text or "").strip())
        except Exception:
            await message.answer("Неверный ввод. Введите число > 0.")
            return
        if cols <= 0:
            await message.answer("Количество колонок должно быть > 0.")
            return
        post.keyboard = keyboard_service.reformat_columns(post.keyboard, cols)
        session["state"] = "idle"
        await message.answer(f"Клавиатура отформатирована в {cols} колонок.", reply_markup=_mk_post_menu())
        return

    # Text editing flows
    if state == "await_text":
        post.text = message.text or ""
        session["state"] = "idle"
        await message.answer("Текст сохранён.", reply_markup=_mk_post_menu())
        return

    if state == "await_new_text":
        post.text = message.text or ""
        session["state"] = "idle"
        await message.answer("Текст обновлён.", reply_markup=_mk_post_menu())
        return

    # Default idle behaviour: quick text update
    post.text = message.text or ""
    await message.answer("Текст обновлён.", reply_markup=_mk_post_menu())


# Keyboard callbacks
@router.callback_query(F.data.startswith("edit_keyboard"))
async def cb_edit_keyboard(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.setdefault(user.id, {"post": Post(author_id=user.id), "state": "idle"})
    post: Post = session["post"]
    rows = [
        [InlineKeyboardButton(text="➕ Добавить строку", callback_data="kb_add_row")],
        [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="kb_add_button")],
        [InlineKeyboardButton(text="🗑️ Удалить кнопку", callback_data="kb_select_delete")],
        [InlineKeyboardButton(text="✏️ Редактировать кнопку", callback_data="kb_select_edit")],
        [InlineKeyboardButton(text="🔀 Переместить кнопку", callback_data="kb_select_move")],
        [InlineKeyboardButton(text="▦ Форматировать в N колонок", callback_data="kb_format")],
        [InlineKeyboardButton(text="👁️ Предпросмотр клавиатуры", callback_data="kb_preview")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="kb_back")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=rows, row_width=1)
    text_lines = ["Редактор клавиатуры", ""]
    if not post.keyboard:
        text_lines.append("(пустая)")
    else:
        for r_idx, row in enumerate(post.keyboard):
            text_lines.append(f"Row {r_idx}: " + ", ".join([f"[{c_idx}] {btn.get('text', '')}" for c_idx, btn in enumerate(row)]))
    await query.message.edit_text("\n".join(text_lines), reply_markup=markup)


@router.callback_query(F.data.startswith("kb_add_row"))
async def cb_kb_add_row(query: CallbackQuery) -> None:
    await query.answer("Добавлена новая строка.")
    user = query.from_user
    session = _sessions.setdefault(user.id, {"post": Post(author_id=user.id), "state": "idle"})
    post: Post = session["post"]
    post.keyboard = keyboard_service.add_row(post.keyboard)
    await query.message.edit_text("Новая строка добавлена.", reply_markup=_mk_post_menu())


@router.callback_query(F.data.startswith("kb_add_button"))
async def cb_kb_add_button(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.setdefault(user.id, {"post": Post(author_id=user.id), "state": "idle"})
    session["state"] = "await_kb_row_index_for_add"
    await query.message.answer("Укажите индекс строки для добавления кнопки (0..n-1). Отправьте -1 для новой строки.\nЗатем отправьте кнопку в формате:\nТекст\nURL")


@router.callback_query(F.data.startswith("kb_preview"))
async def cb_kb_preview(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    post: Post = session["post"]
    try:
        markup = await _safe_build_markup_and_handle_validation(post.keyboard) if post.keyboard else None
    except ValueError as ve:
        await query.answer(str(ve))
        return
    await query.message.answer("Предпросмотр клавиатуры:", reply_markup=markup)


@router.callback_query(F.data.startswith("kb_select_delete"))
async def cb_kb_select_delete(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    post: Post = session["post"]
    if not post.keyboard:
        await query.answer("Клавиатура пустая")
        return
    session["state"] = "await_delete_coords"
    await query.message.answer("Отправьте координаты кнопки для удаления: row col (например: 0 1)")


@router.callback_query(F.data.startswith("kb_select_edit"))
async def cb_kb_select_edit(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    post: Post = session["post"]
    if not post.keyboard:
        await query.answer("Клавиатура пустая")
        return
    session["state"] = "await_edit_coords"
    await query.message.answer("Отправьте координаты кнопки для редактирования: row col (например: 0 1)")


@router.callback_query(F.data.startswith("kb_select_move"))
async def cb_kb_select_move(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id) or {"post": Post(author_id=user.id), "state": "idle"}
    _sessions[user.id] = session
    post: Post = session["post"]
    if not post.keyboard:
        await query.answer("Клавиатура пустая")
        return
    session["state"] = "await_move_source"
    await query.message.answer("Отправьте координаты источника: row col (например: 0 1)")


@router.callback_query(F.data.startswith("kb_format"))
async def cb_kb_format(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.setdefault(user.id, {"post": Post(author_id=user.id), "state": "idle"})
    session["state"] = "await_format_cols"
    await query.message.answer("Отправьте желаемое количество колонок (число > 0), например: 2")


@router.callback_query(F.data.startswith("kb_back"))
async def cb_kb_back(query: CallbackQuery) -> None:
    await query.answer()
    await query.message.edit_text("Вернулись в меню поста.", reply_markup=_mk_post_menu())


# Preview / Save / Publish
@router.callback_query(F.data.startswith("preview"))
async def cb_preview(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    post: Post = session["post"]
    try:
        markup = await _safe_build_markup_and_handle_validation(post.keyboard) if post.keyboard else None
    except ValueError as ve:
        await query.answer(str(ve))
        return

    try:
        final_text, photo = _normalize_empty_for_send(post.text, post.photo_file_id)
        if photo:
            caption = final_text if final_text != "\u200b" else ""
            await bot.send_photo(chat_id=user.id, photo=photo, caption=caption, reply_markup=markup)
        else:
            await bot.send_message(chat_id=user.id, text=(final_text if final_text != "\u200b" else ""), reply_markup=markup)
    except exceptions.TelegramBadRequest:
        logger.exception("Failed preview for user %s", user.id)
        await query.answer("Не удалось отправить предпросмотр (возможно, бот не может писать вам).")
        return
    await query.message.edit_text("Предпросмотр отправлен вам в ЛС.", reply_markup=_mk_preview_options())


@router.callback_query(F.data.startswith("save_draft"))
async def cb_save_draft(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    post: Post = session["post"]
    try:
        keyboard_json = json.dumps(post.keyboard or [])
    except Exception:
        keyboard_json = "[]"
    if post.id:
        await posts_service.update_post(post.id,
                                        text=post.text,
                                        photo_file_id=post.photo_file_id,
                                        keyboard_json=keyboard_json,
                                        status=post.status)
        updated = await posts_service.get_post(post.id)
        session["post"] = updated
    else:
        new = await posts_service.create_post(post)
        session["post"] = new
    await query.message.edit_text(f"Черновик сохранён (id={session['post'].id}).", reply_markup=_mk_post_menu())


@router.callback_query(F.data.startswith("publish"))
async def cb_publish(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    post: Post = session["post"]

    # выбор канала: сначала сессия, иначе конфиг
    channel_id = session.get("post_channel") or config.CHANNEL_ID

    try:
        markup = await _safe_build_markup_and_handle_validation(post.keyboard) if post.keyboard else None
    except ValueError as ve:
        await query.answer(str(ve))
        return

    try:
        final_text, photo = _normalize_empty_for_send(post.text, post.photo_file_id)

        # Проверки перед отправкой (быстрые предикаты)
        if photo:
            caption = final_text if final_text != "\u200b" else ""
            if len(caption) > 1024:
                await query.answer("Ошибка: подпись к фото слишком длинная (максимум 1024 символа).")
                return
            res = await bot.send_photo(chat_id=channel_id, photo=photo, caption=caption, reply_markup=markup)
        else:
            text_to_send = final_text
            if len(text_to_send) > 4096:
                await query.answer("Ошибка: текст сообщения слишком длинный (максимум 4096 символов).")
                return
            res = await bot.send_message(chat_id=channel_id, text=text_to_send, reply_markup=markup)

        published_message_id = res.message_id

        # формируем ссылку
        if isinstance(channel_id, str) and channel_id.startswith("@"):
            published_link = f"https://t.me/{channel_id.strip('@')}/{published_message_id}"
        else:
            cid_str = str(channel_id)
            if cid_str.startswith("-100"):
                cid_short = cid_str[4:]
            elif cid_str.startswith("-"):
                cid_short = cid_str.lstrip("-")
            else:
                cid_short = cid_str
            published_link = f"https://t.me/c/{cid_short}/{published_message_id}"

        # Сохраняем статус публикации в БД
        if post.id:
            await posts_service.update_post(post.id, status="published", published_message_id=published_message_id, published_link=published_link, published_channel=channel_id)
        else:
            post.status = "published"
            post.published_message_id = published_message_id
            post.published_link = published_link
            post.published_channel = channel_id
            await posts_service.create_post(post)

        await query.message.edit_text("Пост опубликован.")
    except aiogram_exceptions.TelegramForbiddenError:
        logger.exception("Bot cannot write to the channel. Check bot membership/permissions.")
        await query.answer("Ошибка: бот не является участником канала или не имеет прав отправлять сообщения. Добавьте бота в канал и выдайте права отправки сообщений.")
    except aiogram_exceptions.TelegramBadRequest as ex:
        await _report_and_log_telegram_bad_request(query, ex)
    except Exception as ex:
        logger.exception("Unexpected error during publish: %s", ex)
        await query.answer("Неожиданная ошибка при публикации. Проверьте логи сервера.")


@router.callback_query(F.data.startswith("edit_post"))
async def cb_edit_post(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    await query.message.edit_text("Меню редактирования поста:", reply_markup=_mk_post_edit_menu())


@router.callback_query(F.data.startswith("edit_text"))
async def cb_edit_text(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    session["state"] = "await_new_text"
    await query.message.answer("Отправьте новый текст поста.")


@router.callback_query(F.data.startswith("edit_photo"))
async def cb_edit_photo(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    session["state"] = "await_new_photo"
    await query.message.answer("Отправьте новое фото или /cancel.")


@router.message(F.photo)
async def cb_receive_new_photo(message: Message) -> None:
    user = message.from_user
    session = _sessions.get(user.id)
    if not session or session.get("state") != "await_new_photo":
        return
    post: Post = session["post"]
    post.photo_file_id = message.photo[-1].file_id
    session["state"] = "idle"
    await message.answer("Фото обновлено.", reply_markup=_mk_post_menu())


@router.callback_query(F.data.startswith("delete_text"))
async def cb_delete_text(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    post: Post = session["post"]
    post.text = ""
    await query.message.edit_text("Текст удалён.", reply_markup=_mk_post_menu())


@router.callback_query(F.data.startswith("delete_photo"))
async def cb_delete_photo(query: CallbackQuery) -> None:
    await query.answer()
    user = query.from_user
    session = _sessions.get(user.id)
    if not session:
        await query.answer("Сессия не найдена")
        return
    post: Post = session["post"]
    post.photo_file_id = None
    await query.message.edit_text("Фото удалено.", reply_markup=_mk_post_menu())


# Drafts
@router.message(Command("drafts"))
async def cmd_drafts(message: Message) -> None:
    items = await posts_service.list_posts(author_id=message.from_user.id)
    if not items:
        await message.answer("У вас нет черновиков.")
        return
    text_lines = []
    kb_rows = []
    for p in items:
        snippet = (p.text[:40] + "...") if p.text and len(p.text) > 40 else (p.text or "(пустой)")
        text_lines.append(f"#{p.id} [{p.status}] {snippet}")
        kb_rows.append([InlineKeyboardButton(text=f"Открыть #{p.id}", callback_data=f"open_draft:{p.id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows, row_width=1)
    await message.answer("\n".join(text_lines), reply_markup=kb)


@router.callback_query(F.data.startswith("open_draft:"))
async def cb_open_draft(query: CallbackQuery) -> None:
    await query.answer()
    payload = query.data.split(":", 1)[1]
    try:
        pid = int(payload)
    except Exception:
        await query.answer("Неверный id")
        return
    p = await posts_service.get_post(pid)
    if not p:
        await query.answer("Черновик не найден")
        return
    _sessions[query.from_user.id] = {"post": p, "state": "idle"}
    kb_rows = [
        [InlineKeyboardButton(text="Редактировать текст", callback_data="edit_text")],
        [InlineKeyboardButton(text="Редактировать фото", callback_data="edit_photo")],
        [InlineKeyboardButton(text="Удалить текст", callback_data="delete_text")],
        [InlineKeyboardButton(text="Удалить фото", callback_data="delete_photo")],
        [InlineKeyboardButton(text="Редактировать клавиатуру", callback_data="edit_keyboard")],
        [InlineKeyboardButton(text="Предпросмотр", callback_data="preview")],
        [InlineKeyboardButton(text="Опубликовать", callback_data="publish")],
        [InlineKeyboardButton(text="Удалить черновик", callback_data=f"delete_draft:{p.id}")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows, row_width=1)
    try:
        if p.photo_file_id:
            caption = p.text or ""
            await query.message.answer_photo(photo=p.photo_file_id, caption=caption, reply_markup=kb)
        else:
            await query.message.answer(p.text or "(пустой пост)", reply_markup=kb)
    except exceptions.TelegramBadRequest:
        logger.exception("Failed to show draft preview to user %s", query.from_user.id)
        await query.answer("Не удалось показать черновик (возможно, бот не может писать вам).")


@router.callback_query(F.data.startswith("delete_draft:"))
async def cb_delete_draft(query: CallbackQuery) -> None:
    await query.answer()
    payload = query.data.split(":", 1)[1]
    try:
        pid = int(payload)
    except Exception:
        await query.answer("Неверный id")
        return

    # Удаляем черновик в БД
    await posts_service.delete_post(pid)
    # Пытаемся безопасно отредактировать сообщение-меню
    try:
        await query.message.edit_text(f"Черновик #{pid} удалён.")
    except aiogram_exceptions.TelegramBadRequest as e:
        err = str(e).lower()
        if "no text" in err or "there is no text" in err or "there is no caption" in err:
            try:
                await query.message.edit_caption(f"Черновик #{pid} удалён.")
            except Exception:
                try:
                    await query.message.answer(f"Черновик #{pid} удалён.")
                except Exception:
                    logger.exception("Не удалось ни отредактировать, ни отправить сообщение об удалении черновика.")
        else:
            logger.exception("Unexpected TelegramBadRequest while editing message: %s", e)
            try:
                await query.message.answer(f"Черновик #{pid} удалён.")
            except Exception:
                pass
    except Exception:
        logger.exception("Unexpected error while editing draft-deletion message.")
        try:
            await query.message.answer(f"Черновик #{pid} удалён.")
        except Exception:
            pass


# Channels management
@router.message(Command("channels"))
async def cmd_channels(message: Message) -> None:
    await message.answer("Загружаю список каналов...")
    items = await channels_service.list_channels()
    if not items:
        await message.answer("Список каналов пуст. Добавьте канал командой /addchannel.")
        return
    lines = []
    kb_rows = []
    for ch in items:
        lines.append(f"{ch['id']}: {ch['chat_id']} {('('+str(ch.get('title'))+')') if ch.get('title') else ''}")
        kb_rows.append([
            InlineKeyboardButton(text=f"Выбрать {ch['chat_id']}", callback_data=f"select_channel:{ch['chat_id']}"),
            InlineKeyboardButton(text=f"Удалить {ch['chat_id']}", callback_data=f"delete_channel:{ch['chat_id']}")
        ])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows, row_width=1)
    await message.answer("\n".join(lines), reply_markup=kb)


@router.message(Command("addchannel"))
async def cmd_addchannel(message: Message) -> None:
    session = _sessions.setdefault(message.from_user.id, {"post": Post(author_id=message.from_user.id), "state": "idle"})
    session["state"] = "await_new_channel"
    await message.answer("Отправьте идентификатор канала (например @channelusername или -1001234567890). Можно также указать название на второй строке.\nФормат:\n@channelusername\nНазвание (опционально)")


@router.callback_query(F.data.startswith("choose_channel"))
async def cb_choose_channel(query: CallbackQuery) -> None:
    await query.answer()
    items = await channels_service.list_channels()
    if not items:
        await query.answer("Нет сохранённых каналов. Добавьте через /addchannel")
        return
    rows = []
    for ch in items:
        rows.append([InlineKeyboardButton(text=f"{ch['chat_id']}", callback_data=f"select_channel:{ch['chat_id']}")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows, row_width=1)
    await query.message.answer("Выберите канал для публикации:", reply_markup=markup)


@router.callback_query(F.data.startswith("select_channel:"))
async def cb_select_channel(query: CallbackQuery) -> None:
    await query.answer()
    payload = query.data.split(":", 1)[1]
    user = query.from_user
    session = _sessions.setdefault(user.id, {"post": Post(author_id=user.id), "state": "idle"})
    session["post_channel"] = payload
    await query.message.edit_text(f"Канал {payload} выбран для публикации.", reply_markup=_mk_post_menu())


@router.callback_query(F.data.startswith("delete_channel:"))
async def cb_delete_channel(query: CallbackQuery) -> None:
    await query.answer()
    payload = query.data.split(":", 1)[1]
    await channels_service.delete_channel(payload)
    await query.message.answer(f"Канал {payload} удалён.")