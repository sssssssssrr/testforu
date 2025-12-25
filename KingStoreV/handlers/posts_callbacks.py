# Пример handler-а: создаёт превью поста, сохраняет payload в БД и даёт кнопку "Редактировать"
# При нажатии загружает payload по id и предлагает редактирование.
from aiogram import Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Text
from services.callback_store import store_payload, get_payload
import html

router = Router()

MAX_CALLBACK_BYTES = 64

def callback_ok(s: str) -> bool:
    return len(s.encode("utf-8")) <= MAX_CALLBACK_BYTES

@router.message(lambda message: message.text and message.text.startswith("/preview"))
async def create_post_preview(message: types.Message):
    """
    Команда /preview <текст поста> — создаёт превью и кнопку Редактировать.
    Замените этот хэндлер на ваш реальный код генерации поста.
    """
    # Получаем текст после команды
    text = message.text.partition(" ")[2].strip() or "Пустой текст поста"
    # Экранируем HTML-пользовательский ввод перед отправкой в parse_mode="HTML"
    safe_text = html.escape(text)

    payload = {
        "author_id": message.from_user.id,
        "text": text,
        # сюда можно положить любые большие дополнительные данные (кнопки, мета и т.д.)
        "meta": {"source": "preview_command"},
    }
    payload_id = await store_payload(payload)
    cb = f"edit:{payload_id}"
    # Защитная проверка (на случай проблем с длиной)
    if not callback_ok(cb):
        # маловероятно, но на всякий случай обрезаем id
        cb = f"e:{payload_id[:12]}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Редактировать", callback_data=cb)]
    ])
    await message.answer(f"Предпросмотр поста:\n\n{safe_text}", reply_markup=kb)

@router.callback_query(Text(startswith="edit:"))
async def on_edit_callback(callback: types.CallbackQuery):
    # Обязательно отвечаем на callback, чтобы убрать "часики"
    await callback.answer()
    _, payload_id = callback.data.split(":", 1)
    payload = await get_payload(payload_id)
    if not payload:
        await callback.message.answer("Данные для редактирования не найдены или устарели.")
        return

    # Пример: показываем содержимое и инструкции по редактированию
    text = payload.get("text", "")
    safe_text = html.escape(text)
    await callback.message.answer(f"Редактируем пост:\n\n{safe_text}\n\n" +
                                  "Отправьте новый текст или нажмите /cancel для отмены.")
    # Здесь вы можете запустить FSM-flow, чтобы пользователь изменил текст,
    # затем сохранить изменения и при необходимости редактировать опубликованное сообщение.