import aiosqlite, os
from pathlib import Path


def _database_path():
    """Resolve Railway's runtime variable and create its parent directory."""
    path = Path(os.getenv("DATABASE_PATH", "college.db")).expanduser()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)

async def init_db():
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            telegram_id INTEGER PRIMARY KEY, full_name TEXT, group_name TEXT,
            role TEXT DEFAULT 'student', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS announcements(
            id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,
            group_name TEXT, body TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        await db.commit()

async def add_user(uid, name, role="student"):
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("INSERT INTO users(telegram_id,full_name,role) VALUES(?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET full_name=excluded.full_name",
                         (uid, name, role))
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(_database_path()) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE telegram_id=?", (uid,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def set_group(uid, group):
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("UPDATE users SET group_name=? WHERE telegram_id=?", (group, uid))
        await db.commit()

async def set_role(uid, role):
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("UPDATE users SET role=? WHERE telegram_id=?", (role, uid))
        await db.commit()

async def list_groups():
    # Группы берутся из Google Sheets; fallback можно задать в .env.
    from sheets import get_groups
    return await get_groups()

async def users_for_group(group):
    async with aiosqlite.connect(_database_path()) as db:
        cur = await db.execute("SELECT telegram_id FROM users WHERE group_name=?", (group,))
        return [r[0] for r in await cur.fetchall()]

async def all_users():
    async with aiosqlite.connect(_database_path()) as db:
        cur = await db.execute("SELECT telegram_id FROM users")
        return [r[0] for r in await cur.fetchall()]

async def save_announcement(uid, group, body):
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("INSERT INTO announcements(telegram_id,group_name,body) VALUES(?,?,?)", (uid, group, body))
        await db.commit()

async def recent_announcements(limit=10):
    async with aiosqlite.connect(_database_path()) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT group_name,body,created_at FROM announcements ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]
