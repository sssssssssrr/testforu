from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

import re
from services.post_storage import init_posts_table, get_post_by_chat_message, update_reply_markup_by_chat_message

router = Router()

RE_PUBLIC = re.compile(r"https?://t\.me/([^/]+)/(\d+)")
RE_PRIVATE = re.compile(r"https?://t\.me/c/(\d+)/(\d+)")

def parse_post_link(link: str):
    m = RE_PUBLIC.search(link)
    if m:
        username, msg_id = m.group(1), m.group(2)
        return f"@{username}", int(msg_id)
    m = RE_PRIVATE.search(link)
    if m:
        channel_part, msg_id = m.group(1), m.group(2)
        return int(f"-100{channel_part}"), int(msg_id)
    raise ValueError("Не удалось распознать ссылку. Ожидаются t.me/username/123 или t.me/c/<id>/<msg>")

@router.message(Command("addbtn"))
async def cmd_addbtn(message, bot: Bot):
    """
    Формат:
    /addbtn <post_link> | <button_text> | <callback_data>
    """
    await init_posts_table()
    args = message.text.partition(" ")[2].strip()
    if not args:
        await message.reply("Использование: /addbtn <ссылка_на_пост> | <текст_кнопки> | <callback_data>")
        return
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 3:
        await message.reply("Неверный формат. Пример: /addbtn https://t.me/chan/123 | Нажми | cb_1")
        return
    link, btn_text, callback_data = parts[0], parts[1], parts[2]
    try:
        chat_id, message_id = parse_post_link(link)
    except ValueError:
        await message.reply("Не удалось распознать ссылку. Используйте t.me/username/123 или t.me/c/<id>/<msg>.")
        return

    # Попробуем найти сохранённый пост в БД
    post = await get_post_by_chat_message(chat_id, message_id)
    # Постараемся создать новую разметку, если её нет — создаём базовую
    if post and post.get("reply_markup"):
        kb_dict = post["reply_markup"]
    else:
        kb_dict = {"inline_keyboard": []}

    # Добавляем кнопку в конец первой строки (или создай новую строк при необходимости)
    # Здесь пример — добавляем в новую строку, чтобы не ломать существующую логику:
    kb_dict["inline_keyboard"].append([{"text": btn_text, "callback_data": callback_data}])

    # Преобразуем в InlineKeyboardMarkup для отправки в Telegram
    ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(b["text"], callback_data=b.get("callback_data"), url=b.get("url")) for b in row] for row in kb_dict["inline_keyboard"]])

    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=ikb)
    except TelegramBadRequest as e:
        await message.reply(f"Не удалось отредактировать сообщение: {e}")
        return

    # Обновляем запись в БД (если записи нет — можно создать новую запись)
    if post:
        await update_reply_markup_by_chat_message(chat_id, message_id, kb_dict)
    else:
        # если нет записи — создаём минимальную запись (save_post можно импортировать при необходимости)
        from services.post_storage import save_post
        await save_post(chat_id=chat_id, message_id=message_id, text=None, reply_markup=kb_dict, created_by=message.from_user.id)

    await message.reply("Кнопка добавлена.")