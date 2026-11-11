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
        await db.execute("""CREATE TABLE IF NOT EXISTS bell_schedule_overrides(
            college_id TEXT PRIMARY KEY, rows_json TEXT NOT NULL,
            source_file TEXT NOT NULL DEFAULT '', updated_by INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS data_migrations(
            name TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS user_settings(
            telegram_id INTEGER PRIMARY KEY,
            hidden_menu TEXT NOT NULL DEFAULT '[]',
            notifications_enabled INTEGER NOT NULL DEFAULT 1,
            schedule_changes INTEGER NOT NULL DEFAULT 1,
            cancellations INTEGER NOT NULL DEFAULT 1,
            transfers INTEGER NOT NULL DEFAULT 1,
            teacher_changes INTEGER NOT NULL DEFAULT 1,
            sports_reminders INTEGER NOT NULL DEFAULT 0,
            class_bells INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE)""")
        setting_columns = {row[1] for row in await (await db.execute("PRAGMA table_info(user_settings)")).fetchall()}
        await db.execute("INSERT OR IGNORE INTO user_settings(telegram_id) SELECT telegram_id FROM users")
        await db.execute("""CREATE TABLE IF NOT EXISTS sports_subscriptions(
            telegram_id INTEGER NOT NULL, section_id TEXT NOT NULL,
            PRIMARY KEY(telegram_id,section_id))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS notification_ledger(
            notification_key TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS auto_schedule_snapshots(
            college_id TEXT NOT NULL, group_name TEXT NOT NULL, schedule_date TEXT NOT NULL,
            rows_json TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(college_id,group_name,schedule_date))""")
        migration_name = "kuzpk_bells_2026_2027"
        applied = await (await db.execute("SELECT 1 FROM data_migrations WHERE name=?", (migration_name,))).fetchone()
        bundled_bells = Path(__file__).resolve().parent / "data" / "kuzpk_bells_2026_2027.json"
        if not applied and bundled_bells.exists():
            rows = json.loads(bundled_bells.read_text(encoding="utf-8"))
            if isinstance(rows, list) and len(rows) >= 20:
                await db.execute("""INSERT INTO bell_schedule_overrides
                    (college_id,rows_json,source_file,updated_by,updated_at)
                    VALUES('kuzpk',?,?,NULL,CURRENT_TIMESTAMP)
                    ON CONFLICT(college_id) DO UPDATE SET rows_json=excluded.rows_json,
                    source_file=excluded.source_file,updated_by=NULL,updated_at=CURRENT_TIMESTAMP""",
                    (json.dumps(rows, ensure_ascii=False), "Расписание_звонков_КузПК_2026-2027.xlsx"))
                await db.execute("INSERT INTO data_migrations(name) VALUES(?)", (migration_name,))
        await db.commit()

async def add_user(uid, name, role="student"):
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("INSERT INTO users(telegram_id,full_name,role) VALUES(?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET full_name=excluded.full_name",
                         (uid, name, role))
        await db.execute("INSERT OR IGNORE INTO user_settings(telegram_id) VALUES(?)", (uid,))
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


DEFAULT_USER_SETTINGS = {
    "hidden_menu": [], "notifications_enabled": 1, "schedule_changes": 1,
    "cancellations": 1, "transfers": 1, "teacher_changes": 1,
    "sports_reminders": 0, "class_bells": 0,
}


async def get_user_settings(uid):
    async with aiosqlite.connect(_database_path()) as db:
        row = await (await db.execute("SELECT * FROM user_settings WHERE telegram_id=?", (uid,))).fetchone()
    if not row:
        return dict(DEFAULT_USER_SETTINGS)
    names = ["telegram_id", "hidden_menu", "notifications_enabled", "schedule_changes",
             "cancellations", "transfers", "teacher_changes", "sports_reminders", "class_bells"]
    values = dict(zip(names, row))
    try:
        values["hidden_menu"] = json.loads(values["hidden_menu"] or "[]")
    except (TypeError, json.JSONDecodeError):
        values["hidden_menu"] = []
    return values


async def update_user_setting(uid, key, value):
    allowed = set(DEFAULT_USER_SETTINGS)
    if key not in allowed:
        raise ValueError("Неизвестная настройка пользователя.")
    if key == "hidden_menu":
        value = json.dumps(sorted(set(value or [])), ensure_ascii=False)
    else:
        value = int(bool(value))
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("INSERT OR IGNORE INTO user_settings(telegram_id) VALUES(?)", (uid,))
        await db.execute(f"UPDATE user_settings SET {key}=? WHERE telegram_id=?", (value, uid))
        await db.commit()


async def toggle_sport_subscription(uid, section_id):
    async with aiosqlite.connect(_database_path()) as db:
        cur = await db.execute("SELECT 1 FROM sports_subscriptions WHERE telegram_id=? AND section_id=?", (uid, section_id))
        exists = await cur.fetchone()
        if exists:
            await db.execute("DELETE FROM sports_subscriptions WHERE telegram_id=? AND section_id=?", (uid, section_id))
            selected = False
        else:
            await db.execute("INSERT INTO sports_subscriptions(telegram_id,section_id) VALUES(?,?)", (uid, section_id))
            selected = True
        await db.commit()
    return selected


async def get_sport_subscriptions(uid):
    async with aiosqlite.connect(_database_path()) as db:
        rows = await (await db.execute("SELECT section_id FROM sports_subscriptions WHERE telegram_id=?", (uid,))).fetchall()
    return {row[0] for row in rows}


async def users_for_sport(section_id):
    async with aiosqlite.connect(_database_path()) as db:
        rows = await (await db.execute("""SELECT s.telegram_id FROM sports_subscriptions s
            JOIN user_settings p ON p.telegram_id=s.telegram_id
            WHERE s.section_id=? AND p.notifications_enabled=1 AND p.sports_reminders=1""", (section_id,))).fetchall()
    return [row[0] for row in rows]


async def users_for_group_preference(group_name, college_id, preference):
    if preference not in {"schedule_changes", "cancellations", "transfers", "teacher_changes", "class_bells"}:
        raise ValueError("Неизвестный тип уведомления.")
    async with aiosqlite.connect(_database_path()) as db:
        rows = await (await db.execute(f"""SELECT u.telegram_id FROM users u
            JOIN user_settings p ON p.telegram_id=u.telegram_id
            WHERE u.group_name=? AND u.college_id=? AND p.notifications_enabled=1 AND p.{preference}=1""",
            (group_name, college_id))).fetchall()
    return [row[0] for row in rows]


async def claim_notification(notification_key):
    async with aiosqlite.connect(_database_path()) as db:
        cur = await db.execute("INSERT OR IGNORE INTO notification_ledger(notification_key) VALUES(?)", (notification_key,))
        await db.commit()
        return cur.rowcount == 1


async def get_auto_schedule_snapshot(college_id, group_name, schedule_date):
    async with aiosqlite.connect(_database_path()) as db:
        row = await (await db.execute("SELECT rows_json FROM auto_schedule_snapshots WHERE college_id=? AND group_name=? AND schedule_date=?",
                                     (college_id, group_name, schedule_date))).fetchone()
    return json.loads(row[0]) if row else None


async def save_auto_schedule_snapshots(college_id, buckets):
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("BEGIN IMMEDIATE")
        for (group_name, schedule_date), rows in buckets.items():
            packed = json.dumps(rows, ensure_ascii=False, sort_keys=True)
            await db.execute("""INSERT INTO auto_schedule_snapshots
                (college_id,group_name,schedule_date,rows_json,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(college_id,group_name,schedule_date) DO UPDATE SET
                rows_json=excluded.rows_json,updated_at=CURRENT_TIMESTAMP""",
                (college_id, group_name, schedule_date, packed))
        await db.commit()


async def get_auto_schedule_snapshots(college_id):
    async with aiosqlite.connect(_database_path()) as db:
        rows = await (await db.execute("SELECT group_name,schedule_date,rows_json FROM auto_schedule_snapshots WHERE college_id=?", (college_id,))).fetchall()
    return {(r[0], r[1]): json.loads(r[2]) for r in rows}


async def users_with_preference(preference):
    if preference not in {"class_bells", "sports_reminders"}:
        raise ValueError("Неизвестный тип уведомления.")
    async with aiosqlite.connect(_database_path()) as db:
        rows = await (await db.execute(f"""SELECT u.telegram_id,u.group_name,u.college_id,u.college_branch
            FROM users u JOIN user_settings p ON p.telegram_id=u.telegram_id
            WHERE p.notifications_enabled=1 AND p.{preference}=1 AND u.group_name IS NOT NULL""")).fetchall()
    return [{"telegram_id": r[0], "group_name": r[1], "college_id": r[2], "college_branch": r[3]} for r in rows]

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


async def save_bells_update(college_id, rows, source_file, updated_by):
    if not rows:
        raise ValueError("В файле не нашлось времени звонков.")
    async with aiosqlite.connect(_database_path()) as db:
        await db.execute("""INSERT INTO bell_schedule_overrides
            (college_id,rows_json,source_file,updated_by,updated_at)
            VALUES(?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(college_id) DO UPDATE SET rows_json=excluded.rows_json,
            source_file=excluded.source_file,updated_by=excluded.updated_by,
            updated_at=CURRENT_TIMESTAMP""",
            (college_id, json.dumps(rows, ensure_ascii=False), source_file, updated_by))
        await db.commit()


async def get_bells_override(college_id):
    async with aiosqlite.connect(_database_path()) as db:
        cur = await db.execute("SELECT rows_json FROM bell_schedule_overrides WHERE college_id=?", (college_id,))
        row = await cur.fetchone()
    return json.loads(row[0]) if row else None
