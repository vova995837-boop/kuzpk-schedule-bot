"""Public schedule reader for Yamalsky Multidisciplinary College (YAMK)."""

import logging
import base64
import io
import os
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://xn----8sbaqd1aje6bf1c2g.xn--p1ai/"
DEPARTMENTS = {
    "osgp": ("ОСГП", "Общественно-социально-гуманитарный профиль"),
    "oenp": ("ОЕНП", "Общественно-естественно-научный профиль"),
    "otp": ("ОТП", "Отделение технического профиля"),
    "osep": ("ОСЭП", "Общественно-социально-экономический профиль"),
}
SOURCE_URL = "https://www.ямк-салехард.рф/"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "KuzbassCollegeScheduleBot/1.0"})
_CACHE_TTL = 300
_TEACHERS = None
_TEACHERS_UNTIL = 0.0
_TEACHER_ROWS = {}
_GROUPS = {}
_DEPARTMENT_DATA = {}
_ENTITY_ROWS = {}
CONVERTAPI_URL = "https://v2.convertapi.com/convert/pdf/to/xlsx"


def _convert_pdf_to_xlsx(pdf_bytes, filename):
    """Convert a public YAMK department PDF through ConvertAPI.

    The API token is read only from the hosting environment and never logged.
    ConvertAPI's default non-stored response keeps the converted workbook out
    of its temporary file storage.
    """
    token = os.getenv("CONVERTAPI_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Для расписаний PDF добавь секрет CONVERTAPI_TOKEN в Railway Variables")
    response = _SESSION.post(
        CONVERTAPI_URL,
        headers={"Authorization": f"Bearer {token}"},
        files={"File": (filename, pdf_bytes, "application/pdf")},
        data={"StoreFile": "false", "OcrMode": "auto", "OcrLanguage": "ru"},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    converted = next((item for item in payload.get("Files", []) if item.get("FileData")), None)
    if converted:
        return base64.b64decode(converted["FileData"])
    # Some account/API configurations return a short-lived download URL.
    converted = next((item for item in payload.get("Files", []) if item.get("Url")), None)
    if converted:
        download = _SESSION.get(converted["Url"], timeout=60)
        download.raise_for_status()
        return download.content
    raise ValueError("Сервис конвертации не вернул Excel-файл")


def _cell_text(value):
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def _is_teacher(value):
    value = _cell_text(value)
    return bool(re.search(r"[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\.?(?:\s*[А-ЯЁ]\.)?$", value))


def _date_value(value):
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", _cell_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(".".join(match.groups()), "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def _parse_department_xlsx(xlsx_bytes, branch):
    """Read group columns from a converted YAMK weekly schedule workbook."""
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    groups, records = [], []
    seen = set()
    for sheet in workbook.worksheets:
        values = list(sheet.iter_rows(values_only=True))
        header_index = next((i for i, row in enumerate(values)
                             if len(row) > 2 and _cell_text(row[0]).casefold() == "дни"
                             and _cell_text(row[1]).casefold() == "время"), None)
        if header_index is None:
            continue
        headers = values[header_index]
        columns = []
        specialty_row = values[header_index - 1] if header_index > 0 else ()
        specialty_starts = []
        for column_index in range(2, len(specialty_row)):
            heading = _cell_text(specialty_row[column_index])
            if (heading and heading.casefold() != "кабинет"
                    and not re.match(r"^\d{2}\s*[А-ЯЁA-Z]", heading, re.IGNORECASE)):
                specialty_starts.append((column_index, heading))
        for index in range(2, len(headers)):
            name = _cell_text(headers[index])
            # The department's table alternates a group column and its room.
            if not name or name.casefold() == "кабинет":
                continue
            # Exclude specialty titles and unrelated headings; group headings
            # begin with the cohort's two-digit admission year.
            if not re.match(r"^\d{2}\s*[А-ЯЁA-Z]", name, re.IGNORECASE):
                continue
            previous_specialties = [title for start_column, title in specialty_starts if start_column <= index]
            specialty = previous_specialties[-1] if previous_specialties else "Специальность не указана"
            key = (branch, specialty, name)
            if key not in seen:
                groups.append({"name": name, "id": str(len(groups) + 1), "branch": branch,
                               "specialty": specialty})
                seen.add(key)
            room_col = index + 1 if index + 1 < len(headers) and _cell_text(headers[index + 1]).casefold() == "кабинет" else None
            columns.append((index, room_col, name, specialty))

        current_date = None
        last_subject = {index: "" for index, _room, _name, _specialty in columns}
        last_room = {index: "" for index, _room, _name, _specialty in columns}
        for row in values[header_index + 1:]:
            if not row:
                continue
            row_date = _date_value(row[0] if len(row) else None)
            if row_date:
                current_date = row_date
                last_subject = {index: "" for index, _room, _name, _specialty in columns}
                last_room = {index: "" for index, _room, _name, _specialty in columns}
            if not current_date:
                continue
            time_text = _cell_text(row[1] if len(row) > 1 else "")
            time_match = re.search(r"(\d{1,2}[.:]\d{2})\s*[-–]\s*(\d{1,2}[.:]\d{2})", time_text)
            for index, room_col, group_name, specialty in columns:
                value = _cell_text(row[index] if len(row) > index else "")
                room_value = _cell_text(row[room_col] if room_col is not None and len(row) > room_col else "")
                if value and not _is_teacher(value):
                    # Subject/course rows are followed by a teacher row.
                    if value.casefold() not in {"поднятие флага", "разговор о важном"}:
                        last_subject[index] = value
                    if room_value:
                        last_room[index] = room_value
                    continue
                if not value or not _is_teacher(value) or not last_subject[index]:
                    continue
                records.append({
                    "date": current_date, "group": group_name,
                    "specialty": specialty,
                    "lesson_no": len([r for r in records if r["date"] == current_date and r["group"] == group_name]) + 1,
                    "lesson": "", "subject": last_subject[index], "teacher": value,
                    "room": room_value or last_room[index],
                    "start": time_match.group(1).replace(".", ":") if time_match else "",
                    "end": time_match.group(2).replace(".", ":") if time_match else "",
                })
    workbook.close()
    return groups, records


def _department_schedule(branch):
    if branch not in DEPARTMENTS or branch == "otp":
        return [], []
    cached = _DEPARTMENT_DATA.get(branch)
    if cached and cached[0] > time.monotonic():
        return list(cached[1]), list(cached[2])
    pdf_bytes, filename = get_department_pdf(branch)
    xlsx_bytes = _convert_pdf_to_xlsx(pdf_bytes, filename)
    groups, rows = _parse_department_xlsx(xlsx_bytes, branch)
    if not groups:
        raise ValueError("В Excel после конвертации не удалось распознать столбцы групп")
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


def get_department_pdf(branch):
    if branch not in DEPARTMENTS or branch == "otp":
        raise ValueError("Для этого отделения PDF не используется")
    page = _SESSION.get(urljoin(BASE_URL, f"student/pdf.php?branch={branch}&?week=1"), timeout=25)
    page.raise_for_status()
    match = re.search(r'PDF_URL\s*=\s*["\']([^"\']+)', page.text)
    if not match:
        raise ValueError("На сайте не найдена ссылка на PDF расписания")
    pdf_url = urljoin(page.url, match.group(1))
    response = _SESSION.get(pdf_url, timeout=45)
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        raise ValueError("Сайт вернул файл не в формате PDF")
    return response.content, f"Расписание_ЯМК_{DEPARTMENTS[branch][0]}.pdf"


def get_groups(branch="otp"):
    """Return groups the official site exposes as individually selectable.

    The other three department pages currently publish only a department-wide
    PDF. Do not infer group names from a PDF or expose fake per-group menus.
    """
    if branch not in DEPARTMENTS:
        return []
    cached = _GROUPS.get(branch)
    if cached and cached[0] > time.monotonic():
        return list(cached[1])
    if branch != "otp":
        groups, _rows = _department_schedule(branch)
        _GROUPS[branch] = (time.monotonic() + _CACHE_TTL, groups)
        return list(groups)
    response = _SESSION.post(urljoin(BASE_URL, "student/list_group.php"), data={"branch": branch}, timeout=25)
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
    response = _SESSION.post(urljoin(BASE_URL, "student/schedule_group.php"), data={
        "id_group": group["id"], "num_group": group["name"], "l_t_n": "this",
        "depart": "otp", "id_depart": "4", "branch": "ОТП", "next": "last",
    }, timeout=25)
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
