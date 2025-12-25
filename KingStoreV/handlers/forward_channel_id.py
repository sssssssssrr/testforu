from aiogram import Router, F
from aiogram.types import Message

router = Router()

@router.message(F.forward_from_chat)
async def detect_forwarded_channel(message: Message) -> None:
    """
    Если пользователь пересылает сообщение из канала в бота,
    бот ответит chat_id и метаданными канала.
    Используйте этот chat_id при /addchannel.
    """
    chat = message.forward_from_chat
    if not chat:
        await message.answer("Не удалось определить исходный чат. Перешлите сообщение прямо из канала.")
        return

    info_lines = [
        f"Найден chat_id: {chat.id}",
        f"type: {chat.type}",
        f"title: {chat.title or '(нет)'}",
        f"username: {chat.username or '(приватный канал без username)'}",
        "",
        "Скопируйте chat_id и используйте /addchannel чтобы сохранить канал.",
    ]
    await message.answer("\n".join(info_lines))