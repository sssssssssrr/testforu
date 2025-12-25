-- SQL schema for posts and channels tables
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id INTEGER NOT NULL,
    text TEXT,
    photo_file_id TEXT,
    keyboard_json TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_message_id INTEGER,
    published_link TEXT,
    published_channel TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL UNIQUE,
    title TEXT,
    added_by INTEGER,
    created_at TEXT NOT NULL
);

-- Создание таблицы posts для хранения отправлённых ботом сообщений
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    message_id INTEGER,
    inline_message_id TEXT,
    text TEXT,
    caption TEXT,
    media_type TEXT,
    media_file_id TEXT,
    reply_markup TEXT, -- JSON
    created_by INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);