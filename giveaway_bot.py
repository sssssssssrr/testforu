
import asyncio
import logging
import os
import re
import sqlite3
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from openpyxl import Workbook

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
PRIVACY_POLICY_URL = os.getenv("PRIVACY_POLICY_URL", "https://example.com/privacy")
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.sqlite3")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow")
SUPERADMIN_IDS = {int(x.strip()) for x in os.getenv("SUPERADMIN_IDS", "").split(",") if x.strip().isdigit()}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не указан в .env")
if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME не указан в .env")

TZ = ZoneInfo(DEFAULT_TIMEZONE)
DATETIME_FORMAT = "%d.%m.%Y %H:%M"
RUS_NAME_RE = re.compile(r"^[А-ЯЁа-яё]+(?:[ -][А-ЯЁа-яё]+)*$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("giveaway_bot")

conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row


def init_db():
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_user_id INTEGER UNIQUE NOT NULL,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        role TEXT NOT NULL DEFAULT 'user',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        chat_id TEXT UNIQUE NOT NULL,
        username TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by_tg_user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS giveaways (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        slug TEXT UNIQUE NOT NULL,
        welcome_text TEXT NOT NULL,
        success_text TEXT NOT NULL,
        success_image_file_id TEXT,
        finished_text TEXT NOT NULL,
        finished_image_file_id TEXT,
        post_channel_id TEXT,
        finish_channel_ids TEXT,
        publish_finish_notice INTEGER NOT NULL DEFAULT 0,
        start_at TEXT NOT NULL,
        end_at TEXT NOT NULL,
        live_at TEXT,
        status TEXT NOT NULL DEFAULT 'scheduled',
        created_by_tg_user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS giveaway_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        giveaway_id INTEGER NOT NULL,
        channel_chat_id TEXT NOT NULL,
        channel_title TEXT,
        post_text TEXT NOT NULL,
        image_file_id TEXT,
        telegram_message_id INTEGER,
        created_by_tg_user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        giveaway_id INTEGER NOT NULL,
        tg_user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        consent_given INTEGER NOT NULL DEFAULT 0,
        is_duplicate_phone INTEGER NOT NULL DEFAULT 0,
        is_suspicious INTEGER NOT NULL DEFAULT 0,
        source TEXT,
        status TEXT NOT NULL DEFAULT 'approved',
        created_at TEXT NOT NULL,
        UNIQUE(giveaway_id, tg_user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_participants_giveaway_id ON participants(giveaway_id);
    CREATE INDEX IF NOT EXISTS idx_participants_phone ON participants(phone);
    """)
    try:
        conn.execute("ALTER TABLE giveaways ADD COLUMN finished_image_file_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def now_local():
    return datetime.now(TZ)


def now_iso():
    return now_local().isoformat()


def parse_dt(text):
    return datetime.strptime(text.strip(), DATETIME_FORMAT).replace(tzinfo=TZ)


def dt_to_iso(dt):
    return dt.isoformat() if dt else None


def iso_to_dt(text):
    return datetime.fromisoformat(text) if text else None


def fmt_dt(text):
    dt = iso_to_dt(text)
    return dt.strftime(DATETIME_FORMAT) if dt else "—"


def slugify(value):
    value = re.sub(r"[^a-zA-Z0-9а-яА-Я_-]+", "-", value.strip().lower())
    value = re.sub(r"-+", "-", value).strip("-")
    return value or f"giveaway-{int(datetime.now().timestamp())}"


def clean_phone(phone):
    return re.sub(r"[^\d+]", "", phone).strip()


def is_valid_russian_name(value):
    return bool(RUS_NAME_RE.fullmatch(value.strip()))


def join_link_for_slug(slug):
    return f"https://t.me/{BOT_USERNAME}?start=join_{slug}"


def upsert_user(message):
    row = conn.execute("SELECT * FROM users WHERE tg_user_id = ?", (message.from_user.id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE users SET username=?, first_name=?, last_name=?, is_active=1 WHERE tg_user_id=?",
            (message.from_user.username, message.from_user.first_name, message.from_user.last_name, message.from_user.id),
        )
    else:
        role = "superadmin" if message.from_user.id in SUPERADMIN_IDS else "user"
        conn.execute(
            "INSERT INTO users (tg_user_id, username, first_name, last_name, role, is_active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name, role, now_iso()),
        )
    conn.commit()


def get_user_role(tg_user_id):
    if tg_user_id in SUPERADMIN_IDS:
        return "superadmin"
    row = conn.execute("SELECT role FROM users WHERE tg_user_id=? AND is_active=1", (tg_user_id,)).fetchone()
    return row["role"] if row else "user"


def is_admin(tg_user_id):
    return get_user_role(tg_user_id) in {"superadmin", "admin", "manager"}


def is_superadmin(tg_user_id):
    return get_user_role(tg_user_id) == "superadmin" or "admin"


def users_with_roles():
    return conn.execute(
        """
        SELECT * FROM users
        WHERE is_active = 1 AND role IN ('superadmin', 'admin', 'manager')
        ORDER BY
            CASE role
                WHEN 'superadmin' THEN 1
                WHEN 'admin' THEN 2
                WHEN 'manager' THEN 3
                ELSE 4
            END,
            COALESCE(first_name, ''),
            COALESCE(last_name, ''),
            tg_user_id
        """
    ).fetchall()


def get_user_by_tg_id(tg_user_id):
    return conn.execute("SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,)).fetchone()


def get_channel_by_id(channel_id):
    return conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()


def get_channel_by_chat_id(chat_id):
    return conn.execute("SELECT * FROM channels WHERE chat_id=?", (str(chat_id),)).fetchone()


def channel_list(active_only=True):
    sql = "SELECT * FROM channels WHERE is_active=1 ORDER BY title ASC" if active_only else "SELECT * FROM channels ORDER BY title ASC"
    return conn.execute(sql).fetchall()


def get_giveaway_by_id(giveaway_id):
    return conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()


def get_giveaway_by_slug(slug):
    return conn.execute("SELECT * FROM giveaways WHERE slug=?", (slug,)).fetchone()


def participant_count(giveaway_id):
    return int(conn.execute("SELECT COUNT(*) cnt FROM participants WHERE giveaway_id=?", (giveaway_id,)).fetchone()["cnt"])


def channel_title_by_chat_id(chat_id):
    if not chat_id:
        return "—"
    row = get_channel_by_chat_id(chat_id)
    return row["title"] if row else str(chat_id)


def active_giveaways():
    return conn.execute("SELECT * FROM giveaways WHERE status='active' ORDER BY start_at ASC").fetchall()


def my_participations(user_id):
    return conn.execute(
        "SELECT p.created_at joined_at, g.* FROM participants p JOIN giveaways g ON g.id=p.giveaway_id WHERE p.tg_user_id=? ORDER BY p.created_at DESC",
        (user_id,),
    ).fetchall()


def set_statuses():
    changed_finished = []
    current = now_local()
    for row in conn.execute("SELECT * FROM giveaways WHERE status!='archived'").fetchall():
        start_at = iso_to_dt(row["start_at"])
        end_at = iso_to_dt(row["end_at"])
        new_status = row["status"]
        if current < start_at:
            new_status = "scheduled"
        elif start_at <= current < end_at:
            new_status = "active"
        else:
            new_status = "finished"
        if new_status != row["status"]:
            conn.execute("UPDATE giveaways SET status=?, updated_at=? WHERE id=?", (new_status, now_iso(), row["id"]))
            conn.commit()
            if new_status == "finished":
                changed_finished.append(get_giveaway_by_id(row["id"]))
    return changed_finished


def export_xlsx_bytes(giveaway):
    wb = Workbook()
    ws = wb.active
    ws.title = giveaway["slug"][:31]
    ws.append(["Дата регистрации","Розыгрыш","Статус","Telegram ID","Username","Имя","Фамилия","Телефон","Согласие","Дубль телефона","Подозрительная заявка","Источник"])
    rows = conn.execute("SELECT * FROM participants WHERE giveaway_id=? ORDER BY created_at ASC", (giveaway["id"],)).fetchall()
    for row in rows:
        ws.append([
            fmt_dt(row["created_at"]), giveaway["title"], row["status"], row["tg_user_id"],
            f"@{row['username']}" if row["username"] else "", row["first_name"], row["last_name"], row["phone"],
            "Да" if row["consent_given"] else "Нет", "Да" if row["is_duplicate_phone"] else "Нет",
            "Да" if row["is_suspicious"] else "Нет", row["source"] or ""
        ])
    for col in ws.columns:
        length = max(len(str(c.value or "")) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(length + 2, 12), 40)
    with NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        wb.save(tmp.name)
        tmp_path = Path(tmp.name)
    data = tmp_path.read_bytes()
    with suppress(Exception):
        tmp_path.unlink()
    return data


def giveaway_summary(row):
    finish_targets = "Не публиковать"
    if row["publish_finish_notice"]:
        targets = [channel_title_by_chat_id(x) for x in (row["finish_channel_ids"] or "").split(",") if x]
        finish_targets = ", ".join(targets) if targets else "Не выбраны"
    return (
        f"<b>{row['title']}</b>\n"
        f"Slug: <code>{row['slug']}</code>\n"
        f"Статус: <b>{row['status']}</b>\n"
        f"Старт: {fmt_dt(row['start_at'])}\n"
        f"Финиш: {fmt_dt(row['end_at'])}\n"
        f"Эфир: {fmt_dt(row['live_at'])}\n"
        f"Пост-канал: {channel_title_by_chat_id(row['post_channel_id'])}\n"
        f"Финальные каналы: {finish_targets}\n"
        f"Участников: <b>{participant_count(row['id'])}</b>\n\n"
        f"Ссылка для кнопки «Участвовать»:\n{join_link_for_slug(row['slug'])}"
    )


def review_text(data):
    username_line = f"@{data.get('username')}" if data.get("username") else "—"
    return (
        "<b>Проверь анкету</b>\n"
        f"Имя: {data.get('first_name', '—')}\n"
        f"Фамилия: {data.get('last_name', '—')}\n"
        f"Username: {username_line}\n"
        f"Телефон: {data.get('phone', '—')}\n"
        "Согласие: да\n\n"
        "Если нужно, исправь поле кнопками ниже."
    )


def post_preview_text(data):
    return (
        "<b>Предпросмотр поста</b>\n"
        f"Розыгрыш: {data.get('giveaway_title', '—')}\n"
        f"Канал: {data.get('channel_title', '—')}\n\n"
        f"{data.get('post_text', '')}"
    )


def build_giveaway_preview_text(data):
    finish_channels = ", ".join(channel_title_by_chat_id(x) for x in (data.get("finish_channel_ids") or "").split(",") if x) or "Не публиковать"
    return (
        "<b>Предпросмотр розыгрыша</b>\n"
        f"Название: {data.get('title', '—')}\n"
        f"Slug: {data.get('slug', '—')}\n"
        f"Старт: {fmt_dt(data.get('start_at'))}\n"
        f"Финиш: {fmt_dt(data.get('end_at'))}\n"
        f"Эфир: {fmt_dt(data.get('live_at'))}\n"
        f"Текст приветствия: {'Заполнен' if data.get('welcome_text') else 'Не заполнен'}\n"
        f"Текст после регистрации: {'Заполнен' if data.get('success_text') else 'Не заполнен'}\n"
        f"Картинка после регистрации: {'Да' if data.get('success_image_file_id') else 'Нет'}\n"
        f"Текст после завершения: {'Заполнен' if data.get('finished_text') else 'Не заполнен'}\n"
        f"Картинка после завершения: {'Да' if data.get('finished_image_file_id') else 'Нет'}\n"
        f"Финальные каналы: {finish_channels}"
    )


def main_menu(role):
    kb = ReplyKeyboardBuilder()
    if role in {"superadmin", "admin", "manager"}:
        kb.row(KeyboardButton(text="Создать розыгрыш"), KeyboardButton(text="Создать пост"))
        kb.row(KeyboardButton(text="Список розыгрышей"), KeyboardButton(text="Скачать таблицу"))
        kb.row(KeyboardButton(text="Каналы"), KeyboardButton(text="Активные розыгрыши"))
        if role == "superadmin":
            kb.row(KeyboardButton(text="Управление ролями"))
    else:
        kb.row(KeyboardButton(text="Активные розыгрыши"), KeyboardButton(text="Мои участия"))
    kb.row(KeyboardButton(text="Мой ID"), KeyboardButton(text="Помощь"))
    return kb.as_markup(resize_keyboard=True)


def cancel_menu():
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text="Отмена"))
    return kb.as_markup(resize_keyboard=True)


def skip_menu():
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text="Пропустить"), KeyboardButton(text="Отмена"))
    return kb.as_markup(resize_keyboard=True)


def phone_menu():
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text="Отправить контакт", request_contact=True))
    kb.row(KeyboardButton(text="Отмена"))
    return kb.as_markup(resize_keyboard=True)


def consent_inline():
    kb = InlineKeyboardBuilder()
    kb.button(text="Согласен", callback_data="join:consent_yes")
    kb.button(text="Не согласен", callback_data="join:consent_no")
    kb.adjust(2)
    return kb


def review_inline():
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтвердить анкету", callback_data="join:submit")
    kb.button(text="Изменить имя", callback_data="join:edit_first_name")
    kb.button(text="Изменить фамилию", callback_data="join:edit_last_name")
    kb.button(text="Изменить телефон", callback_data="join:edit_phone")
    kb.button(text="Отменить", callback_data="join:cancel")
    kb.adjust(1, 2, 2)
    return kb


def build_rows_inline(rows, prefix, label_key="title", status_key=None):
    kb = InlineKeyboardBuilder()
    for row in rows:
        label = row[label_key]
        if status_key:
            label = f"{label} [{row[status_key]}]"
        kb.button(text=label, callback_data=f"{prefix}:{row['id']}")
    kb.adjust(1)
    return kb


def channels_inline(prefix):
    kb = InlineKeyboardBuilder()
    for row in channel_list(True):
        kb.button(text=row["title"], callback_data=f"{prefix}:{row['id']}")
    kb.adjust(1)
    return kb


def finish_mode_inline():
    kb = InlineKeyboardBuilder()
    kb.button(text="Не публиковать", callback_data="finish_mode:none")
    kb.button(text="Выбрать каналы", callback_data="finish_mode:select")
    kb.adjust(1)
    return kb


def finish_channels_selection_inline(selected_ids):
    kb = InlineKeyboardBuilder()
    for row in channel_list(True):
        mark = "✅ " if str(row["chat_id"]) in selected_ids else ""
        kb.button(text=f"{mark}{row['title']}", callback_data=f"finish_ch_toggle:{row['id']}")
    kb.button(text="Готово", callback_data="finish_ch_done")
    kb.adjust(1)
    return kb


def post_preview_inline():
    kb = InlineKeyboardBuilder()
    kb.button(text="Опубликовать", callback_data="post:publish")
    kb.button(text="Изменить розыгрыш", callback_data="post:edit_giveaway")
    kb.button(text="Изменить канал", callback_data="post:edit_channel")
    kb.button(text="Изменить текст", callback_data="post:edit_text")
    kb.button(text="Изменить картинку", callback_data="post:edit_image")
    kb.button(text="Отмена", callback_data="post:cancel")
    kb.adjust(1, 2, 2, 1)
    return kb


def role_users_inline():
    kb = InlineKeyboardBuilder()
    for row in users_with_roles():
        parts = [row["first_name"] or "", row["last_name"] or ""]
        display_name = " ".join(x for x in parts if x).strip() or (f"@{row['username']}" if row["username"] else str(row["tg_user_id"]))
        kb.button(text=f"{display_name} [{row['role']}]", callback_data=f"role_user:{row['tg_user_id']}")
    kb.adjust(1)
    return kb


def role_manage_menu_inline():
    kb = InlineKeyboardBuilder()
    kb.button(text="Назначить роль по ID", callback_data="role_add_by_id")
    kb.adjust(1)
    return kb


def role_actions_inline(target_user_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="Назначить admin", callback_data=f"role_set:{target_user_id}:admin")
    kb.button(text="Назначить manager", callback_data=f"role_set:{target_user_id}:manager")
    kb.button(text="Сделать обычным", callback_data=f"role_set:{target_user_id}:user")
    kb.adjust(1)
    return kb


def giveaway_preview_inline():
    kb = InlineKeyboardBuilder()
    kb.button(text="Сохранить розыгрыш", callback_data="giveaway:save")
    kb.button(text="Изменить название", callback_data="giveaway_edit:title")
    kb.button(text="Изменить slug", callback_data="giveaway_edit:slug")
    kb.button(text="Изменить старт", callback_data="giveaway_edit:start_at")
    kb.button(text="Изменить окончание", callback_data="giveaway_edit:end_at")
    kb.button(text="Изменить эфир", callback_data="giveaway_edit:live_at")
    kb.button(text="Изменить приветствие", callback_data="giveaway_edit:welcome_text")
    kb.button(text="Изменить текст после регистрации", callback_data="giveaway_edit:success_text")
    kb.button(text="Изменить картинку после регистрации", callback_data="giveaway_edit:success_image")
    kb.button(text="Изменить текст после завершения", callback_data="giveaway_edit:finished_text")
    kb.button(text="Изменить картинку после завершения", callback_data="giveaway_edit:finished_image")
    kb.button(text="Изменить каналы завершения", callback_data="giveaway_edit:finish_channels")
    kb.button(text="Отмена", callback_data="giveaway:cancel")
    kb.adjust(1, 2, 2, 1, 2, 2, 1)
    return kb


class CreateGiveawayState(StatesGroup):
    title = State()
    slug = State()
    start_at = State()
    end_at = State()
    live_at = State()
    welcome_text = State()
    success_text = State()
    success_image = State()
    finished_text = State()
    finished_image = State()
    finish_publish_mode = State()
    finish_channels = State()
    preview = State()
    edit_title = State()
    edit_slug = State()
    edit_start_at = State()
    edit_end_at = State()
    edit_live_at = State()
    edit_welcome_text = State()
    edit_success_text = State()
    edit_success_image = State()
    edit_finished_text = State()
    edit_finished_image = State()


class CreateChannelState(StatesGroup):
    title = State()
    chat_id = State()
    username = State()


class CreatePostState(StatesGroup):
    choose_giveaway = State()
    choose_channel = State()
    post_text = State()
    image = State()
    preview = State()
    edit_text = State()
    edit_image = State()


class JoinState(StatesGroup):
    first_name = State()
    last_name = State()
    phone = State()
    consent = State()
    review = State()
    edit_first_name = State()
    edit_last_name = State()
    edit_phone = State()


class RoleState(StatesGroup):
    user_id = State()


session = AiohttpSession(timeout=12)
bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TZ)


async def safe_send(message, text, **kwargs):
    for attempt in range(3):
        try:
            await message.answer(text, **kwargs)
            return True
        except TelegramNetworkError as e:
            if attempt == 2:
                logger.warning("Сеть Telegram недоступна при отправке сообщения: %s", e)
                return False
            await asyncio.sleep(0.8 * (attempt + 1))
        except Exception as e:
            logger.warning("Ошибка отправки сообщения: %s", e)
            return False
    return False


async def send_giveaway_preview(message_or_call, state):
    data = await state.get_data()
    text = build_giveaway_preview_text(data)
    markup = giveaway_preview_inline().as_markup()
    target = message_or_call.message if isinstance(message_or_call, CallbackQuery) else message_or_call
    await target.answer(text, reply_markup=markup)


async def sync_statuses_job():
    try:
        for row in set_statuses():
            if not row["publish_finish_notice"]:
                continue
            for channel_id in [x.strip() for x in (row["finish_channel_ids"] or "").split(",") if x.strip()]:
                try:
                    if row["finished_image_file_id"]:
                        await bot.send_photo(int(channel_id), row["finished_image_file_id"], caption=row["finished_text"])
                    else:
                        await bot.send_message(int(channel_id), row["finished_text"])
                except Exception as e:
                    logger.warning("Не удалось отправить сообщение о завершении в канал %s: %s", channel_id, e)
    except Exception as e:
        logger.warning("Ошибка scheduler: %s", e)


@dp.message(F.text == "Отмена")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    upsert_user(message)
    await safe_send(message, "Действие отменено.", reply_markup=main_menu(get_user_role(message.from_user.id)))


@dp.message(Command("myid"))
@dp.message(F.text == "Мой ID")
async def myid(message: Message):
    upsert_user(message)
    await safe_send(message, f"Твой Telegram ID: <code>{message.from_user.id}</code>", reply_markup=main_menu(get_user_role(message.from_user.id)))


async def handle_start_payload(message, state, payload):
    if payload and payload.startswith("join_"):
        giveaway = get_giveaway_by_slug(payload.replace("join_", "", 1))
        if not giveaway:
            await safe_send(message, "Розыгрыш не найден или ссылка устарела.")
            return True
        await open_join_flow(message, state, giveaway)
        return True
    return False


@dp.message(CommandStart(deep_link=True))
async def cmd_start_deep(message: Message, command: CommandObject, state: FSMContext):
    upsert_user(message)
    if await handle_start_payload(message, state, command.args):
        return
    await safe_send(message, "Бот запущен.", reply_markup=main_menu(get_user_role(message.from_user.id)))


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject = None):
    upsert_user(message)
    payload = command.args if command and command.args else None
    if not payload and (message.text or "").startswith("/start "):
        payload = message.text.split(" ", 1)[1].strip()
    if await handle_start_payload(message, state, payload):
        return
    role = get_user_role(message.from_user.id)
    text = "Привет! "
    text += "Используй меню ниже для управления розыгрышами." if is_admin(message.from_user.id) else "Здесь ты можешь участвовать в розыгрышах и смотреть свои участия."
    await safe_send(message, text, reply_markup=main_menu(role))


@dp.message(F.text == "Помощь")
async def help_menu(message: Message):
    upsert_user(message)
    role = get_user_role(message.from_user.id)
    if is_admin(message.from_user.id):
        text = (
            "<b>Помощь для администратора</b>\n"
            "• В разделе «Каналы» добавляются каналы вручную по chat_id.\n"
            "• Сначала создается розыгрыш, потом отдельный пост с кнопкой «Участвовать».\n"
            "• На предпросмотре розыгрыша можно изменить все ключевые поля перед сохранением.\n"
            "• Для сообщения после завершения можно указать текст, картинку и один или несколько каналов.\n"
            "• В разделе «Скачать таблицу» выгружается Excel по выбранному розыгрышу.\n"
            "• В разделе «Управление ролями» суперадмин может назначать и менять роли."
        )
    else:
        text = (
            "<b>Помощь для участника</b>\n"
            "• Нажми кнопку участия под постом розыгрыша.\n"
            "• Имя и фамилия принимаются только на русском языке.\n"
            "• Перед отправкой анкеты можно изменить имя, фамилию и телефон.\n"
            "• В разделе «Активные розыгрыши» видны текущие розыгрыши.\n"
            "• В разделе «Мои участия» видны розыгрыши, в которых ты уже участвуешь."
        )
    await safe_send(message, text, reply_markup=main_menu(role))


@dp.message(F.text == "Активные розыгрыши")
async def active_menu(message: Message):
    upsert_user(message)
    rows = active_giveaways()
    if not rows:
        await safe_send(message, "Сейчас активных розыгрышей нет.", reply_markup=main_menu(get_user_role(message.from_user.id)))
        return
    txt = "\n\n".join([f"<b>{r['title']}</b>\nДо: {fmt_dt(r['end_at'])}\nЭфир: {fmt_dt(r['live_at'])}\nУчаствовать: {join_link_for_slug(r['slug'])}" for r in rows])
    await safe_send(message, txt, reply_markup=main_menu(get_user_role(message.from_user.id)))


@dp.message(F.text == "Мои участия")
async def my_parts_menu(message: Message):
    upsert_user(message)
    rows = my_participations(message.from_user.id)
    if not rows:
        await safe_send(message, "Ты пока не участвуешь ни в одном розыгрыше.", reply_markup=main_menu(get_user_role(message.from_user.id)))
        return
    txt = "\n\n".join([f"<b>{r['title']}</b>\nСтатус: {r['status']}\nОкончание: {fmt_dt(r['end_at'])}\nЭфир: {fmt_dt(r['live_at'])}" for r in rows])
    await safe_send(message, txt, reply_markup=main_menu(get_user_role(message.from_user.id)))


@dp.message(F.text == "Каналы")
async def channels_menu(message: Message):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        await safe_send(message, "Нет доступа.")
        return
    rows = channel_list(False)
    text = "<b>Каналы</b>\n" + ("\n".join([f"• {r['title']} — <code>{r['chat_id']}</code>{' (@' + r['username'] + ')' if r['username'] else ''}" for r in rows]) if rows else "Пока нет ни одного канала.")
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text="Добавить канал"), KeyboardButton(text="Назад в меню"))
    await safe_send(message, text, reply_markup=kb.as_markup(resize_keyboard=True))


@dp.message(F.text == "Назад в меню")
async def back_menu(message: Message, state: FSMContext):
    await state.clear()
    upsert_user(message)
    await safe_send(message, "Главное меню.", reply_markup=main_menu(get_user_role(message.from_user.id)))


@dp.message(F.text == "Добавить канал")
async def add_channel_start(message: Message, state: FSMContext):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        await safe_send(message, "Нет доступа.")
        return
    await state.clear()
    await state.set_state(CreateChannelState.title)
    await safe_send(message, "Введи название канала:", reply_markup=cancel_menu())


@dp.message(CreateChannelState.title, F.text)
async def add_channel_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(CreateChannelState.chat_id)
    await safe_send(message, "Введи chat_id канала в формате <code>-100...</code>:")


@dp.message(CreateChannelState.chat_id, F.text)
async def add_channel_chat(message: Message, state: FSMContext):
    value = message.text.strip()
    if not re.fullmatch(r"-100\d+", value):
        await safe_send(message, "Нужен корректный chat_id в формате <code>-100...</code>.")
        return
    if get_channel_by_chat_id(value):
        await safe_send(message, "Такой канал уже добавлен.")
        return
    await state.update_data(chat_id=value)
    await state.set_state(CreateChannelState.username)
    await safe_send(message, "Введи username канала без @ или нажми «Пропустить».", reply_markup=skip_menu())


@dp.message(CreateChannelState.username, F.text == "Пропустить")
async def add_channel_user_skip(message: Message, state: FSMContext):
    data = await state.get_data()
    conn.execute("INSERT INTO channels (title, chat_id, username, is_active, created_by_tg_user_id, created_at) VALUES (?, ?, ?, 1, ?, ?)",
                 (data["title"], data["chat_id"], None, message.from_user.id, now_iso()))
    conn.commit()
    await state.clear()
    await safe_send(message, "Канал сохранен.", reply_markup=main_menu(get_user_role(message.from_user.id)))


@dp.message(CreateChannelState.username, F.text)
async def add_channel_user(message: Message, state: FSMContext):
    data = await state.get_data()
    conn.execute("INSERT INTO channels (title, chat_id, username, is_active, created_by_tg_user_id, created_at) VALUES (?, ?, ?, 1, ?, ?)",
                 (data["title"], data["chat_id"], message.text.strip().lstrip("@"), message.from_user.id, now_iso()))
    conn.commit()
    await state.clear()
    await safe_send(message, "Канал сохранен.", reply_markup=main_menu(get_user_role(message.from_user.id)))


@dp.message(F.text == "Создать розыгрыш")
async def giveaway_start(message: Message, state: FSMContext):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        await safe_send(message, "Нет доступа.")
        return
    await state.clear()
    await state.set_state(CreateGiveawayState.title)
    await safe_send(message, "Введи название розыгрыша:", reply_markup=cancel_menu())


@dp.message(CreateGiveawayState.title, F.text)
async def g_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(CreateGiveawayState.slug)
    await safe_send(message, "Введи slug для ссылки или любой текст — я преобразую:")


@dp.message(CreateGiveawayState.slug, F.text)
async def g_slug(message: Message, state: FSMContext):
    slug = slugify(message.text)
    if get_giveaway_by_slug(slug):
        await safe_send(message, "Такой slug уже существует. Пришли другой.")
        return
    await state.update_data(slug=slug)
    await state.set_state(CreateGiveawayState.start_at)
    await safe_send(message, "Дата старта: ДД.ММ.ГГГГ ЧЧ:ММ")


@dp.message(CreateGiveawayState.start_at, F.text)
async def g_start(message: Message, state: FSMContext):
    try:
        dt = parse_dt(message.text)
    except Exception:
        await safe_send(message, "Неверный формат. Пример: 01.10.2026 10:00")
        return
    await state.update_data(start_at=dt_to_iso(dt))
    await state.set_state(CreateGiveawayState.end_at)
    await safe_send(message, "Дата окончания: ДД.ММ.ГГГГ ЧЧ:ММ")


@dp.message(CreateGiveawayState.end_at, F.text)
async def g_end(message: Message, state: FSMContext):
    try:
        dt = parse_dt(message.text)
    except Exception:
        await safe_send(message, "Неверный формат. Пример: 15.10.2026 23:59")
        return
    data = await state.get_data()
    if iso_to_dt(data["start_at"]) >= dt:
        await safe_send(message, "Дата окончания должна быть позже даты старта.")
        return
    await state.update_data(end_at=dt_to_iso(dt))
    await state.set_state(CreateGiveawayState.live_at)
    await safe_send(message, "Дата эфира: ДД.ММ.ГГГГ ЧЧ:ММ или «Пропустить».", reply_markup=skip_menu())


@dp.message(CreateGiveawayState.live_at, F.text == "Пропустить")
async def g_live_skip(message: Message, state: FSMContext):
    await state.update_data(live_at=None)
    await state.set_state(CreateGiveawayState.welcome_text)
    await safe_send(message, "Текст приветствия в боте:")


@dp.message(CreateGiveawayState.live_at, F.text)
async def g_live(message: Message, state: FSMContext):
    try:
        dt = parse_dt(message.text)
    except Exception:
        await safe_send(message, "Неверный формат. Пример: 16.10.2026 18:00")
        return
    await state.update_data(live_at=dt_to_iso(dt))
    await state.set_state(CreateGiveawayState.welcome_text)
    await safe_send(message, "Текст приветствия в боте:")


@dp.message(CreateGiveawayState.welcome_text, F.text)
async def g_welcome(message: Message, state: FSMContext):
    await state.update_data(welcome_text=message.html_text)
    await state.set_state(CreateGiveawayState.success_text)
    await safe_send(message, "Текст после успешной регистрации:")


@dp.message(CreateGiveawayState.success_text, F.text)
async def g_success(message: Message, state: FSMContext):
    await state.update_data(success_text=message.html_text)
    await state.set_state(CreateGiveawayState.success_image)
    await safe_send(message, "Картинка для сообщения после регистрации или «Пропустить».", reply_markup=skip_menu())


@dp.message(CreateGiveawayState.success_image, F.text == "Пропустить")
async def g_success_img_skip(message: Message, state: FSMContext):
    await state.update_data(success_image_file_id=None)
    await state.set_state(CreateGiveawayState.finished_text)
    await safe_send(message, "Текст после завершения розыгрыша или «Пропустить».", reply_markup=skip_menu())


@dp.message(CreateGiveawayState.success_image, F.photo)
async def g_success_img(message: Message, state: FSMContext):
    await state.update_data(success_image_file_id=message.photo[-1].file_id)
    await state.set_state(CreateGiveawayState.finished_text)
    await safe_send(message, "Текст после завершения розыгрыша или «Пропустить».", reply_markup=skip_menu())


@dp.message(CreateGiveawayState.success_image)
async def g_success_img_invalid(message: Message):
    await safe_send(message, "Пришли фото или нажми «Пропустить».")


@dp.message(CreateGiveawayState.finished_text, F.text == "Пропустить")
async def g_finish_skip(message: Message, state: FSMContext):
    await state.update_data(finished_text="РОЗЫГРЫШ ЗАВЕРШЕН, мы подводим итоги!")
    await state.set_state(CreateGiveawayState.finished_image)
    await safe_send(message, "Картинка для сообщения после завершения или «Пропустить».", reply_markup=skip_menu())


@dp.message(CreateGiveawayState.finished_text, F.text)
async def g_finish(message: Message, state: FSMContext):
    await state.update_data(finished_text=message.html_text)
    await state.set_state(CreateGiveawayState.finished_image)
    await safe_send(message, "Картинка для сообщения после завершения или «Пропустить».", reply_markup=skip_menu())


@dp.message(CreateGiveawayState.finished_image, F.text == "Пропустить")
async def g_finished_img_skip(message: Message, state: FSMContext):
    await state.update_data(finished_image_file_id=None)
    await state.set_state(CreateGiveawayState.finish_publish_mode)
    await safe_send(message, "Куда публиковать сообщение после завершения?", reply_markup=finish_mode_inline().as_markup())


@dp.message(CreateGiveawayState.finished_image, F.photo)
async def g_finished_img(message: Message, state: FSMContext):
    await state.update_data(finished_image_file_id=message.photo[-1].file_id)
    await state.set_state(CreateGiveawayState.finish_publish_mode)
    await safe_send(message, "Куда публиковать сообщение после завершения?", reply_markup=finish_mode_inline().as_markup())


@dp.message(CreateGiveawayState.finished_image)
async def g_finished_img_invalid(message: Message):
    await safe_send(message, "Пришли фото или нажми «Пропустить».")


@dp.callback_query(CreateGiveawayState.finish_publish_mode, F.data == "finish_mode:none")
async def g_mode_none(call: CallbackQuery, state: FSMContext):
    await state.update_data(publish_finish_notice=0, finish_channel_ids="", post_channel_id="")
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(call, state)
    await call.answer()


@dp.callback_query(CreateGiveawayState.finish_publish_mode, F.data == "finish_mode:select")
async def g_mode_select(call: CallbackQuery, state: FSMContext):
    if not channel_list(True):
        await state.clear()
        await call.message.answer("Сначала добавь каналы в разделе «Каналы».", reply_markup=main_menu(get_user_role(call.from_user.id)))
        await call.answer()
        return
    await state.update_data(publish_finish_notice=1, finish_channel_ids="")
    await state.set_state(CreateGiveawayState.finish_channels)
    await call.message.answer("Выбери один или несколько каналов для финального сообщения. Затем нажми «Готово».",
                              reply_markup=finish_channels_selection_inline(set()).as_markup())
    await call.answer()


@dp.callback_query(CreateGiveawayState.finish_channels, F.data.startswith("finish_ch_toggle:"))
async def g_toggle_finish_channel(call: CallbackQuery, state: FSMContext):
    channel = get_channel_by_id(int(call.data.split(":", 1)[1]))
    if not channel:
        await call.answer("Канал не найден", show_alert=True)
        return
    data = await state.get_data()
    selected = {x for x in (data.get("finish_channel_ids") or "").split(",") if x}
    chat_id = str(channel["chat_id"])
    if chat_id in selected:
        selected.remove(chat_id)
    else:
        selected.add(chat_id)
    await state.update_data(finish_channel_ids=",".join(sorted(selected)))
    await call.message.edit_reply_markup(reply_markup=finish_channels_selection_inline(selected).as_markup())
    await call.answer()


@dp.callback_query(CreateGiveawayState.finish_channels, F.data == "finish_ch_done")
async def g_finish_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("finish_channel_ids"):
        await call.answer("Выбери хотя бы один канал", show_alert=True)
        return
    await state.update_data(post_channel_id=(data["finish_channel_ids"].split(",")[0]))
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(call, state)
    await call.answer()


@dp.callback_query(CreateGiveawayState.preview, F.data == "giveaway:cancel")
async def g_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Создание розыгрыша отменено.", reply_markup=main_menu(get_user_role(call.from_user.id)))
    await call.answer()


@dp.callback_query(CreateGiveawayState.preview, F.data == "giveaway:save")
async def g_save(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current = now_local()
    start_at = iso_to_dt(data["start_at"])
    end_at = iso_to_dt(data["end_at"])
    status = "scheduled" if current < start_at else ("active" if current < end_at else "finished")
    conn.execute(
        """INSERT INTO giveaways (
            title, slug, welcome_text, success_text, success_image_file_id, finished_text, finished_image_file_id,
            post_channel_id, finish_channel_ids, publish_finish_notice, start_at, end_at, live_at, status,
            created_by_tg_user_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["title"], data["slug"], data["welcome_text"], data["success_text"], data.get("success_image_file_id"),
            data["finished_text"], data.get("finished_image_file_id"), data.get("post_channel_id"),
            data.get("finish_channel_ids", ""), int(data.get("publish_finish_notice", 0)),
            data["start_at"], data["end_at"], data.get("live_at"), status, call.from_user.id, now_iso(), now_iso(),
        ),
    )
    conn.commit()
    row = get_giveaway_by_slug(data["slug"])
    await state.clear()
    await call.message.answer("Розыгрыш сохранен.\n\n" + giveaway_summary(row), reply_markup=main_menu(get_user_role(call.from_user.id)))
    await call.answer("Сохранено")


@dp.callback_query(CreateGiveawayState.preview, F.data.startswith("giveaway_edit:"))
async def giveaway_edit_router(call: CallbackQuery, state: FSMContext):
    action = call.data.split(":", 1)[1]
    mapping = {
        "title": (CreateGiveawayState.edit_title, "Введи новое название розыгрыша:"),
        "slug": (CreateGiveawayState.edit_slug, "Введи новый slug или любой текст — я преобразую:"),
        "start_at": (CreateGiveawayState.edit_start_at, "Введи новую дату старта: ДД.ММ.ГГГГ ЧЧ:ММ"),
        "end_at": (CreateGiveawayState.edit_end_at, "Введи новую дату окончания: ДД.ММ.ГГГГ ЧЧ:ММ"),
        "live_at": (CreateGiveawayState.edit_live_at, "Введи новую дату эфира: ДД.ММ.ГГГГ ЧЧ:ММ или «Пропустить»."),
        "welcome_text": (CreateGiveawayState.edit_welcome_text, "Введи новый текст приветствия:"),
        "success_text": (CreateGiveawayState.edit_success_text, "Введи новый текст после регистрации:"),
        "success_image": (CreateGiveawayState.edit_success_image, "Пришли новую картинку после регистрации или «Пропустить»."),
        "finished_text": (CreateGiveawayState.edit_finished_text, "Введи новый текст после завершения:"),
        "finished_image": (CreateGiveawayState.edit_finished_image, "Пришли новую картинку после завершения или «Пропустить»."),
    }
    if action == "finish_channels":
        await state.set_state(CreateGiveawayState.finish_channels)
        current = {x for x in (await state.get_data()).get("finish_channel_ids", "").split(",") if x}
        await call.message.answer("Выбери один или несколько каналов для финального сообщения. Затем нажми «Готово».",
                                  reply_markup=finish_channels_selection_inline(current).as_markup())
        await call.answer()
        return
    if action not in mapping:
        await call.answer("Неизвестное действие", show_alert=True)
        return
    new_state, prompt = mapping[action]
    await state.set_state(new_state)
    reply_markup = skip_menu() if action in {"live_at", "success_image", "finished_image"} else cancel_menu()
    await call.message.answer(prompt, reply_markup=reply_markup)
    await call.answer()


@dp.message(CreateGiveawayState.edit_title, F.text)
async def edit_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_slug, F.text)
async def edit_slug(message: Message, state: FSMContext):
    slug = slugify(message.text)
    data = await state.get_data()
    existing = get_giveaway_by_slug(slug)
    if existing and slug != data.get("slug"):
        await safe_send(message, "Такой slug уже существует. Пришли другой.")
        return
    await state.update_data(slug=slug)
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_start_at, F.text)
async def edit_start_at(message: Message, state: FSMContext):
    try:
        dt = parse_dt(message.text)
    except Exception:
        await safe_send(message, "Неверный формат даты.")
        return
    await state.update_data(start_at=dt_to_iso(dt))
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_end_at, F.text)
async def edit_end_at(message: Message, state: FSMContext):
    try:
        dt = parse_dt(message.text)
    except Exception:
        await safe_send(message, "Неверный формат даты.")
        return
    data = await state.get_data()
    if iso_to_dt(data["start_at"]) >= dt:
        await safe_send(message, "Дата окончания должна быть позже даты старта.")
        return
    await state.update_data(end_at=dt_to_iso(dt))
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_live_at, F.text == "Пропустить")
async def edit_live_at_skip(message: Message, state: FSMContext):
    await state.update_data(live_at=None)
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_live_at, F.text)
async def edit_live_at(message: Message, state: FSMContext):
    try:
        dt = parse_dt(message.text)
    except Exception:
        await safe_send(message, "Неверный формат даты.")
        return
    await state.update_data(live_at=dt_to_iso(dt))
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_welcome_text, F.text)
async def edit_welcome_text(message: Message, state: FSMContext):
    await state.update_data(welcome_text=message.html_text)
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_success_text, F.text)
async def edit_success_text(message: Message, state: FSMContext):
    await state.update_data(success_text=message.html_text)
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_success_image, F.text == "Пропустить")
async def edit_success_image_skip(message: Message, state: FSMContext):
    await state.update_data(success_image_file_id=None)
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_success_image, F.photo)
async def edit_success_image(message: Message, state: FSMContext):
    await state.update_data(success_image_file_id=message.photo[-1].file_id)
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_success_image)
async def edit_success_image_invalid(message: Message):
    await safe_send(message, "Пришли фото или нажми «Пропустить».")


@dp.message(CreateGiveawayState.edit_finished_text, F.text)
async def edit_finished_text(message: Message, state: FSMContext):
    await state.update_data(finished_text=message.html_text)
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_finished_image, F.text == "Пропустить")
async def edit_finished_image_skip(message: Message, state: FSMContext):
    await state.update_data(finished_image_file_id=None)
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_finished_image, F.photo)
async def edit_finished_image(message: Message, state: FSMContext):
    await state.update_data(finished_image_file_id=message.photo[-1].file_id)
    await state.set_state(CreateGiveawayState.preview)
    await send_giveaway_preview(message, state)


@dp.message(CreateGiveawayState.edit_finished_image)
async def edit_finished_image_invalid(message: Message):
    await safe_send(message, "Пришли фото или нажми «Пропустить».")


@dp.message(F.text == "Создать пост")
async def post_start(message: Message, state: FSMContext):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        await safe_send(message, "Нет доступа.")
        return
    rows = conn.execute("SELECT * FROM giveaways ORDER BY created_at DESC").fetchall()
    if not rows:
        await safe_send(message, "Сначала создай розыгрыш.")
        return
    await state.clear()
    await state.set_state(CreatePostState.choose_giveaway)
    await safe_send(message, "Выбери розыгрыш для поста:", reply_markup=build_rows_inline(rows, "post_giveaway").as_markup())


@dp.callback_query(CreatePostState.choose_giveaway, F.data.startswith("post_giveaway:"))
async def post_choose_giveaway(call: CallbackQuery, state: FSMContext):
    giveaway = get_giveaway_by_id(int(call.data.split(":", 1)[1]))
    if not giveaway:
        await call.answer("Розыгрыш не найден", show_alert=True)
        return
    if not channel_list(True):
        await state.clear()
        await call.message.answer("Нет каналов. Сначала добавь каналы в разделе «Каналы».", reply_markup=main_menu(get_user_role(call.from_user.id)))
        await call.answer()
        return
    await state.update_data(giveaway_id=giveaway["id"], giveaway_title=giveaway["title"])
    await state.set_state(CreatePostState.choose_channel)
    await call.message.answer("Выбери канал для публикации:", reply_markup=channels_inline("post_channel").as_markup())
    await call.answer()


@dp.callback_query(CreatePostState.choose_channel, F.data.startswith("post_channel:"))
async def post_choose_channel(call: CallbackQuery, state: FSMContext):
    channel = get_channel_by_id(int(call.data.split(":", 1)[1]))
    if not channel:
        await call.answer("Канал не найден", show_alert=True)
        return
    await state.update_data(channel_chat_id=channel["chat_id"], channel_title=channel["title"])
    await state.set_state(CreatePostState.post_text)
    await call.message.answer("Введи текст поста:", reply_markup=cancel_menu())
    await call.answer()


@dp.message(CreatePostState.post_text, F.text)
async def post_text(message: Message, state: FSMContext):
    await state.update_data(post_text=message.html_text)
    await state.set_state(CreatePostState.image)
    await safe_send(message, "Пришли картинку для поста или нажми «Пропустить».", reply_markup=skip_menu())


@dp.message(CreatePostState.image, F.text == "Пропустить")
async def post_image_skip(message: Message, state: FSMContext):
    await state.update_data(image_file_id=None)
    await state.set_state(CreatePostState.preview)
    data = await state.get_data()
    await safe_send(message, post_preview_text(data), reply_markup=post_preview_inline().as_markup())


@dp.message(CreatePostState.image, F.photo)
async def post_image(message: Message, state: FSMContext):
    await state.update_data(image_file_id=message.photo[-1].file_id)
    await state.set_state(CreatePostState.preview)
    data = await state.get_data()
    await message.answer_photo(data["image_file_id"], caption=post_preview_text(data), reply_markup=post_preview_inline().as_markup())


@dp.message(CreatePostState.image)
async def post_image_invalid(message: Message):
    await safe_send(message, "Пришли фото или нажми «Пропустить».")


@dp.callback_query(CreatePostState.preview, F.data == "post:cancel")
async def post_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Публикация поста отменена.", reply_markup=main_menu(get_user_role(call.from_user.id)))
    await call.answer()


@dp.callback_query(CreatePostState.preview, F.data == "post:edit_text")
async def post_edit_text(call: CallbackQuery, state: FSMContext):
    await state.set_state(CreatePostState.edit_text)
    await call.message.answer("Введи новый текст поста:", reply_markup=cancel_menu())
    await call.answer()


@dp.message(CreatePostState.edit_text, F.text)
async def post_save_text(message: Message, state: FSMContext):
    await state.update_data(post_text=message.html_text)
    await state.set_state(CreatePostState.preview)
    data = await state.get_data()
    await safe_send(message, post_preview_text(data), reply_markup=post_preview_inline().as_markup())


@dp.callback_query(CreatePostState.preview, F.data == "post:edit_image")
async def post_edit_image(call: CallbackQuery, state: FSMContext):
    await state.set_state(CreatePostState.edit_image)
    await call.message.answer("Пришли новую картинку или нажми «Пропустить».", reply_markup=skip_menu())
    await call.answer()


@dp.message(CreatePostState.edit_image, F.text == "Пропустить")
async def post_edit_image_skip(message: Message, state: FSMContext):
    await state.update_data(image_file_id=None)
    await state.set_state(CreatePostState.preview)
    data = await state.get_data()
    await safe_send(message, post_preview_text(data), reply_markup=post_preview_inline().as_markup())


@dp.message(CreatePostState.edit_image, F.photo)
async def post_edit_image_save(message: Message, state: FSMContext):
    await state.update_data(image_file_id=message.photo[-1].file_id)
    await state.set_state(CreatePostState.preview)
    data = await state.get_data()
    await message.answer_photo(data["image_file_id"], caption=post_preview_text(data), reply_markup=post_preview_inline().as_markup())


@dp.message(CreatePostState.edit_image)
async def post_edit_image_invalid(message: Message):
    await safe_send(message, "Пришли фото или нажми «Пропустить».")


@dp.callback_query(CreatePostState.preview, F.data == "post:edit_channel")
async def post_edit_channel(call: CallbackQuery, state: FSMContext):
    await state.set_state(CreatePostState.choose_channel)
    await call.message.answer("Выбери новый канал:", reply_markup=channels_inline("post_channel").as_markup())
    await call.answer()


@dp.callback_query(CreatePostState.preview, F.data == "post:edit_giveaway")
async def post_edit_giveaway(call: CallbackQuery, state: FSMContext):
    rows = conn.execute("SELECT * FROM giveaways ORDER BY created_at DESC").fetchall()
    await state.set_state(CreatePostState.choose_giveaway)
    await call.message.answer("Выбери новый розыгрыш:", reply_markup=build_rows_inline(rows, "post_giveaway").as_markup())
    await call.answer()


@dp.callback_query(CreatePostState.preview, F.data == "post:publish")
async def post_publish(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    giveaway = get_giveaway_by_id(int(data["giveaway_id"]))
    if not giveaway:
        await state.clear()
        await call.message.answer("Розыгрыш не найден.", reply_markup=main_menu(get_user_role(call.from_user.id)))
        await call.answer()
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Участвовать", url=join_link_for_slug(giveaway["slug"]))
    try:
        if data.get("image_file_id"):
            sent = await bot.send_photo(int(data["channel_chat_id"]), data["image_file_id"], caption=data["post_text"], reply_markup=kb.as_markup())
        else:
            sent = await bot.send_message(int(data["channel_chat_id"]), data["post_text"], reply_markup=kb.as_markup())
    except (TelegramBadRequest, TelegramNetworkError) as e:
        await call.message.answer(f"Не удалось опубликовать пост: {e}", reply_markup=main_menu(get_user_role(call.from_user.id)))
        await state.clear()
        await call.answer()
        return
    conn.execute(
        """INSERT INTO giveaway_posts (giveaway_id, channel_chat_id, channel_title, post_text, image_file_id, telegram_message_id, created_by_tg_user_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (giveaway["id"], data["channel_chat_id"], data["channel_title"], data["post_text"], data.get("image_file_id"), sent.message_id, call.from_user.id, now_iso()),
    )
    conn.execute("UPDATE giveaways SET post_channel_id=?, updated_at=? WHERE id=?", (data["channel_chat_id"], now_iso(), giveaway["id"]))
    conn.commit()
    await state.clear()
    await call.message.answer("Пост успешно опубликован.", reply_markup=main_menu(get_user_role(call.from_user.id)))
    await call.answer("Опубликовано")


@dp.message(F.text == "Список розыгрышей")
async def giveaways_list(message: Message):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        await safe_send(message, "Нет доступа.")
        return
    rows = conn.execute("SELECT * FROM giveaways ORDER BY created_at DESC").fetchall()
    if not rows:
        await safe_send(message, "Розыгрышей пока нет.", reply_markup=main_menu(get_user_role(message.from_user.id)))
        return
    await safe_send(message, "Выбери розыгрыш:", reply_markup=build_rows_inline(rows, "view_giveaway", status_key="status").as_markup())


@dp.callback_query(F.data.startswith("view_giveaway:"))
async def giveaway_view(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    row = get_giveaway_by_id(int(call.data.split(":", 1)[1]))
    if not row:
        await call.answer("Не найдено", show_alert=True)
        return
    await call.message.answer(giveaway_summary(row))
    await call.answer()


@dp.message(F.text == "Скачать таблицу")
async def export_menu(message: Message):
    upsert_user(message)
    if not is_admin(message.from_user.id):
        await safe_send(message, "Нет доступа.")
        return
    rows = conn.execute("SELECT * FROM giveaways ORDER BY created_at DESC").fetchall()
    if not rows:
        await safe_send(message, "Нет розыгрышей для выгрузки.")
        return
    await safe_send(message, "Выбери розыгрыш для выгрузки:", reply_markup=build_rows_inline(rows, "export").as_markup())


@dp.callback_query(F.data.startswith("export:"))
async def export_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    row = get_giveaway_by_id(int(call.data.split(":", 1)[1]))
    if not row:
        await call.answer("Розыгрыш не найден", show_alert=True)
        return
    date_part = now_local().strftime("%d.%m.%y")
    filename = f'Розыгрыш_{row["title"]}_{date_part}.xlsx'
    document = BufferedInputFile(export_xlsx_bytes(row), filename=filename)
    await call.message.answer_document(document=document, caption=f"Таблица участников: <b>{row['title']}</b>")
    await call.answer("Таблица отправлена")


@dp.message(F.text == "Управление ролями")
async def roles_start(message: Message, state: FSMContext):
    upsert_user(message)
    if not is_superadmin(message.from_user.id):
        await safe_send(message, "Только для суперадминистратора.")
        return
    await state.clear()
    rows = users_with_roles()
    if rows:
        await safe_send(message, "Выбери пользователя с ролью:", reply_markup=role_users_inline().as_markup())
        await safe_send(message, "Или назначь роль по Telegram ID:", reply_markup=role_manage_menu_inline().as_markup())
    else:
        await safe_send(message, "Пока нет пользователей с назначенными ролями.", reply_markup=role_manage_menu_inline().as_markup())


@dp.callback_query(F.data == "role_add_by_id")
async def role_add_by_id_start(call: CallbackQuery, state: FSMContext):
    if not is_superadmin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await state.set_state(RoleState.user_id)
    await call.message.answer("Пришли Telegram ID пользователя, которому нужно назначить роль.", reply_markup=cancel_menu())
    await call.answer()


@dp.message(RoleState.user_id, F.text)
async def roles_user_id(message: Message, state: FSMContext):
    try:
        target_user_id = int(message.text.strip())
    except Exception:
        await safe_send(message, "Нужен числовой Telegram ID.")
        return
    await state.update_data(target_user_id=target_user_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="Назначить admin", callback_data="set_role:admin")
    kb.button(text="Назначить manager", callback_data="set_role:manager")
    kb.button(text="Сделать обычным", callback_data="set_role:user")
    kb.adjust(1)
    await safe_send(message, "Выбери роль:", reply_markup=kb.as_markup())


@dp.callback_query(RoleState.user_id, F.data.startswith("set_role:"))
async def roles_set_by_id(call: CallbackQuery, state: FSMContext):
    if not is_superadmin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    role = call.data.split(":", 1)[1]
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    if not target_user_id:
        await call.answer("Пользователь не выбран", show_alert=True)
        return
    row = get_user_by_tg_id(target_user_id)
    if row:
        conn.execute("UPDATE users SET role = ?, is_active = 1 WHERE tg_user_id = ?", (role, target_user_id))
    else:
        conn.execute(
            "INSERT INTO users (tg_user_id, username, first_name, last_name, role, is_active, created_at) VALUES (?, '', '', '', ?, 1, ?)",
            (target_user_id, role, now_iso()),
        )
    conn.commit()
    await state.clear()
    role_label = "обычный пользователь" if role == "user" else role
    await call.message.answer(f"Пользователю <code>{target_user_id}</code> назначена роль <b>{role_label}</b>.")
    rows = users_with_roles()
    if rows:
        await call.message.answer("Актуальный список пользователей с ролями:", reply_markup=role_users_inline().as_markup())
    await call.message.answer("Управление ролями:", reply_markup=role_manage_menu_inline().as_markup())
    await call.answer("Роль сохранена")


@dp.callback_query(F.data.startswith("role_user:"))
async def role_user_card(call: CallbackQuery):
    if not is_superadmin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    target_user_id = int(call.data.split(":", 1)[1])
    row = get_user_by_tg_id(target_user_id)
    if not row:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    display_name = " ".join(x for x in [row["first_name"] or "", row["last_name"] or ""] if x).strip() or "—"
    username = f"@{row['username']}" if row["username"] else "—"
    text = (
        "<b>Пользователь</b>\n"
        f"ID: <code>{row['tg_user_id']}</code>\n"
        f"Имя: {display_name}\n"
        f"Username: {username}\n"
        f"Текущая роль: <b>{row['role']}</b>"
    )
    await call.message.answer(text, reply_markup=role_actions_inline(target_user_id).as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("role_set:"))
async def role_set_from_card(call: CallbackQuery):
    if not is_superadmin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    _, target_user_id, role = call.data.split(":", 2)
    target_user_id = int(target_user_id)
    row = get_user_by_tg_id(target_user_id)
    if not row:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    conn.execute("UPDATE users SET role = ?, is_active = 1 WHERE tg_user_id = ?", (role, target_user_id))
    conn.commit()
    role_label = "обычный пользователь" if role == "user" else role
    await call.message.answer(f"Роль пользователя <code>{target_user_id}</code> изменена на <b>{role_label}</b>.")
    rows = users_with_roles()
    if rows:
        await call.message.answer("Актуальный список пользователей с ролями:", reply_markup=role_users_inline().as_markup())
    await call.message.answer("Управление ролями:", reply_markup=role_manage_menu_inline().as_markup())
    await call.answer("Роль обновлена")


async def open_join_flow(message, state, giveaway):
    current = now_local()
    start_at = iso_to_dt(giveaway["start_at"])
    end_at = iso_to_dt(giveaway["end_at"])
    if giveaway["status"] == "finished" or current >= end_at:
        await safe_send(message, giveaway["finished_text"])
        return
    if current < start_at:
        await safe_send(message, "Регистрация на этот розыгрыш еще не началась.")
        return
    exists = conn.execute("SELECT id FROM participants WHERE giveaway_id=? AND tg_user_id=?", (giveaway["id"], message.from_user.id)).fetchone()
    if exists:
        await safe_send(message, "Вы уже участвуете в этом розыгрыше.")
        return
    await state.clear()
    await state.update_data(giveaway_id=giveaway["id"], username=message.from_user.username)
    await state.set_state(JoinState.first_name)
    await safe_send(message, giveaway["welcome_text"])
    await safe_send(message, "Введи имя на русском языке:", reply_markup=cancel_menu())


@dp.message(Command("join"))
async def join_command(message: Message, state: FSMContext):
    upsert_user(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await safe_send(message, "Использование: /join slug_розыгрыша")
        return
    giveaway = get_giveaway_by_slug(parts[1].strip())
    if not giveaway:
        await safe_send(message, "Розыгрыш не найден.")
        return
    await open_join_flow(message, state, giveaway)


@dp.message(JoinState.first_name, F.text)
async def join_first(message: Message, state: FSMContext):
    if not is_valid_russian_name(message.text.strip()):
        await safe_send(message, "Имя нужно указать только на русском языке. Допустимы буквы, пробел и дефис.")
        return
    await state.update_data(first_name=message.text.strip())
    await state.set_state(JoinState.last_name)
    await safe_send(message, "Введи фамилию на русском языке:")


@dp.message(JoinState.last_name, F.text)
async def join_last(message: Message, state: FSMContext):
    if not is_valid_russian_name(message.text.strip()):
        await safe_send(message, "Фамилию нужно указать только на русском языке. Допустимы буквы, пробел и дефис.")
        return
    await state.update_data(last_name=message.text.strip())
    await state.set_state(JoinState.phone)
    await safe_send(message, "Отправь номер телефона:", reply_markup=phone_menu())


@dp.message(JoinState.phone, F.contact)
async def join_phone_contact(message: Message, state: FSMContext):
    phone = clean_phone(message.contact.phone_number or "")
    if len(phone) < 7:
        await safe_send(message, "Некорректный номер телефона.")
        return
    await state.update_data(phone=phone)
    await state.set_state(JoinState.consent)
    await safe_send(message, f"Для завершения регистрации нужно согласиться на обработку персональных данных.\nПолитика: {PRIVACY_POLICY_URL}",
                    reply_markup=consent_inline().as_markup())


@dp.message(JoinState.phone, F.text)
async def join_phone_text(message: Message, state: FSMContext):
    phone = clean_phone(message.text)
    if len(phone) < 7:
        await safe_send(message, "Некорректный номер телефона.")
        return
    await state.update_data(phone=phone)
    await state.set_state(JoinState.consent)
    await safe_send(message, f"Для завершения регистрации нужно согласиться на обработку персональных данных.\nПолитика: {PRIVACY_POLICY_URL}",
                    reply_markup=consent_inline().as_markup())


@dp.callback_query(JoinState.consent, F.data == "join:consent_yes")
async def join_consent_yes(call: CallbackQuery, state: FSMContext):
    await state.update_data(consent_given=True)
    await state.set_state(JoinState.review)
    await call.message.answer(review_text(await state.get_data()), reply_markup=review_inline().as_markup())
    await call.answer()


@dp.callback_query(JoinState.consent, F.data == "join:consent_no")
async def join_consent_no(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Без согласия участие невозможно.", reply_markup=main_menu(get_user_role(call.from_user.id)))
    await call.answer()


@dp.callback_query(F.data == "join:cancel")
async def join_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Регистрация отменена.", reply_markup=main_menu(get_user_role(call.from_user.id)))
    await call.answer()


@dp.callback_query(JoinState.review, F.data == "join:edit_first_name")
async def join_edit_first(call: CallbackQuery, state: FSMContext):
    await state.set_state(JoinState.edit_first_name)
    await call.message.answer("Введи новое имя на русском языке:", reply_markup=cancel_menu())
    await call.answer()


@dp.callback_query(JoinState.review, F.data == "join:edit_last_name")
async def join_edit_last(call: CallbackQuery, state: FSMContext):
    await state.set_state(JoinState.edit_last_name)
    await call.message.answer("Введи новую фамилию на русском языке:", reply_markup=cancel_menu())
    await call.answer()


@dp.callback_query(JoinState.review, F.data == "join:edit_phone")
async def join_edit_phone(call: CallbackQuery, state: FSMContext):
    await state.set_state(JoinState.edit_phone)
    await call.message.answer("Введи новый телефон или отправь контакт:", reply_markup=phone_menu())
    await call.answer()


@dp.message(JoinState.edit_first_name, F.text)
async def join_save_first(message: Message, state: FSMContext):
    if not is_valid_russian_name(message.text.strip()):
        await safe_send(message, "Имя нужно указать только на русском языке. Допустимы буквы, пробел и дефис.")
        return
    await state.update_data(first_name=message.text.strip())
    await state.set_state(JoinState.review)
    await safe_send(message, review_text(await state.get_data()), reply_markup=review_inline().as_markup())


@dp.message(JoinState.edit_last_name, F.text)
async def join_save_last(message: Message, state: FSMContext):
    if not is_valid_russian_name(message.text.strip()):
        await safe_send(message, "Фамилию нужно указать только на русском языке. Допустимы буквы, пробел и дефис.")
        return
    await state.update_data(last_name=message.text.strip())
    await state.set_state(JoinState.review)
    await safe_send(message, review_text(await state.get_data()), reply_markup=review_inline().as_markup())


@dp.message(JoinState.edit_phone, F.contact)
async def join_save_phone_contact(message: Message, state: FSMContext):
    phone = clean_phone(message.contact.phone_number or "")
    if len(phone) < 7:
        await safe_send(message, "Некорректный номер телефона.")
        return
    await state.update_data(phone=phone)
    await state.set_state(JoinState.review)
    await safe_send(message, review_text(await state.get_data()), reply_markup=review_inline().as_markup())


@dp.message(JoinState.edit_phone, F.text)
async def join_save_phone_text(message: Message, state: FSMContext):
    phone = clean_phone(message.text)
    if len(phone) < 7:
        await safe_send(message, "Некорректный номер телефона.")
        return
    await state.update_data(phone=phone)
    await state.set_state(JoinState.review)
    await safe_send(message, review_text(await state.get_data()), reply_markup=review_inline().as_markup())


@dp.callback_query(JoinState.review, F.data == "join:submit")
async def join_submit(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    giveaway = get_giveaway_by_id(int(data["giveaway_id"]))
    if not giveaway:
        await state.clear()
        await call.message.answer("Розыгрыш не найден.", reply_markup=main_menu(get_user_role(call.from_user.id)))
        await call.answer()
        return
    if now_local() >= iso_to_dt(giveaway["end_at"]):
        await state.clear()
        await call.message.answer(giveaway["finished_text"], reply_markup=main_menu(get_user_role(call.from_user.id)))
        await call.answer()
        return
    already = conn.execute("SELECT id FROM participants WHERE giveaway_id=? AND tg_user_id=?", (giveaway["id"], call.from_user.id)).fetchone()
    if already:
        await state.clear()
        await call.message.answer("Вы уже участвуете в этом розыгрыше.", reply_markup=main_menu(get_user_role(call.from_user.id)))
        await call.answer()
        return
    duplicate_phone = conn.execute("SELECT id FROM participants WHERE giveaway_id=? AND phone=?", (giveaway["id"], data["phone"])).fetchone()
    is_duplicate = 1 if duplicate_phone else 0
    conn.execute(
        """INSERT INTO participants (giveaway_id, tg_user_id, username, first_name, last_name, phone, consent_given, is_duplicate_phone, is_suspicious, source, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'approved', ?)""",
        (giveaway["id"], call.from_user.id, call.from_user.username, data["first_name"], data["last_name"], data["phone"], is_duplicate, is_duplicate, f"deep-link:{giveaway['slug']}", now_iso()),
    )
    conn.commit()
    await state.clear()
    if giveaway["success_image_file_id"]:
        with suppress(Exception):
            await call.message.answer_photo(giveaway["success_image_file_id"], caption=giveaway["success_text"], reply_markup=main_menu(get_user_role(call.from_user.id)))
            await call.answer("Анкета отправлена")
            return
    await call.message.answer(giveaway["success_text"], reply_markup=main_menu(get_user_role(call.from_user.id)))
    await call.answer("Анкета отправлена")


@dp.message()
async def fallback(message: Message):
    upsert_user(message)
    if is_admin(message.from_user.id):
        text = "Используй кнопки меню ниже. Для быстрого старта: добавь канал, создай розыгрыш, потом создай пост."
    else:
        text = "Используй меню ниже или кнопку участия из поста розыгрыша."
    await safe_send(message, text, reply_markup=main_menu(get_user_role(message.from_user.id)))


@dp.errors()
async def global_error_handler(event):
    logger.warning("Необработанная ошибка: %s", event.exception)
    return True


async def ensure_telegram_connection():
    for attempt in range(3):
        try:
            return await bot.get_me()
        except TelegramNetworkError as e:
            if attempt == 2:
                raise RuntimeError("Нет соединения с Telegram API. Проверь доступ к https://api.telegram.org, VPN, провайдера и firewall.") from e
            await asyncio.sleep(2 * (attempt + 1))


async def main():
    init_db()
    set_statuses()
    scheduler.add_job(sync_statuses_job, "interval", minutes=1, id="sync_statuses", replace_existing=True)
    scheduler.start()
    me = await ensure_telegram_connection()
    logger.info("Бот запущен: @%s", me.username)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        with suppress(Exception):
            scheduler.shutdown(wait=False)
        with suppress(Exception):
            await bot.session.close()
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
