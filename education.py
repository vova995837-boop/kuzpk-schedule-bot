"""Public education documents published on the KuzPK website."""
import asyncio
import re
import time
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from moodle import SPECIALTY_CODES

EDUCATION_URL = (
    "https://kuzpc.kemobl.ru/Svedeniya-ob-obrazovatelnoj-organizacii-"
    "080yf3q61je9pul3gz0pwt/Obrazovanie-who7ji5nkc94ln7toh72wf/"
)
PLANS_URL = urljoin(EDUCATION_URL, "uchebnye-plany/")
CALENDAR_SECTION = "Календарные графики учебного процесса 2026-2027г. I семестр"
GIA_SECTION = "Программы ГИА"
PLANS_SECTION = "2026-2027г."
_CACHE = {}
_CACHE_TTL = 1800


class EducationError(Exception):
    pass


class _FilesParser(HTMLParser):
    """Parse the site's spoiler sections and their downloadable files."""
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.sections = {}
        self.section = ""
        self.title_parts = None
        self.file = None
        self.read_file_name = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "span" and "spoiler-title" in classes:
            self.title_parts = []
        elif tag == "li" and "file-item-sp" in classes:
            self.file = {"name_parts": [], "url": ""}
        elif tag == "a" and self.file is not None and "file-action-sp" in classes:
            href = attrs.get("href", "")
            if href:
                self.file["url"] = _safe_url(urljoin(self.page_url, href))
        elif tag == "span" and self.file is not None and "file-name" in classes:
            self.read_file_name = True

    def handle_data(self, data):
        if self.title_parts is not None:
            self.title_parts.append(data)
        if self.file is not None and self.read_file_name:
            self.file["name_parts"].append(data)

    def handle_endtag(self, tag):
        if tag == "span" and self.title_parts is not None:
            self.section = " ".join("".join(self.title_parts).split())
            self.sections.setdefault(self.section, [])
            self.title_parts = None
        elif tag == "span" and self.read_file_name:
            self.read_file_name = False
        elif tag == "li" and self.file is not None:
            name = " ".join("".join(self.file["name_parts"]).split())
            url = self.file["url"]
            if name and url.startswith("https://"):
                self.sections.setdefault(self.section, []).append({"name": name, "url": url})
            self.file = None


def _read_page(url):
    request = Request(url, headers={"User-Agent": "KuzPKScheduleBot/1.0"})
    with urlopen(request, timeout=18) as response:
        return response.read().decode("utf-8", "replace")


def _safe_url(url):
    parts = urlsplit(url)
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _parse_page(url):
    parser = _FilesParser(url)
    parser.feed(_read_page(url))
    return parser.sections


def _specialty_code(group_name):
    prefix = str(group_name or "").split("-", 1)[0].upper().strip()
    # The timetable abbreviates this programme as Ю; the official site uses ЮР.
    if prefix in {"Ю", "ЮР"}:
        prefix = "Ю"
    return SPECIALTY_CODES.get(prefix)


def _fetch_documents(group_name, kind):
    code = _specialty_code(group_name)
    if not code:
        return []
    try:
        if kind == "plans":
            sections = _parse_page(PLANS_URL)
            documents = sections.get(PLANS_SECTION, [])
        else:
            sections = _parse_page(EDUCATION_URL)
            section = CALENDAR_SECTION if kind == "calendar" else GIA_SECTION
            documents = sections.get(section, [])
        pattern = re.compile(rf"(?<!\d){re.escape(code)}(?!\d)")
        return [doc for doc in documents if pattern.search(doc["name"])]
    except Exception as exc:
        raise EducationError("Не удалось загрузить документы с сайта колледжа. Попробуй ещё раз позже.") from exc


async def list_documents(group_name, kind):
    key = (str(group_name), kind)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    documents = await asyncio.to_thread(_fetch_documents, group_name, kind)
    _CACHE[key] = (now, documents)
    return documents


def _download_document(url):
    """Return document bytes and a safe filename for Telegram upload."""
    request = Request(url, headers={"User-Agent": "KuzPKScheduleBot/1.0"})
    with urlopen(request, timeout=30) as response:
        content_type = response.headers.get_content_type()
        data = response.read(49 * 1024 * 1024 + 1)
        disposition = response.headers.get("Content-Disposition", "")
        final_url = response.geturl()
    if len(data) > 49 * 1024 * 1024:
        raise EducationError("Файл превышает ограничение Telegram для отправки ботом.")
    if content_type != "application/pdf" and not final_url.lower().split("?", 1)[0].endswith(".pdf"):
        raise EducationError("Сайт колледжа вернул файл, который не удалось распознать как PDF.")
    filename = "document.pdf"
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", disposition, re.I)
    if match:
        from urllib.parse import unquote
        filename = unquote(match.group(1) or match.group(2)).strip()
    else:
        from urllib.parse import unquote, urlparse
        filename = unquote(urlparse(final_url).path.rsplit("/", 1)[-1]) or filename
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return data, filename[:180]


async def download_document(url):
    try:
        return await asyncio.to_thread(_download_document, url)
    except EducationError:
        raise
    except Exception as exc:
        raise EducationError("Не удалось скачать документ с сайта колледжа.") from exc
