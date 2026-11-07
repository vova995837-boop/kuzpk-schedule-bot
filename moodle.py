"""Read each group's publicly visible courses from the KuzPK Moodle catalogue."""
import asyncio
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

MOODLE_URL = "https://xn--d1ai1ae.xn--g1afbts.xn--p1ai"
_CACHE = {}
_CACHE_TTL = 600
_LOOKALIKE_TRANSLATION = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
    "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
})
COURSE_TYPES = (
    ("mdk", "МДК", r"МДК(?=[\s.0-9]|$)"),
    ("soo", "СОО", r"СОО(?=[\s.0-9]|$)"),
    ("sgc", "СГЦ", r"СГЦ(?=[\s.0-9]|$)"),
    ("sg", "СГ", r"СГ(?=[\s.0-9]|$)"),
    ("opc", "ОПЦ", r"ОПЦ(?=[\s.0-9]|$)"),
    ("op", "ОП", r"ОП(?=[\s.0-9]|$)"),
    ("ogs", "ОГСЭ", r"ОГСЭ(?=[\s.0-9]|$)"),
    ("en", "ЕН", r"ЕН(?=[\s.0-9]|$)"),
    ("pm", "ПМ", r"ПМ(?=[\s.0-9]|$)"),
    ("up", "УП", r"УП(?=[\s.0-9]|$)"),
    ("pp", "ПП", r"ПП(?=[\s.0-9]|$)"),
)

# Moodle's programme names paired with the abbreviations used in the timetable.
SPECIALTY_CODES = {
    "СР": "39.02.01",
    "ДО": "44.02.01",
    "ПНК": "44.02.02",
    "ПДО": "44.02.03",
    "СДО": "44.02.04",
    "КПНО": "44.02.05",
    "ПО": "44.02.06",
    "ФК": "49.02.01",
    "Ю": "40.02.04",
}


class MoodleError(Exception):
    pass


class _CourseListParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.courses = []
        self.depth = 0
        self.href = None
        self.title = []
        self.reading_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            classes = attrs.get("class", "").split()
            if self.depth:
                self.depth += 1
            elif "coursebox" in classes:
                self.depth = 1
        if self.depth and tag == "a":
            href = attrs.get("href", "")
            if "/course/view.php" in href:
                self.href, self.title, self.reading_title = href, [], True

    def handle_data(self, data):
        if self.reading_title:
            self.title.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.reading_title:
            parsed = urlparse(self.href)
            course_id = parse_qs(parsed.query).get("id", [""])[0]
            name = " ".join("".join(self.title).split())
            if course_id.isdigit() and name:
                self.courses.append({
                    "id": int(course_id), "name": name,
                    "url": f"{MOODLE_URL}/course/view.php?id={int(course_id)}",
                })
            self.href, self.title, self.reading_title = None, [], False
        if tag == "div" and self.depth:
            self.depth -= 1


class _CategoryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.categories = []
        self.href = None
        self.label = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        href = attrs.get("href", "")
        if tag == "a" and "/course/index.php?categoryid=" in href:
            self.href, self.label = href, []

    def handle_data(self, data):
        if self.href:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            category_id = parse_qs(urlparse(self.href).query).get("categoryid", [""])[0]
            name = " ".join("".join(self.label).split())
            if category_id.isdigit() and name:
                self.categories.append({"id": int(category_id), "name": name})
            self.href, self.label = None, []


def _read_page(url):
    request = Request(url, headers={"User-Agent": "KuzPKScheduleBot/1.0"})
    with urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8", "replace")


def _category_page(category_id):
    return f"{MOODLE_URL}/course/index.php?categoryid={category_id}"


def _parse_categories(html):
    parser = _CategoryParser()
    parser.feed(html)
    return parser.categories


def _normalize(value):
    return "".join(char for char in str(value).casefold() if char.isalnum())


def _matching_group_categories(categories, group_name):
    _prefix, suffix = group_name.split("-", 1)
    full_code, number = _normalize(group_name), _normalize(suffix)
    matches = [item for item in categories if _normalize(item["name"]) == full_code]
    if not matches:
        # Moodle commonly shortens the category to the cohort number (e.g. 2411).
        matches = [item for item in categories if _normalize(item["name"]) == number]
    if not matches and "+" in suffix:
        # Some timetable groups include a parallel subgroup marker absent from Moodle.
        cohort = _normalize(suffix.split("+", 1)[0])
        matches = [item for item in categories if _normalize(item["name"]) == cohort]
    return matches


def _fetch_courses(group_name):
    try:
        prefix, _ = group_name.split("-", 1)
        programme_code = SPECIALTY_CODES.get(prefix.upper())
        if not programme_code:
            return []

        root = _parse_categories(_read_page(f"{MOODLE_URL}/course/index.php"))
        departments = [item for item in root if _normalize(item["name"]) == _normalize("ОЧНОЕ ОТДЕЛЕНИЕ")]
        if not departments:
            return []

        programme_categories = _parse_categories(_read_page(_category_page(departments[0]["id"])))
        programmes = [item for item in programme_categories if programme_code in item["name"]]
        if not programmes:
            return []

        group_categories = []
        for programme in programmes:
            children = _parse_categories(_read_page(_category_page(programme["id"])))
            group_categories.extend(_matching_group_categories(children, group_name))

        courses_by_id = {}
        for category in group_categories:
            parser = _CourseListParser()
            parser.feed(_read_page(_category_page(category["id"])))
            courses_by_id.update({course["id"]: course for course in parser.courses})
        return sorted(courses_by_id.values(), key=lambda course: course["name"].casefold())
    except Exception as exc:
        if isinstance(exc, MoodleError):
            raise
        raise MoodleError("Не удалось загрузить открытый список курсов Moodle.") from exc


async def list_group_courses(group_name):
    now = time.monotonic()
    cached = _CACHE.get(group_name)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    courses = await asyncio.to_thread(_fetch_courses, group_name)
    _CACHE[group_name] = (now, courses)
    return courses


def categorize_courses(courses):
    categorized = {}
    for course in courses:
        normalized = course["name"].upper().translate(_LOOKALIKE_TRANSLATION)
        match = next((item for item in COURSE_TYPES if re.search(item[2], normalized)), None)
        key, label = (match[0], match[1]) if match else ("other", "Другие")
        categorized.setdefault(key, {"label": label, "courses": []})["courses"].append(course)
    order = {item[0]: index for index, item in enumerate(COURSE_TYPES)}
    return dict(sorted(categorized.items(), key=lambda item: (order.get(item[0], len(order)), item[1]["label"])))
