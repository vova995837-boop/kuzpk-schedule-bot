"""KuzPK timetable scraper and change detector for raspis.kuzpcdesk.ru."""
import asyncio
import logging
import re
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "http://raspis.kuzpcdesk.ru/"
GROUP_INDEX_URL = urljoin(BASE_URL, "cg.htm")
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")


def _get(url):
    response = requests.get(url, timeout=(8, 25), headers={"User-Agent": "StepashaScheduleBot/1.0"})
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def _group_links(html):
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    for anchor in soup.find_all("a", href=True):
        name = " ".join(anchor.get_text(" ", strip=True).split())
        href = anchor["href"].strip()
        if not name or not re.match(r"^[A-Za-zА-Яа-яЁё0-9]+[-–][A-Za-zА-Яа-яЁё0-9+]+$", name):
            continue
        if not re.fullmatch(r"cg\d+\.htm", urlparse(href).path.split("/")[-1], flags=re.I):
            continue
        result[name.replace("–", "-")] = urljoin(BASE_URL, href)
    return result


def fetch_kuzpk_group_names():
    names = sorted(_group_links(_get(GROUP_INDEX_URL)))
    if not names:
        raise RuntimeError("На странице КузПК не найден список учебных групп.")
    return names


def _parse_group_page_data(html, group, today):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.inf")
    if table is None:
        return [], []
    rows = []
    dates = set()
    current_date = None
    for tr in table.find_all("tr", recursive=False):
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        first_text = " ".join(cells[0].get_text(" ", strip=True).split())
        match = DATE_RE.search(first_text)
        if match:
            try:
                current_date = date.fromisoformat(f"{match.group(1)[6:10]}-{match.group(1)[3:5]}-{match.group(1)[:2]}")
            except ValueError:
                current_date = None
            if current_date and today - timedelta(days=1) <= current_date <= today + timedelta(days=14):
                dates.add(current_date.isoformat())
            continue
        if current_date is None or not today - timedelta(days=1) <= current_date <= today + timedelta(days=14):
            continue
        if len(cells) < 2:
            continue
        slot = " ".join(cells[0].get_text(" ", strip=True).split())
        if not slot.isdigit() or int(slot) < 1 or int(slot) > 7:
            continue
        content = cells[1]
        anchors = content.find_all("a", href=True)
        subject = next((" ".join(a.get_text(" ", strip=True).split()) for a in anchors
                        if re.match(r"j\d+\.htm$", a["href"], re.I)), "")
        room = next((" ".join(a.get_text(" ", strip=True).split()) for a in anchors
                     if re.match(r"ca\d+\.htm$", a["href"], re.I)), "")
        teacher = next((" ".join(a.get_text(" ", strip=True).split()) for a in anchors
                        if re.match(r"cp\d+\.htm$", a["href"], re.I)), "")
        if not subject:
            continue
        rows.append({"date": current_date.isoformat(), "group": group, "lesson_no": int(slot),
                     "lesson": "", "subject": subject, "teacher": teacher, "room": room})
    return rows, sorted(dates)


def _parse_group_page(html, group, today):
    return _parse_group_page_data(html, group, today)[0]


def fetch_kuzpk_schedule(today=None):
    today = today or date.today()
    index_html = _get(GROUP_INDEX_URL)
    groups = _group_links(index_html)
    if not groups:
        raise RuntimeError("На странице КузПК не удалось найти список групп.")

    async def fetch_all():
        semaphore = asyncio.Semaphore(8)
        async def one(group, url):
            async with semaphore:
                try:
                    html = await asyncio.to_thread(_get, url)
                    rows, days = _parse_group_page_data(html, group, today)
                    return group, rows, days
                except Exception as exc:
                    logging.warning("Не удалось получить расписание %s: %s", group, exc)
                    return None
        return await asyncio.gather(*(one(group, url) for group, url in groups.items()))

    pages = asyncio.run(fetch_all())
    successful = [result for result in pages if result is not None]
    if len(successful) < max(1, int(len(groups) * 0.8)):
        raise RuntimeError(f"Успешно загружено только {len(successful)} из {len(groups)} групп; база не обновлена.")
    if not successful:
        raise RuntimeError("Не удалось получить страницы групп КузПК.")
    rows = [row for _group, page_rows, _dates in successful for row in page_rows]
    if not rows:
        raise RuntimeError("Сайт не вернул занятий на ближайшие 15 дней; сохранённая версия не затрагивается.")
    buckets = {}
    for group, group_rows, days in successful:
        by_day = {day: [] for day in days}
        for row in group_rows:
            by_day.setdefault(row["date"], []).append(row)
        for day, day_rows in by_day.items():
            buckets[(group, day)] = day_rows
    logging.info("KuzPK source: parsed %s groups, %s lessons", len(successful), len(rows))
    return rows, buckets
