from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
import logging

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    text = (
        "Привет! Я бот для создания и публикации постов в канал.\n\n"
        "Доступные команды:\n"
        "/newpost - создать новый черновик\n"
        "/drafts - показать список ваших черновиков\n"
        "/cancel - отменить текущее действие\n\n"
        "В редакторе клавиатуры вы можете добавлять строки, кнопки, редактировать их текст и URL, удалять и перемещать кнопки, форматировать в N колонок и просматривать клавиатуру."
    )
    await message.answer(text)