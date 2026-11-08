"""Schedule reader for the public KузТАГиС Yandex Disk folder."""
import io
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

PUBLIC_URL = os.getenv("KUZTAGIS_YANDEX_PUBLIC_URL", "https://disk.yandex.ru/d/bbxOH6odZrMXrQ")
API = "https://cloud-api.yandex.net/v1/disk/public/resources"
_CACHE = {"until": 0, "rows": [], "bells": []}
_DAYS = {
    "ПОНЕДЕЛЬНИК": "ПОНЕДЕЛЬНИК", "ВТОРНИК": "ВТОРНИК", "СРЕДА": "СРЕДА",
    "ЧЕТВЕРГ": "ЧЕТВЕРГ", "ПЯТНИЦА": "ПЯТНИЦА", "СУББОТА": "СУББОТА",
}


def _request_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "KuzScheduleBot/1.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def _public_key():
    parsed = urllib.parse.urlparse(PUBLIC_URL)
    if parsed.path.rstrip("/").endswith("/d"):
        return PUBLIC_URL
    return PUBLIC_URL


def _read_public_file(path):
    params = urllib.parse.urlencode({"public_key": _public_key(), "path": path})
    # The public-resource metadata endpoint includes a short-lived direct file
    # URL. Use it instead of /download, which Yandex may rate-limit (429) for
    # public folders even when listing and reading metadata still work.
    meta = _request_json(f"{API}?{params}")
    href = meta.get("file") or meta.get("href")
    if not href:
        raise ValueError(f"Яндекс Диск не вернул ссылку на файл {path}")
    request = urllib.request.Request(href, headers={"User-Agent": "KuzScheduleBot/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _room_value(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text or len(text) > 32:
        return ""
    if (re.fullmatch(r"\d{3,4}(?:[-/]\d+)?", text)
            or re.fullmatch(r"\d+\s*этаж", text, re.I)
            or re.search(r"(?:спорт|тренаж[её]р|гимнаст|мастерск|кабинет|аудитори|зал)", text, re.I)):
        return text
    return ""


def _parse_workbook(content, filename):
    import openpyxl
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    worksheet = workbook[workbook.sheetnames[0]]
    rows = list(worksheet.iter_rows(values_only=True))
    title = str((rows[0][0] if rows and rows[0] else "") or "")
    match = re.search(r"(\d{2}\.\d{2}\.\d{4})", title)
    if not match:
        raise ValueError(f"Не нашёл дату в заголовке файла {filename}")
    schedule_date = datetime.strptime(match.group(1), "%d.%m.%Y").date().isoformat()
    group_starts = (1, 7, 13, 19)
    active_groups = {}
    result = []
    for row_index, row in enumerate(rows):
        row = list(row) + [None] * max(0, 25 - len(row))
        # Group blocks start with a group code (for example, 26-А) in row 5,
        # then repeat every eleven rows in this workbook layout.
        for base in group_starts:
            value = str(row[base] or "").strip()
            if re.match(r"^\d{2,3}[-–—][\wА-Яа-я]+", value):
                active_groups[base] = value
        raw_lesson_no = row[0]
        try:
            lesson_no = int(raw_lesson_no)
        except (TypeError, ValueError):
            continue
        if lesson_no < 1 or row_index + 1 >= len(rows):
            continue
        teacher_row = list(rows[row_index + 1]) + [None] * max(0, 25 - len(rows[row_index + 1]))
        for base in group_starts:
            group = active_groups.get(base)
            if not group:
                continue
            # Each group block has up to three parallel lesson columns. A
            # teacher is in the same column on the following row.
            for subject_col in (base, base + 2, base + 4):
                subject = str(row[subject_col] or "").strip()
                teacher = str(teacher_row[subject_col] or "").strip()
                if not subject or not teacher or subject_col + 1 >= len(row):
                    continue
                if not re.search(r"[А-Яа-яЁё]", subject) or not re.search(r"[А-Яа-яЁё]", teacher):
                    continue
                room = _room_value(row[subject_col + 1])
                if not room:
                    # Some groups put the room in the final merged cell of
                    # their block; use it when there is only one room shown.
                    rooms = [_room_value(row[col]) for col in range(base + 1, base + 6)]
                    rooms = [value for value in rooms if value]
                    room = rooms[0] if len(rooms) == 1 else ""
                result.append({"date": schedule_date, "group": group,
                               "lesson_no": lesson_no, "lesson": str(lesson_no),
                               "subject": subject, "teacher": teacher, "room": room})
    workbook.close()
    return result


def _parse_bells(content):
    from docx import Document
    document = Document(io.BytesIO(content))
    lines = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                lines.append(" · ".join(cells))
    return [{"text": line} for line in lines]


def _refresh():
    """Fetch public files once in a while and retain the last good copy on errors."""
    # Honor the retry window even when the cache is empty; otherwise every
    # get_rows()/get_bells() call retries immediately after a failed fetch.
    if time.monotonic() < _CACHE["until"]:
        return
    try:
        params = urllib.parse.urlencode({"public_key": _public_key(), "limit": 100})
        listing = _request_json(f"{API}?{params}")
        items = listing.get("_embedded", {}).get("items", [])
        rows, bells = [], []
        for item in items:
            name = item.get("name", "")
            path = item.get("path", "")
            if item.get("type") != "file":
                continue
            if name.casefold().endswith(".xlsx"):
                rows.extend(_parse_workbook(_read_public_file(path), name))
            elif name.casefold().endswith(".docx") and "звон" in name.casefold():
                bells = _parse_bells(_read_public_file(path))
        _CACHE.update(rows=rows, bells=bells, until=time.monotonic() + 900)
    except Exception as exc:
        logging.warning("Не удалось обновить расписание КузТАГиС с Яндекс Диска: %s", exc)
        _CACHE["until"] = time.monotonic() + 60


def get_rows():
    _refresh()
    return list(_CACHE["rows"])


def get_bells():
    _refresh()
    return list(_CACHE["bells"])
