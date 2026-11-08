import io, os, json, re, time, logging, asyncio
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
load_dotenv()
from drive_store import list_files as drive_files, download_file as drive_download

SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
CREDS = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
DEMO_GROUPS = [x.strip() for x in os.getenv("GROUPS", "2411").split(",") if x.strip()]
LOCAL_XLSX = os.getenv("SCHEDULE_XLSX", "Расписание_КузПК_09-14.11.2026.xlsx")
TEACHERS_XLSX = os.getenv("SCHEDULE_TEACHERS_XLSX", "расписание_7.11_преподаватели.xlsx")
GROUPS_XLSX = os.getenv("SCHEDULE_GROUPS_XLSX", "расписание_7.11_группы.xlsx")
UNIFIED_XLSX = os.getenv("SCHEDULE_UNIFIED_XLSX", "Расписание_КузПК_единое_09-14.11.2026.xlsx")
_NEW_SCHEDULE_CACHE = None
_NEW_SCHEDULE_CACHE_TIME = 0.0

def _drive_workbook(filename, view=None):
    """Find and open a matching workbook in the configured Drive folder."""
    try:
        files = [f for f in drive_files() if f.get("name", "").lower().endswith((".xlsx", ".xlsm"))]
        target = os.path.basename(filename).casefold()
        match = next((f for f in files if f["name"].casefold() == target), None)
        if not match and view == "group":
            match = next((f for f in files if "групп" in f["name"].casefold()), None)
        if not match and view == "teacher":
            match = next((f for f in files if "преподавател" in f["name"].casefold()), None)
        if not match:
            return None
        import openpyxl
        return openpyxl.load_workbook(io.BytesIO(drive_download(match["id"])), read_only=True, data_only=True)
    except Exception as exc:
        logging.warning("Не удалось прочитать книгу из Google Drive: %s", exc)
        return None

def _unified_workbook():
    """Load the consolidated schedule workbook, if it is present in Drive."""
    try:
        files = [f for f in drive_files() if f.get("name", "").lower().endswith((".xlsx", ".xlsm"))]
        unified_files = [f for f in files if "единое" in f["name"].casefold()]
        match = max(unified_files, key=lambda f: f.get("modifiedTime", ""), default=None)
        import openpyxl
        if match:
            return openpyxl.load_workbook(io.BytesIO(drive_download(match["id"])), read_only=True, data_only=True)
        candidates = [UNIFIED_XLSX] if os.path.isabs(UNIFIED_XLSX) else [
            os.path.join(os.path.dirname(__file__), UNIFIED_XLSX),
            os.path.join(os.path.expanduser("~/Downloads"), UNIFIED_XLSX),
        ]
        path = next((candidate for candidate in candidates if os.path.exists(candidate)), None)
        return openpyxl.load_workbook(path, read_only=True, data_only=True) if path else None
    except Exception as exc:
        logging.warning("Не удалось прочитать единое расписание из Google Drive: %s", exc)
        return None

def _unified_rows(workbook):
    """Convert the unified workbook's group sheet into the bot's schedule format."""
    try:
        ws = workbook["3. Расписание пар (группы)"]
        result = []
        for values in ws.iter_rows(min_row=4, values_only=True):
            if not values or not values[0] or not values[4]:
                continue
            try:
                raw_date = str(values[0]).strip()
                schedule_date = datetime.strptime(raw_date, "%d.%m.%Y").date()
            except (TypeError, ValueError):
                continue
            result.append({
                "date": schedule_date.isoformat(), "group": str(values[4]).strip(),
                "lesson_no": values[2], "lesson": str(values[3] or "").strip(),
                "subject": str(values[5] or "").strip(), "teacher": str(values[6] or "").strip(),
                "room": str(values[7] or "").strip(),
            })
        return _collapse_lessons(result)
    except Exception as exc:
        logging.warning("Не удалось обработать лист расписания групп: %s", exc)
        return []

def _open_workbook(filename, view=None):
    """Prefer the shared Drive copy; fall back to the local XLSX file."""
    import openpyxl
    workbook = _drive_workbook(filename, view)
    if workbook:
        return workbook
    path = filename if os.path.isabs(filename) else os.path.join(os.path.dirname(__file__), filename)
    if not os.path.exists(path):
        return None
    return openpyxl.load_workbook(path, read_only=True, data_only=True)

def _local_rows(tab):
    """Read the provided workbook when Google Sheets is not configured."""
    try:
        workbook = _open_workbook(LOCAL_XLSX)
        if workbook is None:
            return []
        ws = workbook[tab]
        if tab == "Расписание пар":
            headers = [str(v or "").strip() for v in next(ws.iter_rows(min_row=4, max_row=4, values_only=True))]
            return [dict(zip(headers, row)) for row in ws.iter_rows(min_row=5, values_only=True) if row[0]]
        if tab == "Расписание звонков":
            # Preserve the source sheet's labeled time blocks as readable rows.
            return [{"text": " | ".join(str(v) for v in row if v is not None)}
                    for row in ws.iter_rows(min_row=4, values_only=True) if row[0] is not None]
    except Exception:
        return []
    return []

def _service():
    if not SHEET_ID or not CREDS:
        return None
    import gspread
    info = json.loads(CREDS)
    return gspread.service_account_from_dict(info).open_by_key(SHEET_ID)

def _collapse_lessons(rows):
    """Merge the two lesson slots that make up one numbered college pair."""
    buckets = {}
    for row in rows:
        pair = str(row.get("lesson_no") or "").strip()
        group = str(row.get("group") or "").strip()
        pair_key = pair or str(row.get("lesson") or len(buckets))
        # A pair number repeats on every day; keep different dates separate.
        key = (str(row.get("date") or "").strip(), pair_key, group)
        buckets.setdefault(key, []).append(row)
    merged = []
    for (_day, pair, _group), items in buckets.items():
        first = dict(items[0])
        lessons = sorted({int(str(r.get("lesson"))) for r in items
                          if str(r.get("lesson", "")).isdigit()})
        subjects, teachers, rooms = [], [], []
        for r in items:
            subject = str(r.get("subject") or "").strip()
            teacher = str(r.get("teacher") or "").strip()
            # Some source rows put the teacher's full name in both fields.
            # Keep real subjects such as "Практика", which can also be used
            # as a placeholder in the teacher row.
            tokens = subject.replace("-", " ").split()
            looks_like_person = (2 <= len(tokens) <= 3 and all(token[:1].isupper() for token in tokens))
            repeated_person = subject.casefold() == teacher.casefold() and looks_like_person
            if subject and not repeated_person and subject not in subjects:
                subjects.append(subject)
            if teacher and teacher.casefold() != subject.casefold() and teacher not in teachers:
                teachers.append(teacher)
            room = str(r.get("room") or "").strip()
            if room and room not in rooms: rooms.append(room)
        first["lesson_no"] = pair or first.get("lesson_no")
        first["lesson"] = f"{lessons[0]}–{lessons[-1]}" if len(lessons) > 1 else (str(lessons[0]) if lessons else "")
        first["subject"] = " / ".join(subjects) if subjects else "Занятие"
        first["teacher"] = " / ".join(teachers) if teachers else ""
        first["room"] = " / ".join(rooms) if rooms else ""
        merged.append(first)
    return merged

def _new_schedule_rows():
    """Convert the group and teacher weekly workbooks into schedule records."""
    global _NEW_SCHEDULE_CACHE, _NEW_SCHEDULE_CACHE_TIME
    if _NEW_SCHEDULE_CACHE is not None and time.monotonic() - _NEW_SCHEDULE_CACHE_TIME < 120:
        return _NEW_SCHEDULE_CACHE
    try:
        unified = _unified_workbook()
        if unified is not None:
            unified_rows = _unified_rows(unified)
            if unified_rows:
                _NEW_SCHEDULE_CACHE = unified_rows
                _NEW_SCHEDULE_CACHE_TIME = time.monotonic()
                return _NEW_SCHEDULE_CACHE
        group_output, teacher_output = [], []
        for filename, view in ((GROUPS_XLSX, "group"), (TEACHERS_XLSX, "teacher")):
            workbook = _open_workbook(filename, view)
            if workbook is None: continue
            ws = workbook["Текущее расписание"]
            raw = list(ws.iter_rows(values_only=True))
            # The date headers are on row 8, one per weekday block. Excel
            # exports them as e.g. "09.11.2026_x000D_\\nПОНЕДЕЛЬНИК".
            date_headers = raw[7] if len(raw) > 7 else ()
            day_blocks = []
            for base in range(5, len(date_headers), 5):
                value = str(date_headers[base] or "")
                match = re.search(r"(\d{2}\.\d{2}\.\d{4})", value)
                if match:
                    try:
                        day_blocks.append((base, datetime.strptime(match.group(1), "%d.%m.%Y").date()))
                    except ValueError:
                        pass
            if not day_blocks: continue
            owner = None
            for idx in range(12, len(raw)-1):
                row, following = raw[idx], raw[idx+1]
                first = str(row[0]).strip() if row[0] is not None else ""
                if first and first not in ("Преподаватель", "Отде- ление, группа"):
                    owner = first
                try:
                    pair = int(row[1])
                    lesson = int(row[2])
                except (TypeError, ValueError, IndexError):
                    continue
                if pair < 1 or lesson < 1 or not owner: continue
                for base, schedule_date in day_blocks:
                    subject_columns = (base, base + 2) if view == "group" else (base,)
                    for subject_column in subject_columns:
                        subject_value = row[subject_column] if len(row) > subject_column else None
                        subject = str(subject_value).strip() if subject_value is not None else ""
                        if not subject: continue
                        related = following[subject_column] if len(following) > subject_column else None
                        room_column = base + 3
                        room_value = row[room_column] if len(row) > room_column else None
                        room = str(room_value).strip() if room_value is not None else ""
                        if view == "group":
                            group, teacher = owner, str(related).strip() if related is not None else ""
                        else:
                            teacher, group = owner, str(related).strip() if related is not None else ""
                        if not group: continue
                        record = {"date": schedule_date.isoformat(), "group": group,
                                  "lesson_no": pair, "lesson": "", "subject": subject,
                                  "teacher": teacher, "room": room}
                        (group_output if view == "group" else teacher_output).append(record)
        # The group workbook is the primary view; the teacher workbook fills
        # only group/periods absent from it, avoiding duplicate subgroup rows.
        covered = {(r["group"], str(r["lesson_no"]), r["date"]) for r in group_output}
        supplemental = [r for r in teacher_output
                        if (r["group"], str(r["lesson_no"]), r["date"]) not in covered]
        _NEW_SCHEDULE_CACHE = _collapse_lessons(group_output + supplemental)
        _NEW_SCHEDULE_CACHE_TIME = time.monotonic()
    except Exception:
        _NEW_SCHEDULE_CACHE = []
        _NEW_SCHEDULE_CACHE_TIME = time.monotonic()
    return _NEW_SCHEDULE_CACHE

async def _values(tab):
    try:
        sh = _service()
        if not sh: return []
        ws = sh.worksheet(tab)
        vals = ws.get_all_records()
        return [dict(x) for x in vals]
    except Exception:
        return []

async def get_groups(college_id="kuzpk"):
    if college_id == "pgt":
        from pgt import get_groups as pgt_groups
        return await pgt_groups()
    if college_id == "kuztagis":
        from kuztagis import get_rows
        rows = await asyncio.to_thread(get_rows)
        return sorted({r["group"] for r in rows if r.get("group")})
    if college_id == "yamk":
        from yamk import get_groups as yamk_groups
        result = []
        for branch in ("osgp", "oenp", "otp", "osep"):
            groups = await asyncio.to_thread(yamk_groups, branch)
            result.extend(r["name"] for r in groups)
        return sorted(set(result))
    new_rows = _new_schedule_rows()
    if new_rows:
        return sorted({str(r["group"]).strip() for r in new_rows if r.get("group")})
    rows = await _values("Группы")
    found = [str(r.get("group","")).strip() for r in rows if r.get("group")]
    rows = _local_rows("Расписание пар")
    found.extend(str(r.get("Группа", "")).strip() for r in rows if r.get("Группа"))
    found.extend(str(r.get("group", "")).strip() for r in _new_schedule_rows() if r.get("group"))
    return sorted(set(found)) or DEMO_GROUPS

async def get_schedule(group, day: date, college_id="kuzpk", college_branch=None):
    if college_id == "pgt":
        from pgt import get_group_rows
        rows = await get_group_rows(group)
        return [r for r in rows if r.get("date") == day.isoformat()]
    if college_id == "kuztagis":
        from kuztagis import get_rows
        rows = await asyncio.to_thread(get_rows)
        return _collapse_lessons([r for r in rows if r.get("date") == day.isoformat() and r.get("group") == group])
    if college_id == "yamk":
        from yamk import get_group_schedule
        rows = await asyncio.to_thread(get_group_schedule, group, college_branch or "otp")
        return [r for r in rows if r.get("date") == day.isoformat()]
    new_rows = _new_schedule_rows()
    if new_rows:
        return _collapse_lessons([r for r in new_rows if r["date"] == day.isoformat() and r["group"] == group])
    rows = await _values("Расписание")
    if rows:
        result = [r for r in rows if str(r.get("group","")).strip() == group
            and str(r.get("date","")).strip() in (day.isoformat(), day.strftime("%d.%m.%Y"))]
        result.extend(r for r in _new_schedule_rows() if r["date"] == day.isoformat() and r["group"] == group)
        return _collapse_lessons(result)
    rows = _local_rows("Расписание пар")
    result = []
    for r in rows:
        try:
            raw_date = r.get("Дата", "")
            row_date = datetime.strptime(str(raw_date), "%d.%m.%Y").date() if "." in str(raw_date) else date.fromisoformat(str(raw_date))
        except Exception: continue
        if str(r.get("Группа", "")).strip() == group and row_date == day:
            result.append({"lesson_no": r.get("Пара"), "subject": r.get("Дисциплина"),
                           "teacher": r.get("Преподаватель"), "room": r.get("Аудитория"),
                           "lesson": r.get("Урок"), "group": r.get("Группа")})
    result.extend(r for r in _new_schedule_rows() if r["date"] == day.isoformat() and r["group"] == group)
    return _collapse_lessons(result)

async def get_week_schedule(group, monday: date, college_id="kuzpk", college_branch=None):
    """Load one week's schedule in a single source read, indexed by date."""
    days = [monday + timedelta(days=offset) for offset in range(7)]
    by_day = {day: [] for day in days}
    if college_id == "pgt":
        from pgt import get_group_rows
        for row in await get_group_rows(group):
            try:
                row_day = date.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            if row_day in by_day:
                by_day[row_day].append(row)
        return by_day
    if college_id == "kuztagis":
        from kuztagis import get_rows
        rows = await asyncio.to_thread(get_rows)
        for row in rows:
            if row.get("group") != group:
                continue
            try:
                row_day = date.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            if row_day in by_day:
                by_day[row_day].append(row)
        return {day: _collapse_lessons(day_rows) for day, day_rows in by_day.items()}
    if college_id == "yamk":
        from yamk import get_group_schedule
        rows = await asyncio.to_thread(get_group_schedule, group, college_branch or "otp")
        for row in rows:
            try:
                row_day = date.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            if row_day in by_day:
                by_day[row_day].append(row)
        return {day: _collapse_lessons(day_rows) for day, day_rows in by_day.items()}
    new_rows = _new_schedule_rows()
    if new_rows:
        for row in new_rows:
            if row.get("group") != group: continue
            try:
                row_day = date.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            if row_day in by_day: by_day[row_day].append(row)
        return {day: _collapse_lessons(day_rows) for day, day_rows in by_day.items()}
    rows = await _values("Расписание")
    if rows:
        for row in rows:
            if str(row.get("group", "")).strip() != group:
                continue
            raw_date = str(row.get("date", "")).strip()
            for day in days:
                if raw_date in (day.isoformat(), day.strftime("%d.%m.%Y")):
                    by_day[day].append(row)
                    break
    else:
        for row in _local_rows("Расписание пар"):
            if str(row.get("Группа", "")).strip() != group:
                continue
            try:
                raw_date = str(row.get("Дата", ""))
                row_day = datetime.strptime(raw_date, "%d.%m.%Y").date() if "." in raw_date else date.fromisoformat(raw_date)
            except Exception:
                continue
            if row_day in by_day:
                by_day[row_day].append({"lesson_no": row.get("Пара"), "subject": row.get("Дисциплина"),
                    "teacher": row.get("Преподаватель"), "room": row.get("Аудитория"),
                    "lesson": row.get("Урок"), "group": row.get("Группа")})
    for row in _new_schedule_rows():
        if row.get("group") != group:
            continue
        try:
            row_day = date.fromisoformat(row["date"])
        except (KeyError, ValueError):
            continue
        if row_day in by_day:
            by_day[row_day].append(row)
    return {day: _collapse_lessons(day_rows) for day, day_rows in by_day.items()}

async def get_teachers(college_id="kuzpk", college_branch=None):
    if college_id == "pgt":
        from pgt import get_entity_names
        return [{"teacher": name} for name in await get_entity_names("teacher")]
    if college_id == "kuztagis":
        from kuztagis import get_rows
        rows = await asyncio.to_thread(get_rows)
        return [{"teacher": r["teacher"], "subject": r["subject"], "groups": r["group"]}
                for r in rows if r.get("teacher")]
    if college_id == "yamk":
        from yamk import get_teachers as yamk_teachers
        return await asyncio.to_thread(yamk_teachers)
    new_rows = _new_schedule_rows()
    if new_rows:
        return [{"teacher": r["teacher"], "subject": r["subject"], "groups": r["group"]}
                for r in new_rows if r.get("teacher")]
    rows = await _values("Преподаватели")
    local = [{"teacher": r.get("Преподаватель"), "subject": r.get("Дисциплина"), "groups": r.get("Группа")}
             for r in _local_rows("Расписание пар") if r.get("Преподаватель")]
    local += [{"teacher": r["teacher"], "subject": r["subject"], "groups": r["group"]}
              for r in _new_schedule_rows() if r.get("teacher")]
    return rows + local
async def get_rooms(college_id="kuzpk", college_branch=None):
    if college_id == "pgt":
        from pgt import get_entity_names
        return [{"room": name} for name in await get_entity_names("room")]
    if college_id == "kuztagis":
        from kuztagis import get_rows
        rows = await asyncio.to_thread(get_rows)
        return [{"room": room} for room in {r.get("room") for r in rows if r.get("room")}]
    if college_id == "yamk":
        from yamk import get_entity_rows
        rows = await asyncio.to_thread(get_entity_rows, "room", "", college_branch)
        return [{"room": room} for room in sorted({r.get("room") for r in rows if r.get("room")})]
    new_rows = _new_schedule_rows()
    if new_rows:
        return [{"room": room} for room in {r.get("room") for r in new_rows if r.get("room")}]
    rows = await _values("Аудитории")
    local = [{"room": room} for room in {r.get("Аудитория") for r in _local_rows("Расписание пар") if r.get("Аудитория")}]
    local += [{"room": r["room"]} for r in _new_schedule_rows() if r.get("room")]
    return rows + local
async def get_bells(college_id="kuzpk"):
    if college_id == "pgt":
        return []
    if college_id == "kuztagis":
        from kuztagis import get_bells as kuztagis_bells
        return await asyncio.to_thread(kuztagis_bells)
    rows = await _values("Звонки")
    if rows:
        return rows
    return _local_bells()

def _local_bells():
    try:
        import openpyxl
        workbook = _unified_workbook()
        if workbook is not None:
            ws = workbook["1. Расписание звонков"]
            items = []
            section = ""
            for row in ws.iter_rows(min_row=3, values_only=True):
                cells = [str(v).strip() if v is not None else "" for v in row]
                first = cells[0] if cells else ""
                if not first:
                    continue
                if first == "Смена":
                    continue
                if len(cells) > 1 and cells[1] and ("смена" in first.casefold()):
                    items.append({"section": section, "shift": first, "lesson": cells[1],
                                  "first": cells[2] if len(cells) > 2 else "",
                                  "break_1": cells[3] if len(cells) > 3 else "",
                                  "second": cells[4] if len(cells) > 4 else "",
                                  "break_2": cells[5] if len(cells) > 5 else "",
                                  "note": cells[6] if len(cells) > 6 else ""})
                elif len(cells) > 1 and not cells[1]:
                    section = first
            return items
        path = LOCAL_XLSX if os.path.isabs(LOCAL_XLSX) else os.path.join(os.path.dirname(__file__), LOCAL_XLSX)
        if not os.path.exists(path): return []
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True)["Расписание звонков"]
        section, items, notes = "", [], []
        for row in ws.iter_rows(min_row=4, values_only=True):
            cells = [str(v).strip() if v is not None else "" for v in row]
            first = cells[0]
            if not first: continue
            if first == "Смена": continue
            if first.startswith("Примечание:"):
                notes.append(first); continue
            if first in ("1 смена", "2 смена") and cells[1]:
                items.append({"section": section, "shift": first, "lesson": cells[1],
                              "first": cells[2], "break_1": cells[3], "second": cells[4],
                              "break_2": cells[5], "note": cells[6]})
            elif not cells[1]:
                section = first
        if items and notes: items[-1]["general_note"] = " ".join(notes)
        return items
    except Exception:
        return []

async def get_entity_names(kind, college_id="kuzpk", college_branch=None):
    if kind == "teacher":
        rows = await get_teachers(college_id, college_branch)
        key = "teacher"
    else:
        rows = await get_rooms(college_id, college_branch)
        key = "room"
    names = {str(r.get(key, "")).strip() for r in rows if r.get(key)}
    if kind == "room" and college_id == "kuzpk":
        names = {name for name in names if room_building(name) in (1, 2, 3, 4)}
    return sorted(names)

def room_building(room):
    value = str(room).strip().casefold()
    if any(word in value for word in ("спортзал", "гимн", "тр/зал", "бор/", "бокс")):
        return 3
    if len(value) > 2 and value[0].isdigit() and value[1] == "-":
        return int(value[0])
    return 0

async def get_entity_schedule(kind, value, day: date | None, college_id="kuzpk", college_branch=None):
    if college_id == "pgt":
        from pgt import get_entity_rows
        rows = await get_entity_rows(kind, value)
        return rows if day is None else [r for r in rows if r.get("date") == day.isoformat()]
    if college_id == "kuztagis":
        from kuztagis import get_rows
        all_rows = await asyncio.to_thread(get_rows)
        new_rows = [r for r in all_rows if day is None or r.get("date") == day.isoformat()]
        key = "teacher" if kind == "teacher" else "room"
        result = [{k: r.get(k) for k in ("date", "lesson_no", "lesson", "subject", "teacher", "room", "group")}
                  for r in new_rows if str(r.get(key, "")).strip() == value]
        return _collapse_lessons(result)
    if college_id == "yamk":
        from yamk import get_entity_rows
        rows = await asyncio.to_thread(get_entity_rows, kind, value, college_branch)
        return rows if day is None else [row for row in rows if row.get("date") == day.isoformat()]
    new_rows = _new_schedule_rows()
    if new_rows:
        key = "teacher" if kind == "teacher" else "room"
        return _collapse_lessons([{k: r.get(k) for k in ("lesson_no", "lesson", "subject", "teacher", "room", "group", "start", "end")}
                                  for r in new_rows if r["date"] == day.isoformat() and str(r.get(key, "")).strip() == value])
    rows = await _values("Расписание")
    if rows:
        result = []
        for r in rows:
            raw_date = str(r.get("date", "")).strip()
            if raw_date not in (day.isoformat(), day.strftime("%d.%m.%Y")): continue
            key = "teacher" if kind == "teacher" else "room"
            if str(r.get(key, "")).strip() == value:
                result.append({"lesson_no": r.get("lesson_no"), "lesson": r.get("lesson"),
                              "subject": r.get("subject"), "teacher": r.get("teacher"),
                              "room": r.get("room"), "group": r.get("group"),
                              "start": r.get("start"), "end": r.get("end")})
        key = "teacher" if kind == "teacher" else "room"
        result.extend({k: r.get(k) for k in ("lesson_no", "lesson", "subject", "teacher", "room", "group", "start", "end")}
                      for r in _new_schedule_rows() if r["date"] == day.isoformat() and str(r.get(key, "")).strip() == value)
        return _collapse_lessons(result)
    result = []
    key = "Преподаватель" if kind == "teacher" else "Аудитория"
    for r in _local_rows("Расписание пар"):
        try:
            raw = str(r.get("Дата", ""))
            row_day = datetime.strptime(raw, "%d.%m.%Y").date() if "." in raw else date.fromisoformat(raw)
        except Exception:
            continue
        if row_day == day and str(r.get(key, "")).strip() == value:
            result.append({"lesson_no": r.get("Пара"), "lesson": r.get("Урок"),
                           "subject": r.get("Дисциплина"), "teacher": r.get("Преподаватель"),
                           "room": r.get("Аудитория"), "group": r.get("Группа")})
    key_field = "teacher" if kind == "teacher" else "room"
    result.extend({k: r.get(k) for k in ("lesson_no", "lesson", "subject", "teacher", "room", "group", "start", "end")}
                  for r in _new_schedule_rows() if r["date"] == day.isoformat() and str(r.get(key_field, "")).strip() == value)
    return _collapse_lessons(result)
