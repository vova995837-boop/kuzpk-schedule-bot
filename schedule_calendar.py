"""Calendar attachments for group timetables."""
from datetime import datetime, timezone
from hashlib import sha256


def _text(value):
    return str(value or "").replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _fold(line):
    parts = []
    part = bytearray()
    for char in line:
        encoded = char.encode()
        if len(part) + len(encoded) > 75:
            parts.append(bytes(part))
            part = bytearray(b" ")
        part.extend(encoded)
    return b"\r\n".join([*parts, bytes(part)])


def group_calendar(days, group):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    group_key = sha256(group.encode()).hexdigest()[:16]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Stepasha//Timetable//RU", "CALSCALE:GREGORIAN"]
    for day, rows in sorted(days.items()):
        date_key = day.strftime("%Y%m%d")
        for index, row in enumerate(rows):
            lines.extend(["BEGIN:VEVENT", f"UID:{date_key}-{group_key}-{index}@stepasha", f"DTSTAMP:{stamp}"])
            times = []
            for field in ("start", "end"):
                try:
                    times.append(datetime.strptime(str(row.get(field)), "%H:%M").strftime("%H%M%S"))
                except ValueError:
                    times.append(None)
            start, end = times
            lines.append(f"DTSTART:{date_key}T{start}" if start else f"DTSTART;VALUE=DATE:{date_key}")
            if start and end and end > start:
                lines.append(f"DTEND:{date_key}T{end}")
            details = " · ".join(filter(None, [row.get("teacher"), group]))
            lines.extend(["SUMMARY:" + _text(row.get("subject") or row.get("lesson") or "Занятие"),
                          "LOCATION:" + _text(row.get("room")), "DESCRIPTION:" + _text(details), "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return b"\r\n".join(map(_fold, lines)) + b"\r\n"
