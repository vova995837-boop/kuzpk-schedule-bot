"""Read group and lesson cells from the ruled weekly timetable."""
import csv
import io
import math
import re
import subprocess
from datetime import datetime, timedelta
from kemtip import _lock
from functools import lru_cache

import cv2
import numpy as np
import pdfplumber


def runs(values):
    points = np.flatnonzero(values)
    return [int(np.median(part)) for part in np.split(points, np.where(np.diff(points) > 4)[0]+1) if len(part)]


def words(image, mode=6):
    ok, png = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("Unreadable page")
    result = subprocess.run(["tesseract", "stdin", "stdout", "-l", "rus", "--psm", str(mode), "tsv"],
                            input=png.tobytes(), capture_output=True, check=True, timeout=35)
    return [r for r in csv.DictReader(io.StringIO(result.stdout.decode()), delimiter="\t") if r["level"] == "5" and r["text"].strip()]


def grid(page):
    gray = np.array(page.to_image(resolution=180).original.convert("L"))
    h,w = gray.shape
    lines = cv2.HoughLinesP((gray<155).astype("uint8"), 1, np.pi/1800, 100, minLineLength=w//3, maxLineGap=14)
    angles = [math.degrees(math.atan2(y2-y1,x2-x1)) for x1,y1,x2,y2 in lines.reshape(-1,4)] if lines is not None else []
    near = [a for a in angles if abs(a)<4]
    gray = cv2.warpAffine(gray, cv2.getRotationMatrix2D((w/2,h/2), np.median(near) if near else 0,1),(w,h),borderValue=255)
    ink=(gray<180).astype("uint8")
    xs=runs(ink.sum(axis=0)>h*.53)
    candidates=runs(ink.sum(axis=0)>h*.34)
    cols=[]
    for i,(a,b) in enumerate(zip(candidates,candidates[1:])):
        if b-a>w*.12:
            ends=[v for v in candidates[i+2:] if w*.03 < v-b < w*.075]
            if ends:
                cols.append((a,b,ends[0]))
    if not cols or len(xs)<4: raise ValueError("Unrecognized timetable columns")
    a,b=xs[:2]
    ys=runs(ink[:,a+4:b-4].mean(axis=1)>.77)
    return gray, ink, cols, ys


@lru_cache(maxsize=2)
def catalogue(data):
    groups={}
    with _lock, pdfplumber.open(io.BytesIO(data)) as pdf:
        for index,page in enumerate(pdf.pages):
            gray,_,cols,ys=grid(page)
            # The six day blocks are taller than the title and group headings.
            days=[(a,b) for a,b in zip(ys,ys[1:]) if b-a>gray.shape[0]*.065]
            if len(days)!=6: continue
            top=days[0][0]
            for c,(x,right,_) in enumerate(cols):
                header=gray[max(0,top-int(gray.shape[0]*.04)):top-2,x+4:right-3]
                text=" ".join(r["text"] for r in words(header))
                code=re.findall(r"(?<!\d)\d{4}(?!\d)",text)
                if len(code)==1: groups[code[0]]=(index,c,days)
    return groups


@lru_cache(maxsize=1)
def _page_grid(data, index):
    with _lock, pdfplumber.open(io.BytesIO(data)) as pdf:
        return grid(pdf.pages[index])


@lru_cache(maxsize=32)
def lesson_cells(data, group, weekday):
    index,c,days=catalogue(data)[group]
    gray,ink,cols,_=_page_grid(data, index)
    x,right,end=cols[c]
    top,bottom=days[weekday]
    crop=gray[top:bottom+1,x:end+1].copy()
    binary=(crop<180).astype("uint8")
    for size in [(max(25,(right-x)//3),1),(1,max(25,(bottom-top)//3))]:
        mask=cv2.morphologyEx(binary,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,size))
        crop[cv2.dilate(mask,np.ones((3,3),np.uint8))>0]=255
    tokens=words(crop)
    cuts=[0]+[v for v in runs(ink[top:bottom,x+8:right-8].mean(axis=1)>.75) if 8<v<bottom-top-8]+[bottom-top]
    number_edge = cols[0][0]
    slot_width = max(10, int(gray.shape[1] * .015))
    slot_edges = runs(ink[top:bottom, number_edge-slot_width:number_edge-5].mean(axis=1)>.8)
    result=[]
    for a,b in zip(cuts,cuts[1:]):
        entries=[r for r in tokens if a<=int(r["top"])+int(r["height"])/2<b]
        lines={}
        rooms=[]
        for r in entries:
            if int(r["left"])+int(r["width"])/2>=right-x:
                rooms.append(r["text"])
            else:
                lines.setdefault((r["block_num"],r["par_num"],r["line_num"]),[]).append(r["text"])
        labels=[" ".join(v).strip(" |") for v in lines.values()]
        if not labels: continue
        prior = sum(6 < edge <= a+6 for edge in slot_edges)
        # Monday includes two zero-period rows before lesson one.
        number = max(0, (prior + (0 if weekday == 0 else 2)) // 2)
        result.append((str(number), " · ".join(filter(None,labels)), ", ".join(dict.fromkeys(rooms))))
    return result

@lru_cache(maxsize=2)
def week_start(data):
    with _lock, pdfplumber.open(io.BytesIO(data)) as pdf:
        gray, _, _, _ = grid(pdf.pages[0])
    header = " ".join(r["text"] for r in words(gray[:int(gray.shape[0]*.15)]))
    period = re.search(r"(\d{2}\.\d{2}\.\d{4})\s*[-–—]\s*(\d{2}\.\d{2}\.\d{4})", header)
    if period:
        start, end = (datetime.strptime(d, "%d.%m.%Y").date() for d in period.groups())
        if start.weekday() == 0 and end == start + timedelta(days=5):
            return start


def lessons(data, group, weekday):
    return [(label, room) for _, label, room in lesson_cells(data, group, weekday)]


def timetable(data):
    monday = week_start(data)
    if not monday:
        raise ValueError("Missing timetable week")
    rows = []
    for group in catalogue(data):
        for weekday in range(6):
            for number, label, room in lesson_cells(data, group, weekday):
                subject, sep, teacher = label.rpartition(" · ")
                if not sep or not re.search(r"[А-ЯЁ]\.\s*[А-ЯЁ]\.", teacher):
                    subject, teacher = label, ""
                rows.append(dict(group=group, date=(monday+timedelta(days=weekday)).isoformat(),
                                 lesson_no=number, subject=subject, teacher=teacher, room=room))
    if not rows:
        raise ValueError("Empty timetable")
    return rows


def add_times(rows, data, bell_docs):
    """Attach only explicit times or times from a recognized theory shift."""
    from kemtip import read_page
    slots = {}
    for title, document in bell_docs:
        if "теори" not in title.lower():
            continue
        text, _ = read_page(document, 0)
        shift = None
        for line in text.splitlines():
            heading = re.search(r"([12])\s*смена", line.lower())
            if heading:
                shift = heading[1]
            match = re.search(r"(\d+)\s*,\s*\d+\s*урок\s+(\d{1,2})[.:](\d{2})\s*[-–]\s*(\d{1,2})[.:](\d{2})", line)
            if shift and match:
                slot, h, m, eh, em = match.groups()
                slots[shift, str((int(slot)+1)//2)] = (f"{int(h):02d}:{m}", f"{int(eh):02d}:{em}")
    groups = catalogue(data)
    shifts = {}
    for group, (page, _, _) in groups.items():
        if page not in shifts:
            gray, _, _, _ = _page_grid(data, page)
            text = " ".join(r["text"] for r in words(gray[:int(gray.shape[0]*.15)]))
            found = re.search(r"\b([12])\s*смена", text, re.I)
            shifts[page] = found[1] if found else ""
    for row in rows:
        explicit = re.search(r"\b(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})\b", row["subject"])
        period = explicit.groups() if explicit else slots.get((shifts[groups[row["group"]][0]],row["lesson_no"]))
        if period and (explicit or "практик" not in row["subject"].lower()):
            row["start"], row["end"] = period
