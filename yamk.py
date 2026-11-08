"""Public schedule reader for Yamalsky Multidisciplinary College (YAMK)."""

import logging
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
    response = _SESSION.post(urljoin(BASE_URL, "student/list_group.php"), data={"branch": branch}, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    groups = []
    for button in soup.select("button.group_box.open_modal"):
        name = button.get("value", "").strip()
        group_id = button.get("id", "").strip()
        if name and group_id.isdigit():
            groups.append({"name": name, "id": group_id})
    _GROUPS[branch] = (time.monotonic() + _CACHE_TTL, groups)
    return list(groups)


def get_group_schedule(group_name):
    group = next((item for item in get_groups() if item["name"] == group_name), None)
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


def get_entity_rows(kind, name):
    if kind != "teacher":
        return []
    return get_teacher_rows(name)
