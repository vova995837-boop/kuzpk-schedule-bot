"""Public schedule reader for Yamalsky Multidisciplinary College (YAMK)."""

import logging
import io
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://xn----8sbaqd1aje6bf1c2g.xn--p1ai/"
DEPARTMENTS = {
    "osgp": ("ОСГП", "Общественно-социально-гуманитарный профиль"),
    "oenp": ("ОЕНП", "Общественно-естественно-научный профиль"),
    "otp": ("ОТП", "Отделение технического профиля"),
    "osep": ("ОСЭП", "Общественно-социально-экономический профиль"),
}
ACTIVITY_URL = "https://ymk.yanao.ru/activity/38207/"
CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "yamk_cache.json")
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://ymk.yanao.ru/",
})
_CACHE_TTL = 300
_TEACHERS = None
_TEACHERS_UNTIL = 0.0
_TEACHER_ROWS = {}
_GROUPS = {}
_DEPARTMENT_DATA = {}
_ENTITY_ROWS = {}
_PUBLIC_DOCUMENTS = None
_PUBLIC_DOCUMENTS_UNTIL = 0.0
_PUBLIC_DOCUMENTS_STALE = False
_FALLBACK_CACHE = None
_GROUP_RE = re.compile(r"(?<!\d)(\d{2}\s*[А-ЯЁA-Z]{1,8}(?:\s*/?\d+)?)", re.IGNORECASE)
_TEACHER_RE = re.compile(r"(?<![А-ЯЁа-яё])([А-ЯЁ][А-ЯЁа-яё-]{1,})\s+([А-ЯЁ]\.\s*(?:[А-ЯЁ]\.)?)")
_TIME_RE = re.compile(r"(\d{1,2})[.:](\d{2})\s*[-–]\s*(\d{1,2})[.:](\d{2})")
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
    "июня": 6, "июля": 7, "августа": 8, "сентября": 9,
    "октября": 10, "ноября": 11, "декабря": 12,
}


def _cell_text(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def _fallback_cache():
    """Read the last committed schedule/doc snapshot when the college site is unreachable."""
    global _FALLBACK_CACHE
    if _FALLBACK_CACHE is None:
        try:
            with open(CACHE_FILE, encoding="utf-8") as cache_file:
                _FALLBACK_CACHE = json.load(cache_file)
        except (OSError, ValueError):
            _FALLBACK_CACHE = {"branches": {}, "documents": []}
    return _FALLBACK_CACHE


def _date_value(value):
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", _cell_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(".".join(match.groups()), "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def _date_from_pdf_cell(value, year=None):
    text = _cell_text(value)
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    # OSEP writes dates backwards in its day header (for example 6202.11.20).
    for token in re.findall(r"\d{4}\.\d{1,2}\.\d{1,2}", compact):
        try:
            return datetime.strptime(token[::-1], "%d.%m.%Y").date().isoformat()
        except ValueError:
            pass
    # OENP's vertically merged date cell is ordered year, month, day.
    match = re.search(r"(20\d{2})\D{0,4}(\d{1,2})\.(\d{1,2})\.?", compact)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date().isoformat()
        except ValueError:
            pass
    normal = _date_value(text)
    if normal:
        return normal
    if year:
        month_match = re.search(r"([А-ЯЁ]{4,})\s*/\s*(\d{1,2})", text, re.IGNORECASE)
        if month_match:
            month_name = month_match.group(1)[::-1].lower()
            try:
                month = _MONTHS[month_name]
                day = int(month_match.group(2)[::-1])
                return datetime(year, month, day).date().isoformat()
            except (KeyError, ValueError):
                pass
    return None


def _pdf_week_start(text):
    match = re.search(
        r"(\d{1,2})\s+([А-ЯЁ]+)\s*(?:ПО|[-–])\s*\d{1,2}\s+([А-ЯЁ]+).*?(20\d{2})",
        text.upper(), re.IGNORECASE,
    )
    if not match:
        return None
    month_name = match.group(2).lower()
    month = _MONTHS.get(month_name)
    if not month:
        return None
    try:
        from datetime import date
        return date(int(match.group(4)), month, int(match.group(1)))
    except ValueError:
        return None


def _table_and_header(page, branch):
    tables = page.extract_tables()
    if not tables:
        return None, None
    table = max(tables, key=lambda value: max((len(row) for row in value), default=0))
    for index, row in enumerate(table):
        cells = [_cell_text(cell) for cell in row]
        if branch == "osep":
            if any("время" in cell.casefold() and "группа" in cell.casefold() for cell in cells):
                return table, index
        elif any(cell.casefold() == "время" for cell in cells) and sum(bool(_GROUP_RE.search(cell)) for cell in cells) >= 2:
            return table, index
    return None, None


def _teacher_name(line):
    match = _TEACHER_RE.search(line)
    if not match:
        return None, None
    initials = re.sub(r"\s+", "", match.group(2))
    return f"{match.group(1)} {initials}", match


def _room_from_text(text):
    match = re.search(r"\b\d{2,3}\s*[А-ЯЁA-Z]\b|АКТ\.?\s*З|КОНФ\.?\s*З|\bЗАЛ\b", text, re.IGNORECASE)
    return _cell_text(match.group(0)) if match else ""


def _parse_group_cell(value, room_value, state, current_date, group_name, specialty, time_text, records):
    lines = [line.strip() for line in str(value or "").replace("\xa0", " ").splitlines() if line.strip()]
    room_value = _cell_text(room_value)
    if room_value and room_value.casefold() not in {"кабинет", "тенибак", "инд"}:
        state["room"] = room_value
    time_matches = list(_TIME_RE.finditer(time_text))
    start = f"{time_matches[0].group(1)}:{time_matches[0].group(2)}" if time_matches else ""
    end = f"{time_matches[-1].group(3)}:{time_matches[-1].group(4)}" if time_matches else ""
    if "поднятие флага" in " ".join(lines).casefold() and time_matches:
        end = f"{time_matches[0].group(3)}:{time_matches[0].group(4)}"
    has_subject_on_row = False
    for line in lines:
        line = _cell_text(line)
        if room_value and line.casefold() == room_value.casefold():
            continue
        if _room_from_text(line) and re.fullmatch(r"(?:\d{2,3}\s*[А-ЯЁA-Z]|АКТ\.?\s*З|КОНФ\.?\s*З|ЗАЛ)", line, re.IGNORECASE):
            continue
        teachers = list(_TEACHER_RE.finditer(line))
        if teachers:
            subject = _cell_text(line[:teachers[0].start()]).strip(" -/;:")
            if subject and subject.casefold() not in {"поднятие флага", "разговор о важном"}:
                subject = re.sub(r"^\d+(?:\s+\d+|\s*[-–]\s*\d+)?\s*(?:[/–.-])?\s*", "", subject)
                state["subject"] = subject or state.get("subject", "")
                has_subject_on_row = True
            tail = line[teachers[-1].end():]
            room = room_value or _room_from_text(tail) or state.get("room", "")
            pending = state.get("pending_time") or (start, end)
            lesson_start, lesson_end = (start, end) if has_subject_on_row else pending
            for teacher_match in teachers:
                initials = re.sub(r"\s+", "", teacher_match.group(2))
                teacher = f"{teacher_match.group(1)} {initials}"
                subject_text = state.get("subject", "")
                if not current_date or not subject_text:
                    continue
                if subject_text.casefold() in {"поднятие флага", "разговор о важном"}:
                    continue
                lesson_no = sum(1 for row in records if row["date"] == current_date and row["group"] == group_name) + 1
                records.append({"date": current_date, "group": group_name, "specialty": specialty,
                                "lesson_no": lesson_no, "lesson": str(lesson_no), "subject": subject_text,
                                "teacher": teacher, "room": room, "start": lesson_start, "end": lesson_end})
            state["pending_time"] = None
        elif line.casefold() not in {"поднятие флага", "разговор о важном"}:
            line = re.sub(r"^\d+(?:\s+\d+|\s*[-–]\s*\d+)?\s*(?:[/–.-])?\s*", "", line)
            if line and not re.fullmatch(r"\d{1,2}\s*\d{1,2}", line):
                state["subject"] = line
                has_subject_on_row = True
                if time_matches:
                    state["pending_time"] = (start, end)


def _parse_department_pdf(pdf_bytes, branch):
    """Extract searchable schedule tables directly from YAMK's weekly PDFs."""
    import pdfplumber

    groups, records, seen = [], [], set()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as document:
        document_text = "\n".join(page.extract_text() or "" for page in document.pages)
        week_start = _pdf_week_start(document_text)
        year_match = re.search(r"20\d{2}", document_text)
        year = int(year_match.group(0)) if year_match else None
        for page in document.pages:
            table, header_index = _table_and_header(page, branch)
            if table is None:
                continue
            headers = [_cell_text(cell) for cell in table[header_index]]
            columns = []
            if branch == "osep":
                starts = []
                for index, heading in enumerate(headers):
                    if "преподаватель" not in heading.casefold():
                        continue
                    match = _GROUP_RE.search(heading)
                    if match:
                        starts.append((index, _cell_text(match.group(1))))
                for group_pos, (start, group_name) in enumerate(starts):
                    end = starts[group_pos + 1][0] if group_pos + 1 < len(starts) else len(headers)
                    specialty = f"Отделение {DEPARTMENTS[branch][0]}"
                    key = (branch, group_name)
                    if key not in seen:
                        groups.append({"name": group_name, "id": str(len(groups) + 1), "branch": branch,
                                       "specialty": specialty})
                        seen.add(key)
                    columns.append((start, end, group_name, specialty))
            else:
                specialty_row = table[header_index - 1] if header_index else []
                specialty_starts = []
                for index, raw in enumerate(specialty_row):
                    heading = _cell_text(raw)
                    if heading and heading.casefold() not in {"кабинет", "тенибак"} and not _GROUP_RE.search(heading):
                        specialty_starts.append((index, heading))
                for index, name in enumerate(headers):
                    match = _GROUP_RE.match(name)
                    if not match:
                        continue
                    group_name = _cell_text(match.group(1))
                    specialty = next((title for start_col, title in reversed(specialty_starts) if start_col <= index), DEPARTMENTS[branch][1])
                    key = (branch, group_name)
                    if key not in seen:
                        groups.append({"name": group_name, "id": str(len(groups) + 1), "branch": branch,
                                       "specialty": specialty})
                        seen.add(key)
                    room_col = index + 1 if index + 1 < len(headers) else None
                    columns.append((index, room_col, group_name, specialty))

            states = {column[0]: {"subject": "", "room": ""} for column in columns}
            current_date = None
            day_index = -1
            for row in table[header_index + 1:]:
                if not row:
                    continue
                date_cell = row[0] if row else ""
                row_date = _date_from_pdf_cell(date_cell, year)
                if row_date:
                    current_date = row_date
                    day_index = -1
                    states = {column[0]: {"subject": "", "room": ""} for column in columns}
                time_index = 2 if branch == "osep" else 1
                time_text = _cell_text(row[time_index] if len(row) > time_index else "")
                if branch == "osgp" and not row_date and week_start and re.match(r"^0?8[.:]", time_text):
                    day_index += 1
                    if day_index < 6:
                        from datetime import timedelta
                        current_date = (week_start + timedelta(days=day_index)).isoformat()
                        states = {column[0]: {"subject": "", "room": ""} for column in columns}
                if not current_date:
                    continue
                for column in columns:
                    start_col, room_or_end, group_name, specialty = column
                    if branch == "osep":
                        end_col = room_or_end
                        value = "\n".join(
                            line.strip() for cell in row[start_col:end_col]
                            if cell for line in str(cell).replace("\xa0", " ").splitlines() if line.strip()
                        )
                        room_value = next((_cell_text(cell) for cell in reversed(row[start_col:end_col])
                                           if _room_from_text(_cell_text(cell))), "")
                    else:
                        room_col = room_or_end
                        value = row[start_col] if len(row) > start_col else ""
                        room_value = row[room_col] if room_col is not None and len(row) > room_col else ""
                    _parse_group_cell(value, room_value, states[start_col], current_date,
                                      group_name, specialty, time_text, records)
    return groups, records


def _department_schedule(branch):
    global _DEPARTMENT_DATA
    if branch not in DEPARTMENTS or branch == "otp":
        return [], []
    cached = _DEPARTMENT_DATA.get(branch)
    if cached and cached[0] > time.monotonic():
        return list(cached[1]), list(cached[2])
    if _PUBLIC_DOCUMENTS_STALE:
        fallback = _fallback_cache().get("branches", {}).get(branch, {})
        groups, rows = fallback.get("groups", []), fallback.get("rows", [])
        if groups:
            logging.warning("Используется сохранённое расписание ЯМК (%s): список сайта недоступен", branch)
            _DEPARTMENT_DATA[branch] = (time.monotonic() + _CACHE_TTL, groups, rows)
            return list(groups), list(rows)
    try:
        pdf_bytes, _filename = get_department_pdf(branch)
        groups, rows = _parse_department_pdf(pdf_bytes, branch)
        if not groups:
            raise ValueError("Не удалось распознать группы в PDF расписания ЯМК")
    except Exception:
        fallback = _fallback_cache().get("branches", {}).get(branch, {})
        groups, rows = fallback.get("groups", []), fallback.get("rows", [])
        if not groups:
            raise
        logging.warning("Используется сохранённое расписание ЯМК (%s): сайт или PDF недоступен", branch)
    _DEPARTMENT_DATA[branch] = (time.monotonic() + _CACHE_TTL, groups, rows)
    return list(groups), list(rows)


def _lesson_times(cells):
    matches = re.findall(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", " ".join(cells[5:]))
    return (matches[0][0], matches[-1][1]) if matches else ("", "")


def _get(path):
    response = _SESSION.get(urljoin(BASE_URL, path), timeout=25)
    response.raise_for_status()
    response.encoding = "utf-8"
    return BeautifulSoup(response.text, "html.parser")


def _public_document_category(title):
    text = title.casefold()
    if "задолж" in text or "долг" in text:
        return "debts"
    if "сесс" in text:
        return "sessions"
    if "дополнительного образования" in text or "круж" in text or "студи" in text:
        return "extra"
    if "аттестаци" in text or "экзамен" in text or "гиа" in text:
        return "attestation"
    if "расписание занятий" in text:
        return "schedules"
    return "other"


def get_public_documents(refresh=False):
    """Read the college's public timetable and student-document listing."""
    global _PUBLIC_DOCUMENTS, _PUBLIC_DOCUMENTS_UNTIL, _PUBLIC_DOCUMENTS_STALE
    if not refresh and _PUBLIC_DOCUMENTS is not None and time.monotonic() < _PUBLIC_DOCUMENTS_UNTIL:
        return [dict(item) for item in _PUBLIC_DOCUMENTS]

    documents = []
    fetched_live = True
    try:
        for page_number in range(1, 4):
            url = ACTIVITY_URL if page_number == 1 else f"{ACTIVITY_URL}?nav-documents=page-{page_number}"
            response = _SESSION.get(url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            details = soup.select(".list__detail")
            if not details:
                break
            for detail in details:
                title_link = detail.select_one(".list__title a[href]")
                file_link = detail.select_one("a.js-file-download[href]")
                if not title_link or not file_link:
                    continue
                title = _cell_text(title_link.get_text(" ", strip=True))
                date_element = detail.select_one("time[datetime]")
                documents.append({
                    "title": title,
                    "date": date_element.get("datetime", "") if date_element else "",
                    "category": _public_document_category(title),
                    "source_url": urljoin(ACTIVITY_URL, title_link.get("href", "")),
                    "file_url": urljoin(ACTIVITY_URL, file_link.get("href", "")),
                })
    except requests.RequestException:
        logging.warning("Не удалось получить список документов ЯМК; используется сохранённый список", exc_info=True)
        fetched_live = False
        documents = _fallback_cache().get("documents", [])
    if not documents:
        fetched_live = False
        documents = _fallback_cache().get("documents", [])
    _PUBLIC_DOCUMENTS_STALE = not fetched_live
    _PUBLIC_DOCUMENTS = documents
    _PUBLIC_DOCUMENTS_UNTIL = time.monotonic() + _CACHE_TTL
    return [dict(item) for item in documents]


def download_public_document(index):
    documents = get_public_documents()
    if index < 0 or index >= len(documents):
        raise ValueError("Документ уже обновился на сайте. Открой список документов заново.")
    document = documents[index]
    response = _SESSION.get(document["file_url"], headers={
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "Referer": document["source_url"],
    }, timeout=15)
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        raise ValueError("Сайт ЯМК вернул файл не в формате PDF.")
    if len(response.content) > 48 * 1024 * 1024:
        raise ValueError("PDF слишком большой для отправки через Telegram.")
    filename = os.path.basename(unquote(urlparse(document["file_url"]).path)) or "document.pdf"
    return document, response.content, filename


def get_department_pdf(branch):
    if branch not in DEPARTMENTS or branch == "otp":
        raise ValueError("Для этого отделения PDF не используется")
    department_titles = {
        "osgp": "социально-гуманитарного профиля",
        "oenp": "естественнонаучного профиля",
        "osep": "социально-экономического профиля",
    }
    keyword = department_titles[branch]
    for document in get_public_documents():
        title = document["title"].casefold()
        if document["category"] == "schedules" and keyword in title:
            response = _SESSION.get(document["file_url"], headers={
                "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
                "Referer": document["source_url"],
            }, timeout=15)
            response.raise_for_status()
            if not response.content.startswith(b"%PDF"):
                raise ValueError("Сайт ЯМК вернул расписание не в формате PDF")
            return response.content, os.path.basename(unquote(urlparse(document["file_url"]).path))

    # Compatibility fallback for the older timetable site if the official
    # document index is temporarily unavailable or does not list this profile.
    from datetime import date
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Novokuznetsk")).date()
    school_year = today.year if today.month >= 9 else today.year - 1
    first_week = date(school_year, 9, 2)
    current_week = max(1, (today - first_week).days // 7 + 1)
    # Department PDF numbering can drift: one department may publish the
    # current week as week 10 while another calls it week 11. Check nearby
    # indexes and select by dates inside the PDF itself.
    candidates = []
    week_numbers = (current_week, current_week + 1, current_week - 1,
                    current_week + 2, current_week - 2)
    for week_number in week_numbers:
        if week_number < 1:
            continue
        page = _SESSION.get(
            urljoin(BASE_URL, "student/pdf.php"),
            params={"branch": branch, "week": week_number}, timeout=25,
        )
        page.raise_for_status()
        match = re.search(r'PDF_URL\s*=\s*["\']([^"\']+)', page.text)
        if not match:
            raise ValueError("На сайте не найдена ссылка на PDF расписания")
        pdf_url = urljoin(page.url, match.group(1))
        response = _SESSION.get(pdf_url, timeout=45)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        if not response.content.startswith(b"%PDF"):
            continue
        try:
            _groups, rows = _parse_department_pdf(response.content, branch)
            dates = [datetime.strptime(row["date"], "%Y-%m-%d").date() for row in rows if row.get("date")]
        except Exception:
            logging.exception("Не удалось прочитать PDF ЯМК %s", pdf_url)
            continue
        if not dates:
            continue
        if min(dates) <= today <= max(dates):
            return response.content, pdf_url.rsplit("/", 1)[-1]
        distance = min(abs((value - today).days) for value in dates)
        candidates.append((distance, max(dates), response.content, pdf_url.rsplit("/", 1)[-1]))

    if candidates:
        _distance, _last_date, content, filename = min(candidates, key=lambda item: (item[0], -item[1].toordinal()))
        return content, filename

    raise ValueError("На сайте ЯМК пока нет PDF расписания с распознаваемыми занятиями")


def get_groups(branch="otp"):
    """Return group names parsed from the current department timetable."""
    if branch not in DEPARTMENTS:
        return []
    cached = _GROUPS.get(branch)
    if cached and cached[0] > time.monotonic():
        return list(cached[1])
    if branch != "otp":
        groups, _rows = _department_schedule(branch)
        _GROUPS[branch] = (time.monotonic() + _CACHE_TTL, groups)
        return list(groups)
    try:
        response = _SESSION.post(urljoin(BASE_URL, "student/list_group.php"), data={"branch": branch}, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        groups = []
        for button in soup.select("button.group_box.open_modal"):
            name = button.get("value", "").strip()
            group_id = button.get("id", "").strip()
            if name and group_id.isdigit():
                label = " ".join(button.get_text(" ", strip=True).split())
                specialty = label[len(name):].strip() if label.startswith(name) else "Специальность не указана"
                groups.append({"name": name, "id": group_id, "branch": branch,
                               "specialty": specialty or "Специальность не указана"})
    except requests.RequestException:
        logging.warning("Не удалось получить группы ЯМК (%s); используется сохранённый список", branch, exc_info=True)
        groups = _fallback_cache().get("branches", {}).get(branch, {}).get("groups", [])
    if not groups:
        groups = _fallback_cache().get("branches", {}).get(branch, {}).get("groups", [])
    _GROUPS[branch] = (time.monotonic() + _CACHE_TTL, groups)
    return list(groups)


def get_group_schedule(group_name, branch="otp"):
    if branch != "otp":
        groups, rows = _department_schedule(branch)
        if not any(item["name"] == group_name for item in groups):
            return []
        return [row for row in rows if row["group"] == group_name]
    group = next((item for item in get_groups(branch) if item["name"] == group_name), None)
    if not group:
        return []
    try:
        response = _SESSION.post(urljoin(BASE_URL, "student/schedule_group.php"), data={
            "id_group": group["id"], "num_group": group["name"], "l_t_n": "this",
            "depart": "otp", "id_depart": "4", "branch": "ОТП", "next": "last",
        }, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        result = []
        for table in soup.select(".rasp_stud_table table"):
            day_text = " ".join(table.get_text(" ", strip=True).split())
            match = re.search(r"(\d{2}\.\d{2}\.\d{4})", day_text)
            if not match:
                continue
            try:
                day = datetime.strptime(match.group(1), "%d.%m.%Y").date().isoformat()
            except ValueError:
                continue
            for tr in table.select("tr"):
                cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in tr.find_all(["td", "th"], recursive=False)]
                if len(cells) < 5 or not cells[0].strip().isdigit():
                    continue
                start, end = _lesson_times(cells)
                result.append({"date": day, "group": group_name, "lesson_no": cells[0], "lesson": cells[0],
                               "subject": cells[1], "room": cells[2], "teacher": cells[3],
                               "start": start, "end": end})
    except requests.RequestException:
        logging.warning("Не удалось получить расписание ЯМК (%s); используется сохранённое расписание", group_name, exc_info=True)
        result = []
    if not result:
        result = [row for row in _fallback_cache().get("branches", {}).get(branch, {}).get("rows", [])
                  if row.get("group") == group_name]
    return result


def _teacher_index():
    global _TEACHERS, _TEACHERS_UNTIL
    if _TEACHERS is not None and time.monotonic() < _TEACHERS_UNTIL:
        return _TEACHERS
    soup = _get("mobile/teacher/")
    teachers = []
    for link in soup.select('a[href*="/mobile/teacher/shedule/"]'):
        name = link.get_text(" ", strip=True)
        href = link.get("href", "")
        query = parse_qs(urlparse(href).query)
        teacher_id = (query.get("id") or [""])[0]
        if name and teacher_id:
            teachers.append({"name": name, "id": teacher_id, "href": href})
    _TEACHERS = teachers
    _TEACHERS_UNTIL = time.monotonic() + _CACHE_TTL
    return _TEACHERS


def get_teachers():
    return [{"teacher": teacher["name"]} for teacher in _teacher_index()]


def get_teacher_rows(name):
    cached = _TEACHER_ROWS.get(name)
    if cached and cached[0] > time.monotonic():
        return list(cached[1])
    teacher = next((item for item in _teacher_index() if item["name"] == name), None)
    if not teacher:
        return []
    soup = _get(teacher["href"])
    result = []
    for table in soup.select("table"):
        rows = table.select("tr")
        if not rows:
            continue
        date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", rows[0].get_text(" ", strip=True))
        if not date_match:
            continue
        try:
            day = datetime.strptime(date_match.group(1), "%d.%m.%Y").date().isoformat()
        except ValueError:
            continue
        for tr in rows:
            cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in tr.find_all(["td", "th"], recursive=False)]
            if len(cells) < 6 or not cells[0].strip().isdigit():
                continue
            start, end = _lesson_times(cells)
            result.append({"date": day, "lesson_no": cells[0], "lesson": cells[0],
                           "subject": cells[1], "room": cells[2], "teacher": name,
                           "group": cells[4], "start": start, "end": end})
    _TEACHER_ROWS[name] = (time.monotonic() + _CACHE_TTL, result)
    return list(result)


def get_entity_rows(kind, name, branch=None):
    if kind == "teacher":
        return get_teacher_rows(name)
    if kind != "room":
        return []
    cache_key = f"room:{branch or 'all'}"
    cached = _ENTITY_ROWS.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return [row for row in cached[1] if not name or row.get("room") == name]
    rows = []
    for current_branch in ((branch,) if branch in DEPARTMENTS else ("osgp", "oenp", "otp", "osep")):
        try:
            if current_branch == "otp":
                for group in get_groups(current_branch):
                    rows.extend(get_group_schedule(group["name"], current_branch))
            else:
                _groups, branch_rows = _department_schedule(current_branch)
                rows.extend(branch_rows)
        except Exception:
            logging.exception("Не удалось загрузить кабинеты ЯМК (%s)", current_branch)
    _ENTITY_ROWS[cache_key] = (time.monotonic() + _CACHE_TTL, rows)
    return [row for row in rows if not name or row.get("room") == name]
