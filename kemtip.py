"""Read the college's public PDF timetable one page at a time."""
import io
import subprocess
import time
import zipfile
from functools import lru_cache
from pathlib import PurePosixPath
from threading import Lock

import pdfplumber
import requests

SCHEDULES = {
    "classes": ("Расписание занятий", "https://cloud.42tip.ru/index.php/s/yJbscbdKBWPCPGN"),
    "exams": ("Расписание экзаменов", "https://cloud.42tip.ru/index.php/s/rS82twwmtdSJknj"),
    "bells": ("Расписание звонков", "https://cloud.42tip.ru/index.php/s/Ysno5JbRGGm945p"),
}
_cache = {}
_lock = Lock()


def documents(kind):
    with _lock:
        now = time.monotonic()
        cached = _cache.get(kind)
        if cached and now - cached[0] < 600:
            return cached[1]
        try:
            response = requests.get(SCHEDULES[kind][1] + "/download", timeout=(5, 25))
            response.raise_for_status()
            data = response.content
            if data.startswith(b"%PDF"):
                docs = [(SCHEDULES[kind][0], data)]
            else:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    docs = [(PurePosixPath(f.filename).stem, archive.read(f))
                            for f in archive.infolist() if f.filename.lower().endswith(".pdf")]
            if not docs:
                raise ValueError("No timetable PDFs")
        except Exception:
            if cached:
                return cached[1]
            raise
        _cache[kind] = now, docs
        return docs


@lru_cache(maxsize=18)
def read_page(data, index):
    with _lock, pdfplumber.open(io.BytesIO(data)) as pdf:
        page = pdf.pages[index]
        content = page.extract_text(layout=True) or ""
        if not content.strip():
            image = page.to_image(resolution=220).original.convert("L")
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            proc = subprocess.run(["tesseract", "stdin", "stdout", "-l", "rus+eng", "--psm", "6"],
                                  input=buf.getvalue(), capture_output=True, check=True, timeout=60)
            content = proc.stdout.decode()
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        return "\n".join(lines), len(pdf.pages)
