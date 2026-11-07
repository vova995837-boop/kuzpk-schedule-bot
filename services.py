from html import escape
from datetime import timedelta
from db import users_for_group, all_users

def format_schedule(group, day, rows, entity_type="group"):
    weekdays = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
    labels = {"group": ("🎓", "Группа"), "teacher": ("👨‍🏫", "Преподаватель"), "room": ("🚪", "Кабинет")}
    icon, label = labels.get(entity_type, labels["group"])
    title = (f"📅 <b>{weekdays[day.weekday()].capitalize()}, {day.strftime('%d.%m.%Y')}</b>\n"
             f"{icon} <b>{label}: {escape(str(group))}</b>\n")
    if not rows:
        return title + "\n<i>Занятий на эту дату нет или расписание ещё не опубликовано.</i>"
    rows.sort(key=lambda x: (int(x.get("lesson_no") or 0) if str(x.get("lesson_no") or "").isdigit() else 999,
                            int(x.get("lesson") or 0) if str(x.get("lesson") or "").isdigit() else 999))
    out = [f"<i>Занятий: {len(rows)}</i>"]
    for r in rows:
        subject = escape(str(r.get('subject') or 'Занятие'))
        teacher = escape(str(r.get('teacher') or 'Не указан'))
        room = escape(str(r.get('room') or 'Аудитория не указана'))
        lesson = escape(str(r.get('lesson_no') or '?'))
        sublesson = f" · урок {escape(str(r.get('lesson')))}" if r.get('lesson') else ""
        time = f"\n🕒 {escape(str(r.get('start')))}–{escape(str(r.get('end')))}" if r.get('start') and r.get('end') else ""
        group_line = f"\n🎓 {escape(str(r.get('group')))}" if entity_type != "group" and r.get("group") else ""
        teacher_line = f"\n👨‍🏫 {teacher}"
        room_line = f"\n🚪 {room}"
        out.append(f"<b>🔹 {lesson} пара{sublesson}</b>\n<blockquote>📚 {subject}{group_line}{teacher_line}{room_line}{time}</blockquote>")
    return title + "\n" + "\n".join(out)

def format_week_schedule(group, monday, schedule_by_day):
    weekdays = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
    days = [monday + timedelta(days=offset) for offset in range(7)]

    def compose(subject_limit=55, include_details=True):
        out = [
            "🗓 <b>Расписание на неделю</b>",
            f"🎓 <b>Группа: {escape(str(group))}</b>",
            f"<i>{monday:%d.%m}–{days[-1]:%d.%m.%Y}</i>",
        ]
        for day in days:
            out.append(f"\n<b>{weekdays[day.weekday()]}, {day:%d.%m}</b>")
            rows = sorted(schedule_by_day.get(day, []), key=lambda row: (
                int(row.get("lesson_no") or 0) if str(row.get("lesson_no") or "").isdigit() else 999,
                int(row.get("lesson") or 0) if str(row.get("lesson") or "").isdigit() else 999,
            ))
            if not rows:
                out.append("<i>Занятий нет</i>")
                continue
            for row in rows:
                subject_raw = str(row.get("subject") or "Занятие").strip()
                if len(subject_raw) > subject_limit:
                    subject_raw = subject_raw[:subject_limit - 1].rstrip() + "…"
                pair = escape(str(row.get("lesson_no") or "?"))
                start, end = str(row.get("start") or "").strip(), str(row.get("end") or "").strip()
                time_label = f"{escape(start)}–{escape(end)}" if start and end else escape(start or end)
                time_part = f" · {time_label}" if time_label else ""
                out.append(f"• <b>{pair} пара</b>{time_part} — {escape(subject_raw)}")
                if include_details:
                    teacher = str(row.get("teacher") or "").strip()
                    room = str(row.get("room") or "").strip()
                    details = [value for value in (teacher, room) if value and value not in ("Не указан", "Аудитория не указана")]
                    if details:
                        out.append("  <i>" + " · ".join(escape(value[:45]) for value in details) + "</i>")
        return "\n".join(out)

    for width, include_details in ((55, True), (55, False), (35, False), (16, False), (8, False), (4, False)):
        text = compose(subject_limit=width, include_details=include_details)
        if len(text) <= 3900:
            return text
    return "Расписание слишком объёмное для одного сообщения."

def format_bells(rows):
    if not rows:
        return "🔔 <b>Расписание звонков</b>\n\n<i>Данные пока не загружены.</i>"
    if "text" in rows[0]:
        # Compatibility with rows from an older workbook loader.
        return "🔔 <b>Расписание звонков</b>\n\n" + "\n".join(escape(r["text"]) for r in rows)
    if "section" not in rows[0]:
        cards = []
        for r in rows:
            cards.append(f"<b>{escape(str(r.get('day_type', '')))} · {escape(str(r.get('lesson', '')))}</b>\n<blockquote>🕒 {escape(str(r.get('start', '')))}–{escape(str(r.get('end', '')))}</blockquote>")
        return "🔔 <b>Расписание звонков</b>\n\n" + "\n\n".join(cards)
    out = ["🔔 <b>РАСПИСАНИЕ ЗВОНКОВ</b>"]
    section = None
    for row in rows:
        if row.get("section") != section:
            section = row.get("section")
            out.append(f"\n<b>🗓 {escape(str(section))}</b>")
        details = [f"🕘 {escape(str(row.get('first', '—')))}"]
        if row.get("second") and row["second"] != "—":
            details.append(f"🕙 {escape(str(row['second']))}")
        breaks = [x for x in (row.get("break_1"), row.get("break_2")) if x and x != "—"]
        if breaks: details.append("☕ Перемены: " + " / ".join(escape(x) for x in breaks))
        if row.get("note"): details.append("ℹ️ " + escape(str(row["note"])))
        out.append(f"<b>{escape(str(row.get('lesson', 'Пара')))} · {escape(str(row.get('shift', '')))}</b>\n<blockquote>" + "\n".join(details) + "</blockquote>")
    note = rows[-1].get("general_note")
    if note: out.append("\n<i>ℹ️ " + escape(str(note)) + "</i>")
    return "\n".join(out)

async def send_group_announcement(bot, group, text):
    ids = await users_for_group(group)
    sent = 0
    for uid in ids:
        try:
            await bot.send_message(uid, f"📢 <b>Объявление группы {escape(str(group))}</b>\n\n{escape(str(text))}")
            sent += 1
        except Exception: pass
    return sent

async def send_all_announcement(bot, text):
    ids = await all_users()
    sent = 0
    for uid in ids:
        try:
            await bot.send_message(uid, f"📢 <b>Объявление колледжа</b>\n\n{escape(str(text))}")
            sent += 1
        except Exception: pass
    return sent
