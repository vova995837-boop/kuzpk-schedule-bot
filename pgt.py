"""Reader for the public weekly schedule of PGT named after V. P. Romanov."""

import asyncio
import logging
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "http://raspisanie.pgt.su/"
_INDEX_CACHE = {}
_INDEX_CACHE_TIME = 0.0
_PAGE_CACHE = {}
_CACHE_TTL = 300
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "KuzbassCollegeScheduleBot/1.0"})


def _page(path):
    response = _SESSION.get(urljoin(BASE_URL, path), timeout=20)
    response.raise_for_status()
    response.encoding = "cp1251"
    return BeautifulSoup(response.text, "html.parser")


def _list_entities(path, link_class="z0"):
    soup = _page(path)
    return [(a.get_text(" ", strip=True), a.get("href"))
            for a in soup.select(f"a.{link_class}") if a.get("href")]


def _index(kind):
    global _INDEX_CACHE, _INDEX_CACHE_TIME
    if _INDEX_CACHE and time.monotonic() - _INDEX_CACHE_TIME < _CACHE_TTL:
        return _INDEX_CACHE.get(kind, [])
    paths = {"group": "cg.htm", "teacher": "cp.htm", "room": "ca.htm"}
    _PAGE_CACHE.clear()
    _INDEX_CACHE = {name: _list_entities(path) for name, path in paths.items()}
    _INDEX_CACHE_TIME = time.monotonic()
    return _INDEX_CACHE.get(kind, [])


def _periods(table):
    """Read day blocks where the first cell spans six lessons."""
    rows = table.find_all("tr")
    result = []
    active_date = None
    for tr in rows:
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue
        first = cells[0]
        if first.get("rowspan"):
            match = re.search(r"(\d{2}\.\d{2}\.\d{4})", first.get_text(" ", strip=True))
            if match:
                try:
                    active_date = datetime.strptime(match.group(1), "%d.%m.%Y").date().isoformat()
                except ValueError:
                    active_date = None
        if active_date is None or len(cells) < 2:
            continue
        lesson_text = cells[1].get_text(" ", strip=True)
        number = re.match(r"\s*(\d+)", lesson_text)
        if not number:
            continue
        details = cells[-1].get_text(" ", strip=True)
        if not details:
            continue
        time_match = re.search(r"(\d{1,2}[.:]\d{2})\s*[-–]\s*(\d{1,2}[.:]\d{2})", lesson_text)
        result.append({"date": active_date, "lesson_no": int(number.group(1)),
                       "lesson": number.group(1), "details": details,
                       "start": time_match.group(1).replace('.', ':') if time_match else "",
                       "end": time_match.group(2).replace('.', ':') if time_match else ""})
    return result


def _parse_detail_page(path, kind, name):
    soup = _page(path)
    table = next((t for t in soup.find_all("table") if len(t.find_all("tr")) > 3), None)
    if not table:
        return []
    result = []
    for item in _periods(table):
        details = item.pop("details")
        row = {**item, "group": "", "teacher": "", "room": "", "subject": ""}
        if kind == "group":
            row["group"] = name
            # Subject, room and teacher are conventionally separated by spaces.
            # Use the final room-like token and the trailing surname/initials as
            # boundaries while preserving the full subject as a safe fallback.
            match = re.match(r"^(.*?)(?:\s+)(\d{3,4}[А-ЯA-Z]?(?:[-/]\d+)?)(?:\s+)(.+)$", details)
            if match:
                row["subject"], row["room"], row["teacher"] = (part.strip() for part in match.groups())
            else:
                row["subject"] = details
        else:
            # Teacher/room pages usually list teacher, group, then subject;
            # preserve the raw line as a useful subject when it is ambiguous.
            parts = details.split()
            if kind == "teacher":
                row["teacher"] = name
                group = next((p for p in parts if re.search(r"-\d{2}", p)), "")
                row["group"] = group
                row["subject"] = details
                room = next((p for p in parts if re.fullmatch(r"\d{3,4}[А-ЯA-Z]?(?:[-/]\d+)?", p)), "")
                row["room"] = room
            else:
                row["room"] = name
                row["subject"] = details
                row["teacher"] = " ".join(parts[:2]) if len(parts) > 1 else ""
                row["group"] = next((p for p in parts if re.search(r"-\d{2}", p)), "")
        result.append(row)
    return result


def _cached_page(kind, name):
    for entity_name, path in _index(kind):
        if entity_name == name:
            key = (kind, path)
            if key not in _PAGE_CACHE:
                _PAGE_CACHE[key] = _parse_detail_page(path, kind, name)
            return _PAGE_CACHE[key]
    return []


async def get_rows():
    # Compatibility helper for callers that need directory-wide teacher/room
    # views. Schedule queries use get_group_rows() and fetch only one page.
    return await asyncio.to_thread(lambda: [row for name, _ in _index("group")
                                             for row in _cached_page("group", name)])


async def get_groups():
    return sorted(await asyncio.to_thread(lambda: [name for name, _ in _index("group")]))


async def get_entity_names(kind):
    return sorted(await asyncio.to_thread(lambda: [name for name, _ in _index(kind)]))


async def get_group_rows(name):
    return await asyncio.to_thread(_cached_page, "group", name)


async def get_entity_rows(kind, name):
    return await asyncio.to_thread(_cached_page, kind, name)
