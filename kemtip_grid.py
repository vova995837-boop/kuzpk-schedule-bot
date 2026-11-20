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


@lru_cache(maxsize=32)
def lessons(data, group, weekday):
    index,c,days=catalogue(data)[group]
    with _lock, pdfplumber.open(io.BytesIO(data)) as pdf:
        gray,ink,cols,_=grid(pdf.pages[index])
    x,right,end=cols[c]
    top,bottom=days[weekday]
    crop=gray[top:bottom+1,x:end+1].copy()
    binary=(crop<180).astype("uint8")
    for size in [(max(25,(right-x)//3),1),(1,max(25,(bottom-top)//3))]:
        mask=cv2.morphologyEx(binary,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,size))
        crop[cv2.dilate(mask,np.ones((3,3),np.uint8))>0]=255
    tokens=words(crop)
    cuts=[0]+[v for v in runs(ink[top:bottom,x+8:right-8].mean(axis=1)>.75) if 8<v<bottom-top-8]+[bottom-top]
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
        result.append((" · ".join(filter(None,labels)), ", ".join(dict.fromkeys(rooms))))
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
