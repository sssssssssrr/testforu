import pytest
from models import Post
from services import posts
from database import Database

pytestmark = pytest.mark.asyncio


async def setup_db(tmp_path):
    db_file = tmp_path / "testdb.sqlite3"
    db = Database(str(db_file))
    await db.connect()
    await db.execute("""
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
        published_link TEXT
    );
    """)
    return db


async def test_create_and_get_post(tmp_path):
    db = await setup_db(tmp_path)
    import database as database_module
    database_module.db = db

    p = Post(author_id=1, text="Hello", photo_file_id=None, keyboard=[])
    created = await posts.create_post(p)
    assert created.id is not None

    fetched = await posts.get_post(created.id)
    assert fetched is not None
    assert fetched.text == "Hello"

    await db.close()


async def test_update_post(tmp_path):
    db = await setup_db(tmp_path)
    import database as database_module
    database_module.db = db

    p = Post(author_id=2, text="Initial")
    created = await posts.create_post(p)
    await posts.update_post(created.id, text="Updated")
    fetched = await posts.get_post(created.id)
    assert fetched.text == "Updated"
    await db.close()