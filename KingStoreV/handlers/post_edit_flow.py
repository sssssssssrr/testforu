import re
import json
import logging
from typing import Optional, Any

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.exceptions import TelegramBadRequest

from services.post_service import (
    get_post_row_by_id,
    get_post_row_by_chat_message,
    update_post_text,
    update_post_photo,
    update_post_keyboard,
)
from services import keyboard as keyboard_service

logger = logging.getLogger(__name__)
router = Router()

RE_PUBLIC = re.compile(r"https?://t\.me/([^/]+)/(\d+)")
RE_PRIVATE = re.compile(r"https?://t\.me/c/(\d+)/(\d+)")

def parse_post_link(link: str) -> (Optional[str], Optional[int]):
    m = RE_PUBLIC.search(link)
    if m:
        username, msg_id = m.group(1), m.group(2)
        return f"@{username}", int(msg_id)
    m = RE_PRIVATE.search(link)
    if m:
        channel_part, msg_id = m.group(1), m.group(2)
        return int(f"-100{channel_part}"), int(msg_id)
    return None, None

# FSM states for the keyboard editor
class KBStates(StatesGroup):
    waiting_row_index_for_add = State()
    waiting_button_text_url = State()
    waiting_delete_coords = State()
    waiting_edit_coords = State()
    waiting_new_button_text_url = State()
    waiting_move_source = State()
    waiting_move_target = State()
    waiting_format_cols = State()

# Per-user in-memory editing sessions.
# Each session keeps original values (orig_*) and working copy (text, photo_file_id, keyboard)
# session = {
#   "post_id": Optional[int],
#   "chat_id": str,
#   "message_id": int,
#   "orig_text": Optional[str],
#   "orig_photo_file_id": Optional[str],
#   "orig_keyboard": list,
#   "text": Optional[str],
#   "photo_file_id": Optional[str],
#   "keyboard": list,
#   "awaiting": Optional[str],  # "text" | "photo" | None
#   "keyboard_staged": bool,
# }
_edit_sessions: dict[int, dict] = {}

# UI helpers
def _main_edit_menu(post_id_str: str, chat_id: str, message_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data=f"editpost|text|{post_id_str}|{chat_id}|{message_id}")],
        [InlineKeyboardButton(text="🖼️ Заменить фото", callback_data=f"editpost|photo|{post_id_str}|{chat_id}|{message_id}")],
        [InlineKeyboardButton(text="🔧 Редактировать клавиатуру", callback_data=f"editpost|keyboard|{post_id_str}|{chat_id}|{message_id}")],
        [InlineKeyboardButton(text="👁️ Предпросмотр (стейдж)", callback_data=f"editpost|preview|{post_id_str}|{chat_id}|{message_id}")],
        [InlineKeyboardButton(text="✅ Применить текст", callback_data=f"editpost|apply_text|{post_id_str}|{chat_id}|{message_id}"),
         InlineKeyboardButton(text="✅ Применить фото", callback_data=f"editpost|apply_photo|{post_id_str}|{chat_id}|{message_id}")],
        [InlineKeyboardButton(text="✅ Применить клавиатуру", callback_data=f"editpost|apply_keyboard|{post_id_str}|{chat_id}|{message_id}")],
        [InlineKeyboardButton(text="🚀 Применить все изменения", callback_data=f"editpost|apply_all|{post_id_str}|{chat_id}|{message_id}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="editpost|cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows, row_width=2)

def _kb_editor_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Добавить строку", callback_data="kbeditor|add_row")],
        [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="kbeditor|add_button")],
        [InlineKeyboardButton(text="🗑️ Удалить кнопку", callback_data="kbeditor|del_button")],
        [InlineKeyboardButton(text="✏️ Редактировать кнопку", callback_data="kbeditor|edit_button")],
        [InlineKeyboardButton(text="🔀 Переместить кнопку", callback_data="kbeditor|move_button")],
        [InlineKeyboardButton(text="▦ Формат в N колонок", callback_data="kbeditor|format")],
        [InlineKeyboardButton(text="👁️ Предпросмотр (стейдж)", callback_data="kbeditor|preview")],
        # stage saves keyboard in session; apply_keyboard applies it to live message
        [InlineKeyboardButton(text="💾 Сохранить в сессии (стейдж)", callback_data="kbeditor|stage")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="kbeditor|back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows, row_width=1)

def _render_keyboard_summary(kb: list) -> str:
    if not kb:
        return "(пустая клавиатура)"
    lines = []
    for r_idx, row in enumerate(kb):
        texts = []
        for c_idx, btn in enumerate(row):
            label = btn.get("text", "(no text)")
            texts.append(f"[{c_idx}] {label}")
        lines.append(f"Row {r_idx}: " + " | ".join(texts))
    return "\n".join(lines)

# --- Command to start editing a published post ---
@router.message(Command("editpost"))
async def cmd_editpost(message: types.Message):
    """
    /editpost <post_id|t.me/username/123|t.me/c/...>
    Initialize editing session (staged changes).
    """
    arg = message.text.partition(" ")[2].strip()
    if not arg:
        await message.reply("Использование: /editpost <post_id|t.me/username/123|t.me/c/...>")
        return

    post = None
    chat_id = None
    message_id = None

    # Try numeric id
    try:
        pid = int(arg)
        post = await get_post_row_by_id(pid)
        if post:
            chat_id = post.get("published_channel") or post.get("published_link")
            message_id = post.get("published_message_id")
            if post.get("published_link") and (not chat_id or not message_id):
                ch, mid = parse_post_link(post.get("published_link") or "")
                if ch:
                    chat_id, message_id = ch, mid
    except ValueError:
        ch, mid = parse_post_link(arg)
        if ch:
            post = await get_post_row_by_chat_message(ch, mid)
            chat_id, message_id = ch, mid

    if not chat_id or not message_id:
        await message.reply("Не удалось определить, где опубликован пост. Убедитесь, что в БД заполнены published_channel и published_message_id, либо передайте ссылку.")
        return

    # Check bot rights (basic)
    try:
        me = await message.bot.get_me()
        cm = await message.bot.get_chat_member(chat_id=chat_id, user_id=me.id)
        status = getattr(cm, "status", "")
        if status not in ("administrator", "creator"):
            await message.reply("Бот не является администратором/создателем в этом чате — редактирование может быть недоступно.")
            return
    except Exception as e:
        await message.reply(f"Ошибка при проверке прав бота: {e}")
        return

    # Init session with originals and editable copy - DO NOT edit the live message now.
    uid = message.from_user.id
    kb_orig = post.get("keyboard") if post and post.get("keyboard") else []
    text_orig = post.get("text") if post else None
    photo_orig = post.get("photo_file_id") if post else None

    _edit_sessions[uid] = {
        "post_id": post.get("id") if post else None,
        "chat_id": chat_id,
        "message_id": int(message_id),
        "orig_text": text_orig,
        "orig_photo_file_id": photo_orig,
        "orig_keyboard": json.loads(json.dumps(kb_orig)),
        # working copy (staged)
        "text": text_orig,
        "photo_file_id": photo_orig,
        "keyboard": json.loads(json.dumps(kb_orig)),
        "awaiting": None,
        "keyboard_staged": False,
    }

    post_id_str = str(post["id"]) if post and post.get("id") else ""
    summary = (
        f"Текст: {(_edit_sessions[uid]['text'][:200] + '...') if _edit_sessions[uid]['text'] and len(_edit_sessions[uid]['text'])>200 else (_edit_sessions[uid]['text'] or '(пустой)')}\n\n"
        f"Клавиатура:\n{_render_keyboard_summary(_edit_sessions[uid]['keyboard'])}\n\n"
        f"Фото: {'есть' if _edit_sessions[uid]['photo_file_id'] else 'нет'}"
    )
    await message.reply("Открыт редактор опубликованного поста (изменения будут применены только после подтверждения).\n\n" + summary, reply_markup=_main_edit_menu(post_id_str, chat_id, int(message_id)))

# --- Main edit menu callbacks (staging mode with per-field apply) ---
@router.callback_query(lambda c: bool(c.data) and c.data.startswith("editpost|"))
async def cb_editpost_main(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("|")
    if len(parts) >= 2 and parts[1] == "cancel":
        _edit_sessions.pop(callback.from_user.id, None)
        try:
            await callback.message.edit_text("Редактирование отменено.")
        except Exception:
            pass
        await callback.answer()
        return

    if len(parts) < 5:
        await callback.answer("Неверные данные.")
        return

    action = parts[1]
    post_id = int(parts[2]) if parts[2] else None
    chat_id = parts[3]
    message_id = int(parts[4])

    uid = callback.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        sess = {
            "post_id": post_id, "chat_id": chat_id, "message_id": message_id,
            "orig_text": None, "orig_photo_file_id": None, "orig_keyboard": [],
            "text": None, "photo_file_id": None, "keyboard": [], "awaiting": None, "keyboard_staged": False
        }
        _edit_sessions[uid] = sess

    await callback.answer()

    if action == "text":
        sess["awaiting"] = "text"
        logger.info("User %s awaiting=text for post_id=%s", uid, sess.get("post_id"))
        try:
            await callback.bot.send_message(uid, "Отправьте новый текст для сообщения (HTML разрешён). Это изменение сохранится в сессии. Используйте 'Предпросмотр' и затем 'Применить текст' или 'Применить все изменения' для публикации.")
            await callback.answer("Проверьте личные сообщения бота.")
        except Exception:
            try:
                await callback.message.answer("Отправьте новый текст для сообщения (HTML разрешён). Это изменение сохранится в сессии. Используйте 'Предпросмотр' и затем 'Применить текст' или 'Применить все изменения' для публикации.")
            except Exception:
                pass
            await callback.answer()
        return

    if action == "photo":
        sess["awaiting"] = "photo"
        logger.info("User %s awaiting=photo for post_id=%s", uid, sess.get("post_id"))
        try:
            await callback.bot.send_message(uid, "Пришлите новое фото (файл/фото). Это изменение сохранится в сессии. После сохранения используйте 'Применить фото' или 'Применить все изменения'.")
            await callback.answer("Проверьте личные сообщения бота.")
        except Exception:
            try:
                await callback.message.answer("Пришлите новое фото (файл/фото). Это изменение сохранится в сессии. После сохранения используйте 'Применить фото' или 'Применить все изменения'.")
            except Exception:
                pass
            await callback.answer()
        return

    if action == "keyboard":
        kb_summary = _render_keyboard_summary(sess.get("keyboard", []))
        try:
            await callback.message.edit_text("Редактор клавиатуры открыт (изменения сохраняются в сессии и не применяются до подтверждения).\n\nТекущее состояние:\n" + kb_summary, reply_markup=_kb_editor_menu())
        except Exception:
            await callback.message.answer("Редактор клавиатуры открыт.\n\nТекущее состояние:\n" + kb_summary, reply_markup=_kb_editor_menu())
        await callback.answer()
        return

    if action == "preview":
        # Send preview using staged values in session (no changes applied yet)
        try:
            kb = sess.get("keyboard") or []
            if kb:
                ok, msg = keyboard_service.validate_keyboard_structure(kb)
                if not ok:
                    await callback.answer(f"Ошибка клавиатуры: {msg}")
                    return
                markup = keyboard_service.build_inline_markup(kb)
            else:
                markup = None
            final_text = sess.get("text") or ""
            photo = sess.get("photo_file_id")
            if photo:
                caption = final_text if final_text else ""
                await callback.message.answer_photo(photo=photo, caption=caption, reply_markup=markup)
            else:
                await callback.message.answer(final_text or "(пустой пост)", reply_markup=markup)
            await callback.answer("Предпросмотр (стейдж) отправлен.")
        except Exception as e:
            logger.exception("preview failed: %s", e)
            await callback.answer(f"Ошибка при предпросмотре: {e}")
        return

    # APPLY TEXT only
    if action == "apply_text":
        text = sess.get("text")
        if text is None:
            await callback.answer("В сессии нет нового текста. Сначала отредактируйте текст.")
            return
        try:
            # Try edit_text; if message is media, fallback to edit_caption
            try:
                await callback.bot.edit_message_text(text=text if text != "" else "\u200b", chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            except TelegramBadRequest:
                await callback.bot.edit_message_caption(caption=text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
        except TelegramBadRequest as e:
            await callback.answer(f"Ошибка при применении текста: {e}")
            return
        # update DB and session snapshot
        post_id = sess.get("post_id")
        if post_id:
            try:
                await update_post_text(post_id, text)
            except Exception:
                logger.exception("Failed to update_post_text for post_id=%s", post_id)
        sess["orig_text"] = text
        await callback.answer("Текст применён.")
        try:
            await callback.message.edit_text("Текст применён к опубликованному посту.", reply_markup=_main_edit_menu(str(sess.get("post_id") or ""), sess["chat_id"], sess["message_id"]))
        except Exception:
            pass
        return

    # APPLY PHOTO only
    if action == "apply_photo":
        photo = sess.get("photo_file_id")
        if photo is None:
            await callback.answer("В сессии нет нового фото. Сначала пришлите фото.")
            return
        # use staged text if exists to preserve caption, otherwise use orig_text
        caption_text = sess.get("text") if sess.get("text") is not None else sess.get("orig_text") or ""
        try:
            media = InputMediaPhoto(media=photo, caption=caption_text if caption_text != "" else None)
            await callback.bot.edit_message_media(media=media, chat_id=chat_id, message_id=message_id)
        except TelegramBadRequest as e:
            await callback.answer(f"Ошибка при применении фото: {e}")
            return
        # update DB and session snapshot
        post_id = sess.get("post_id")
        if post_id:
            try:
                await update_post_photo(post_id, photo)
            except Exception:
                logger.exception("Failed to update_post_photo for post_id=%s", post_id)
        sess["orig_photo_file_id"] = photo
        await callback.answer("Фото применено.")
        try:
            await callback.message.edit_text("Фото применено к опубликованному посту.", reply_markup=_main_edit_menu(str(sess.get("post_id") or ""), sess["chat_id"], sess["message_id"]))
        except Exception:
            pass
        return

    # APPLY KEYBOARD only
    if action == "apply_keyboard":
        kb = sess.get("keyboard") or []
        try:
            markup = keyboard_service.build_inline_markup(kb) if kb else None
            await callback.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except TelegramBadRequest as e:
            await callback.answer(f"Ошибка при применении клавиатуры: {e}")
            return
        post_id = sess.get("post_id")
        if post_id:
            try:
                await update_post_keyboard(post_id, kb)
            except Exception:
                logger.exception("Failed to update_post_keyboard for post_id=%s", post_id)
        sess["orig_keyboard"] = json.loads(json.dumps(kb))
        sess["keyboard_staged"] = False
        await callback.answer("Клавиатура применена.")
        try:
            await callback.message.edit_text("Клавиатура применена к опубликованному посту.", reply_markup=_main_edit_menu(str(sess.get("post_id") or ""), sess["chat_id"], sess["message_id"]))
        except Exception:
            pass
        return

    # APPLY ALL (media->text->keyboard)
    if action == "apply_all":
        kb = sess.get("keyboard") or []
        text = sess.get("text")
        photo = sess.get("photo_file_id")
        try:
            # 1) replace media if changed
            if sess.get("orig_photo_file_id") != photo:
                if photo:
                    try:
                        media = InputMediaPhoto(media=photo, caption=None)
                        # we will set caption later as separate step
                        await callback.bot.edit_message_media(media=media, chat_id=chat_id, message_id=message_id)
                    except TelegramBadRequest as e:
                        await callback.answer(f"Не удалось заменить медиа: {e}")
                        return
                else:
                    await callback.answer("Удаление медиа не поддерживается автоматически.")
                    return
            # 2) update text/caption if changed
            if sess.get("orig_text") != text:
                try:
                    if text is not None:
                        send_text = text if text != "" else "\u200b"
                        try:
                            await callback.bot.edit_message_text(text=send_text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
                        except TelegramBadRequest:
                            await callback.bot.edit_message_caption(caption=text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
                except TelegramBadRequest as e:
                    await callback.answer(f"Не удалось обновить текст/подпись: {e}")
                    return
            # 3) update keyboard
            try:
                markup = keyboard_service.build_inline_markup(kb) if kb else None
                await callback.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
            except TelegramBadRequest as e:
                await callback.answer(f"Не удалось обновить клавиатуру: {e}")
                return
            # update DB on success
            post_id = sess.get("post_id")
            if post_id:
                try:
                    if sess.get("orig_text") != text:
                        await update_post_text(post_id, text)
                    if sess.get("orig_photo_file_id") != photo and photo:
                        await update_post_photo(post_id, photo)
                    await update_post_keyboard(post_id, kb)
                except Exception:
                    logger.exception("Failed to update DB after apply_all for post_id=%s", post_id)
            # update snapshots
            sess["orig_text"] = text
            sess["orig_photo_file_id"] = photo
            sess["orig_keyboard"] = json.loads(json.dumps(kb))
            sess["keyboard_staged"] = False
            try:
                await callback.message.edit_text("Все изменения применены к опубликованному посту.")
            except Exception:
                pass
            _edit_sessions.pop(uid, None)
            await callback.answer("Изменения применены.")
        except Exception as e:
            logger.exception("apply_all unexpected error: %s", e)
            await callback.answer(f"Ошибка при применении изменений: {e}")
        return

# --- Keyboard editor callbacks (staged changes only) ---
@router.callback_query(lambda c: bool(c.data) and c.data.startswith("kbeditor|"))
async def cb_kbeditor_actions(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        await callback.answer("Сессия редактора не найдена. Запустите /editpost.")
        return

    action = callback.data.split("|", 1)[1]
    await callback.answer()

    if action == "add_row":
        sess["keyboard"] = keyboard_service.add_row(sess["keyboard"])
        await _safe_edit_or_send(callback.message, "Добавлена новая строка (сохранено в сессии).\n\n" + _render_keyboard_summary(sess["keyboard"]), reply_markup=_kb_editor_menu())
        return

    if action == "add_button":
        await callback.message.answer("Укажите индекс строки (0..n-1) для добавления кнопки, или -1 для новой строки.")
        await state.set_state(KBStates.waiting_row_index_for_add)
        return

    if action == "del_button":
        if not sess["keyboard"]:
            await callback.answer("Клавиатура пуста.")
            return
        await callback.message.answer("Отправьте координаты кнопки для удаления в формате: row col (например: 0 1)")
        await state.set_state(KBStates.waiting_delete_coords)
        return

    if action == "edit_button":
        if not sess["keyboard"]:
            await callback.answer("Клавиатура пуста.")
            return
        await callback.message.answer("Отправьте координаты кнопки для редактирования в формате: row col (например: 0 1)")
        await state.set_state(KBStates.waiting_edit_coords)
        return

    if action == "move_button":
        if not sess["keyboard"]:
            await callback.answer("Клавиатура пуста.")
            return
        await callback.message.answer("Отправьте координаты источника в формате: row col (например: 0 1)")
        await state.set_state(KBStates.waiting_move_source)
        return

    if action == "format":
        await callback.message.answer("Введите число колонок (целое > 0), в которые нужно распределить кнопки.")
        await state.set_state(KBStates.waiting_format_cols)
        return

    if action == "preview":
        try:
            ok, msg = keyboard_service.validate_keyboard_structure(sess["keyboard"])
            if not ok:
                await callback.answer(f"Ошибка структуры клавиатуры: {msg}")
                return
            markup = keyboard_service.build_inline_markup(sess["keyboard"])
            await callback.message.answer("Предпросмотр клавиатуры (стейдж):", reply_markup=markup)
            await callback.message.edit_text("Редактор клавиатуры (предпросмотр отправлен). Текущее состояние:\n\n" + _render_keyboard_summary(sess["keyboard"]), reply_markup=_kb_editor_menu())
            await callback.answer()
        except Exception as e:
            logger.exception("kb preview failed: %s", e)
            await callback.answer(f"Ошибка при предпросмотре: {e}")
        return

    if action == "stage":
        # Stage keyboard in session only; do NOT apply to live message or DB now.
        sess["keyboard_staged"] = True
        await callback.answer("Клавиатура сохранена в сессии. Она будет применена при 'Применить клавиатуру' или 'Применить все изменения'.")
        try:
            await callback.message.edit_text("Клавиатура сохранена в сессии (не применена к сообщению). Возврат в меню редактирования.", reply_markup=_main_edit_menu(str(sess.get("post_id") or ""), sess["chat_id"], sess["message_id"]))
        except Exception:
            pass
        return

    if action == "apply":
        await callback.answer("Непосредственное применение пока отключено. Сохраните в сессии и используйте 'Применить клавиатуру' или 'Применить все изменения'.")
        return

    if action == "back":
        post_id_str = str(sess.get("post_id")) if sess.get("post_id") else ""
        await callback.message.edit_text("Возврат в главное меню редактирования.", reply_markup=_main_edit_menu(post_id_str, sess["chat_id"], sess["message_id"]))
        await callback.answer()
        return

# --- Keyboard editor FSM handlers (staged) ---
@router.message(KBStates.waiting_row_index_for_add)
async def kb_row_index_received(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        await message.reply("Сессия не найдена. Запустите /editpost.")
        await state.clear()
        return
    text = (message.text or "").strip()
    try:
        idx = int(text)
    except Exception:
        await message.reply("Ожидался числовой индекс. Попробуйте снова.")
        return
    row_index = None if idx == -1 else idx
    await state.update_data(kb_add_target={"row_index": row_index})
    await message.reply("Отправьте кнопку в формате (две строки):\nТекст кнопки\nURL или callback_data (оставьте пустым, если не нужно).")
    await state.set_state(KBStates.waiting_button_text_url)

@router.message(KBStates.waiting_button_text_url)
async def kb_button_data_received(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        await message.reply("Сессия не найдена.")
        await state.clear()
        return
    data = await state.get_data()
    target = data.get("kb_add_target", {})
    row_index = target.get("row_index")
    parts = (message.text or "").splitlines()
    if not parts:
        await message.reply("Пустой ввод. Попробуйте снова.")
        return
    text_btn = parts[0].strip()
    second = parts[1].strip() if len(parts) > 1 else ""
    if second.startswith(("http://","https://","tg://","mailto:")):
        url = second
        callback_data = None
    else:
        url = None
        callback_data = second or None
    sess["keyboard"] = keyboard_service.add_button_to_row(sess["keyboard"], row_index, text_btn, callback_data, url)
    await message.reply("Кнопка добавлена в сессию.\n\n" + _render_keyboard_summary(sess["keyboard"]), reply_markup=_kb_editor_menu())
    await state.clear()

@router.message(KBStates.waiting_delete_coords)
async def kb_delete_coords(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        await message.reply("Сессия не найдена.")
        await state.clear()
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply("Неверный формат. Используйте: row col")
        return
    try:
        r = int(parts[0]); c = int(parts[1])
    except ValueError:
        await message.reply("Индексы должны быть числами.")
        return
    sess["keyboard"] = keyboard_service.delete_button(sess["keyboard"], r, c)
    await message.reply("Кнопка удалена из сессии (если координаты корректны).\n\n" + _render_keyboard_summary(sess["keyboard"]), reply_markup=_kb_editor_menu())
    await state.clear()

@router.message(KBStates.waiting_edit_coords)
async def kb_edit_coords(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        await message.reply("Сессия не найдена.")
        await state.clear()
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply("Неверный формат. row col")
        return
    try:
        r = int(parts[0]); c = int(parts[1])
    except ValueError:
        await message.reply("Индексы должны быть числами.")
        return
    if r < 0 or r >= len(sess["keyboard"]) or c < 0 or c >= len(sess["keyboard"][r]):
        await message.reply("Координаты вне диапазона.")
        await state.clear()
        return
    await state.update_data(kb_edit_target={"r": r, "c": c})
    await message.reply("Отправьте новую кнопку в формате (две строки):\nТекст кнопки\nURL или callback_data (если нужно).")
    await state.set_state(KBStates.waiting_new_button_text_url)

@router.message(KBStates.waiting_new_button_text_url)
async def kb_new_button_data(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        await message.reply("Сессия не найдена.")
        await state.clear()
        return
    data = await state.get_data()
    target = data.get("kb_edit_target", {})
    r = target.get("r"); c = target.get("c")
    parts = (message.text or "").splitlines()
    if not parts:
        await message.reply("Пустой ввод.")
        return
    text_btn = parts[0].strip()
    second = parts[1].strip() if len(parts) > 1 else ""
    if second.startswith(("http://","https://","tg://","mailto:")):
        url = second
        callback_data = None
    else:
        url = None
        callback_data = second or None
    sess["keyboard"][r][c] = {"text": text_btn}
    if callback_data:
        sess["keyboard"][r][c]["callback_data"] = callback_data
    if url:
        sess["keyboard"][r][c]["url"] = url
    await message.reply("Кнопка обновлена в сессии.\n\n" + _render_keyboard_summary(sess["keyboard"]), reply_markup=_kb_editor_menu())
    await state.clear()

@router.message(KBStates.waiting_move_source)
async def kb_move_source(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        await message.reply("Сессия не найдена.")
        await state.clear()
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply("Неверный формат. row col")
        return
    try:
        r = int(parts[0]); c = int(parts[1])
    except ValueError:
        await message.reply("Индексы должны быть числами.")
        return
    sess["move_source"] = {"r": r, "c": c}
    await message.reply("Теперь отправьте координаты целевой позиции в формате: row col")
    await state.set_state(KBStates.waiting_move_target)

@router.message(KBStates.waiting_move_target)
async def kb_move_target(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        await message.reply("Сессия не найдена.")
        await state.clear()
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply("Неверный формат. row col")
        return
    try:
        tr = int(parts[0]); tc = int(parts[1])
    except ValueError:
        await message.reply("Индексы должны быть числами.")
        return
    src = sess.get("move_source")
    if not src:
        await message.reply("Источник не задан.")
        await state.clear()
        return
    sess["keyboard"] = keyboard_service.move_button(sess["keyboard"], src["r"], src["c"], tr, tc)
    sess.pop("move_source", None)
    await message.reply("Кнопка перемещена в сессии.\n\n" + _render_keyboard_summary(sess["keyboard"]), reply_markup=_kb_editor_menu())
    await state.clear()

@router.message(KBStates.waiting_format_cols)
async def kb_format_cols(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        await message.reply("Сессия не найдена.")
        await state.clear()
        return
    try:
        cols = int((message.text or "").strip())
    except Exception:
        await message.reply("Неверный ввод. Введите число > 0.")
        return
    if cols <= 0:
        await message.reply("Количество колонок должно быть > 0.")
        return
    sess["keyboard"] = keyboard_service.reformat_columns(sess["keyboard"], cols)
    await message.reply(f"Клавиатура отформатирована в {cols} колонок (в сессии).\n\n" + _render_keyboard_summary(sess["keyboard"]), reply_markup=_kb_editor_menu())
    await state.clear()

# --- Catch responses for staged text/photo edits (store in session only) ---
@router.message(lambda message: message.from_user is not None and message.from_user.id in _edit_sessions)
async def catch_edit_responses(message: types.Message):
    uid = message.from_user.id
    sess = _edit_sessions.get(uid)
    if not sess:
        return

    awaiting = sess.get("awaiting")
    if not awaiting:
        return

    logger.info("catch_edit_responses: user=%s awaiting=%s", uid, awaiting)

    # TEXT: store staged text in session (do not edit live message yet)
    if awaiting == "text":
        if not message.text:
            await message.reply("Ожидается текст. Попробуйте снова.")
            return
        new_text = message.text
        sess["text"] = new_text
        sess["awaiting"] = None
        await message.reply("Текст сохранён в сессии. Используйте 'Предпросмотр' -> 'Применить текст' или 'Применить все изменения' для публикации.")
        return

    # PHOTO: store staged photo in session (do not edit live message yet)
    if awaiting == "photo":
        file_id = None
        if message.photo:
            file_id = message.photo[-1].file_id
        elif message.document:
            file_id = message.document.file_id
        elif message.animation:
            file_id = message.animation.file_id

        if not file_id:
            await message.reply("Не удалось получить файл. Пришлите фото или файл ещё раз.")
            return

        sess["photo_file_id"] = file_id
        sess["awaiting"] = None
        await message.reply("Фото сохранено в сессии. Используйте 'Применить фото' или 'Применить все изменения' для публикации.")
        return

# --- Helper: safe edit or send fallback ---
async def _safe_edit_or_send(msg_obj: types.Message, text: str, **kwargs: Any):
    try:
        await msg_obj.edit_text(text, **kwargs)
    except Exception:
        try:
            await msg_obj.answer(text, **kwargs)
        except Exception:
            logger.exception("Failed to edit or send message fallback.")