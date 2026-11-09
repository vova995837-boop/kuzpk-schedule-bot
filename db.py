import aiosqlite, json, os
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
        columns = {row[1] for row in await (await db.execute("PRAGMA table_info(users)")).fetchall()}
        if "college_id" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN college_id TEXT NOT NULL DEFAULT 'kuzpk'")
        if "college_selected" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN college_selected INTEGER NOT NULL DEFAULT 0")
            await db.execute("UPDATE users SET college_selected=1 WHERE group_name IS NOT NULL AND group_name!=''")
        if "college_branch" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN college_branch TEXT")
        announcement_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(announcements)")).fetchall()}
        if "college_id" not in announcement_columns:
            await db.execute("ALTER TABLE announcements ADD COLUMN college_id TEXT NOT NULL DEFAULT 'kuzpk'")
        await db.execute("""CREATE TABLE IF NOT EXISTS schedule_override_days(
            college_id TEXT NOT NULL, college_branch TEXT NOT NULL DEFAULT '',
            group_name TEXT NOT NULL, schedule_date TEXT NOT NULL,
            rows_json TEXT NOT NULL, source_file TEXT NOT NULL DEFAULT '',
            updated_by INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(college_id, college_branch, group_name, schedule_date))""")
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

async def set_college(uid, college_id):
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("UPDATE users SET college_id=?, college_selected=1, group_name=NULL WHERE telegram_id=?", (college_id, uid))
        await db.commit()

async def set_college_branch(uid, branch):
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("UPDATE users SET college_branch=? WHERE telegram_id=?", (branch, uid))
        await db.commit()

async def set_role(uid, role):
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("UPDATE users SET role=? WHERE telegram_id=?", (role, uid))
        await db.commit()

async def list_groups(college_id="kuzpk"):
    # Группы берутся из Google Sheets; fallback можно задать в .env.
    from sheets import get_groups
    return await get_groups(college_id)

async def users_for_group(group, college_id=None):
    async with aiosqlite.connect(_database_path()) as db:
        if college_id:
            cur = await db.execute("SELECT telegram_id FROM users WHERE group_name=? AND college_id=?", (group, college_id))
        else:
            cur = await db.execute("SELECT telegram_id FROM users WHERE group_name=?", (group,))
        return [r[0] for r in await cur.fetchall()]

async def users_for_college(college_id):
    async with aiosqlite.connect(_database_path()) as db:
        cur = await db.execute("SELECT telegram_id FROM users WHERE college_id=? AND college_selected=1", (college_id,))
        return [r[0] for r in await cur.fetchall()]

async def all_users():
    async with aiosqlite.connect(_database_path()) as db:
        cur = await db.execute("SELECT telegram_id FROM users")
        return [r[0] for r in await cur.fetchall()]

async def save_announcement(uid, group, body, college_id="kuzpk"):
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("INSERT INTO announcements(telegram_id,group_name,body,college_id) VALUES(?,?,?,?)", (uid, group, body, college_id))
        await db.commit()

async def recent_announcements(limit=10, college_id="kuzpk"):
    async with aiosqlite.connect(_database_path()) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT group_name,body,created_at FROM announcements WHERE college_id IN (?, 'all') ORDER BY id DESC LIMIT ?", (college_id, limit))
        return [dict(r) for r in await cur.fetchall()]


async def save_schedule_update(college_id, rows, source_file, updated_by, college_branch="", replace_days=None):
    """Replace imported schedule entries by college, group, and day."""
    buckets = {}
    for row in rows:
        group = str(row.get("group") or "").strip()
        schedule_date = str(row.get("date") or "").strip()
        if not group or not schedule_date:
            continue
        buckets.setdefault((group, schedule_date), []).append(row)
    for group, schedule_date in replace_days or []:
        if group and schedule_date:
            buckets.setdefault((str(group).strip(), str(schedule_date).strip()), [])
    if not buckets:
        raise ValueError("В файле не нашлось расписания с группами и датами.")
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("BEGIN IMMEDIATE")
        for (group, schedule_date), day_rows in buckets.items():
            if "преподавател" in str(source_file or "").casefold() or "teacher" in str(source_file or "").casefold():
                cur = await db.execute("""SELECT rows_json FROM schedule_override_days
                    WHERE college_id=? AND college_branch=? AND group_name=? AND schedule_date=?""",
                    (college_id, college_branch or "", group, schedule_date))
                existing = await cur.fetchone()
                if existing:
                    merged = json.loads(existing[0])
                    by_pair = {str(row.get("lesson_no") or ""): row for row in merged}

                    def combine(first, second):
                        values = []
                        for value in (first, second):
                            for part in str(value or "").split(" / "):
                                part = part.strip()
                                if part and part not in values:
                                    values.append(part)
                        return " / ".join(values)

                    for incoming in day_rows:
                        key = str(incoming.get("lesson_no") or "")
                        previous = by_pair.get(key)
                        if previous is None:
                            merged.append(incoming)
                            by_pair[key] = incoming
                            continue
                        for field in ("subject", "teacher", "room"):
                            previous[field] = combine(previous.get(field), incoming.get(field))
                        for field in ("lesson", "start", "end"):
                            if incoming.get(field) and not previous.get(field):
                                previous[field] = incoming[field]
                    day_rows = merged
            await db.execute("""INSERT INTO schedule_override_days
                (college_id,college_branch,group_name,schedule_date,rows_json,source_file,updated_by,updated_at)
                VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(college_id,college_branch,group_name,schedule_date) DO UPDATE SET
                rows_json=excluded.rows_json,source_file=excluded.source_file,
                updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP""",
                (college_id, college_branch or "", group, schedule_date,
                 json.dumps(day_rows, ensure_ascii=False), source_file, updated_by))
        await db.commit()
    return len(buckets)


async def get_schedule_override(college_id, group_name, schedule_date, college_branch=""):
    async with aiosqlite.connect(_database_path()) as db:
        cur = await db.execute("""SELECT rows_json FROM schedule_override_days
            WHERE college_id=? AND college_branch IN (?, '') AND group_name=? AND schedule_date=?
            ORDER BY CASE WHEN college_branch=? THEN 0 ELSE 1 END LIMIT 1""",
            (college_id, college_branch or "", group_name, schedule_date, college_branch or ""))
        row = await cur.fetchone()
    return json.loads(row[0]) if row else None


async def get_schedule_overrides(college_id, start_date, end_date, college_branch=""):
    async with aiosqlite.connect(_database_path()) as db:
        cur = await db.execute("""SELECT group_name,schedule_date,rows_json FROM schedule_override_days
            WHERE college_id=? AND college_branch IN (?, '') AND schedule_date>=? AND schedule_date<=?
            ORDER BY CASE WHEN college_branch='' THEN 0 ELSE 1 END""",
            (college_id, college_branch or "", start_date, end_date))
        return {(r[0], r[1]): json.loads(r[2]) for r in await cur.fetchall()}


async def schedule_override_groups(college_id, college_branch=""):
    async with aiosqlite.connect(_database_path()) as db:
        cur = await db.execute("SELECT DISTINCT group_name FROM schedule_override_days WHERE college_id=? AND college_branch IN (?, '')",
                               (college_id, college_branch or ""))
        return [row[0] for row in await cur.fetchall()]
