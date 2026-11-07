import asyncio, logging, os
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, BufferedInputFile
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from db import init_db, add_user, get_user, set_group, set_role, list_groups, users_for_group, all_users, save_announcement, recent_announcements
from sheets import get_schedule, get_week_schedule, get_teachers, get_rooms, get_bells, get_entity_names, get_entity_schedule, room_building
from services import format_schedule, format_week_schedule, format_bells, send_group_announcement, send_all_announcement
from moodle import list_group_courses, categorize_courses, MoodleError, SPECIALTY_CODES
from education import list_documents, download_document, EducationError, EDUCATION_URL, PLANS_URL

load_dotenv()
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Novokuznetsk"))

if not TOKEN or TOKEN in ("ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER", "PASTE_TOKEN_HERE", "YOUR_BOT_TOKEN"):
    raise RuntimeError("Укажи действующий BOT_TOKEN в файле .env (не отправляй его в чат).")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

SPORTS_DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота")
SPORTS_SECTIONS = [
    {"name": "Волейбол", "days": {0: "16:00–17:30", 1: "16:00–17:30", 4: "16:00–17:30"}, "coach": "Толстов С.Н."},
    {"name": "Баскетбол", "days": {0: "18:00–19:30", 2: "18:00–19:30", 3: "18:00–19:30"}, "coach": "Баранов Е.В."},
    {"name": "Футбол", "days": {1: "18:00–19:30", 2: "16:00–17:30", 3: "16:00–17:30"}, "coach": "Булгаков А. С."},
    {"name": "Настольный теннис", "days": {4: "16:00–17:30"}, "coach": "Булгаков А. С."},
    {"name": "Атлетическая гимнастика", "days": {0: "14:00–16:30", 1: "14:00–16:30", 2: "14:00–16:30", 3: "14:00–16:30", 4: "14:00–16:30"}, "coach": "Петраев Д.Л."},
    {"name": "Лёгкая атлетика", "days": {2: "16:00–17:30", 3: "16:00–17:30"}, "coach": "Толстов С.Н."},
    {"name": "Лыжи", "days": {1: "16:00–17:30", 2: "16:00–17:30", 4: "16:00–17:30"}, "coach": "Королёв В.И."},
    {"name": "ГТО", "days": {1: "14:00–15:30", 3: "14:00–15:30"}, "coach": "Паньженская О.А."},
    {"name": "Шахматы", "days": {2: "14:00–16:00"}, "coach": "Толстов С.Н."},
]

def is_courses_group(group_name):
    normalized = str(group_name or "").upper().replace("–", "-").replace("—", "-").replace(" ", "")
    return "-" in normalized and len(normalized) >= 5

def main_menu(role="student", group_name=None):
    rows = [
        [InlineKeyboardButton(text="📅 Расписание", callback_data="menu|schedule")],
        [InlineKeyboardButton(text="📢 Объявления", callback_data="menu|announcements"), InlineKeyboardButton(text="⚙️ Моя группа", callback_data="menu|group")]
    ]
    if is_courses_group(group_name):
        rows.insert(1, [InlineKeyboardButton(text="📚 Курсы", callback_data="menu|courses")])
        rows.insert(2, [InlineKeyboardButton(text="🎓 Образование", callback_data="menu|education")])
    if role in ("admin", "starosta", "teacher"):
        rows.append([InlineKeyboardButton(text="🛠 Управление", callback_data="menu|management")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_button(destination="home"):
    label = "🏠 Главное меню" if destination == "home" else "↩️ Назад"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"menu|{destination}")]])

def schedule_menu_keyboard():
    rows = [
        [InlineKeyboardButton(text="📅 Моё расписание", callback_data="menu|today")],
        [InlineKeyboardButton(text="🔔 Звонки", callback_data="menu|bells")],
        [InlineKeyboardButton(text="👨‍🏫 Преподаватель", callback_data="menu|teachers"), InlineKeyboardButton(text="🚪 Кабинет", callback_data="menu|rooms")],
        [InlineKeyboardButton(text="🎓 Группа / специальность", callback_data="menu|group")],
        [InlineKeyboardButton(text="📚 Образование и документы", callback_data="menu|education_schedule")],
        [InlineKeyboardButton(text="🏅 Спортивные секции", callback_data="menu|sports")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def safe_edit(target, text, reply_markup=None):
    try:
        await target.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise

async def remove_old_keyboard(m: Message):
    # Telegram keeps reply keyboards visible until the bot explicitly removes them.
    notice = await m.answer("✅", reply_markup=ReplyKeyboardRemove())
    await notice.delete()

@dp.callback_query(F.data.startswith("menu|"))
async def menu_action(c: CallbackQuery, state: FSMContext):
    action = c.data.split("|", 1)[1]
    if action == "home":
        u = await get_user(c.from_user.id)
        role = "admin" if c.from_user.id == ADMIN_ID else (u["role"] if u else "student")
        await safe_edit(c.message, "🎓 <b>ПАРА | КузПК</b>\nВыбери раздел:", reply_markup=main_menu(role, u["group_name"] if u else None))
    elif action == "schedule":
        await safe_edit(c.message, "📅 <b>Расписание</b>\nВыбери, что посмотреть:", reply_markup=schedule_menu_keyboard())
    elif action == "courses":
        user = await get_user(c.from_user.id)
        if not user or not is_courses_group(user.get("group_name")):
            await c.answer("Сначала выбери свою учебную группу.", show_alert=True)
            return
        await show_moodle_courses(c.message, user.get("group_name"))
    elif action in ("education", "education_schedule"):
        user = await get_user(c.from_user.id)
        if not user or not is_courses_group(user.get("group_name")):
            await c.answer("Сначала выбери свою учебную группу.", show_alert=True)
            return
        await show_education_menu(
            c.message, user.get("group_name"),
            back_to="schedule" if action == "education_schedule" else "home",
        )
    elif action == "sports":
        await show_sports_sections(c.message)
    elif action in ("today", "tomorrow"):
        await render_schedule(c.message, c.from_user.id, datetime.now(TZ).date() + timedelta(days=0 if action == "today" else 1), edit=True)
    elif action == "week":
        today = datetime.now(TZ).date()
        monday = today - timedelta(days=today.weekday())
        await render_schedule(c.message, c.from_user.id, monday, "w", today.weekday(), edit=True)
    elif action == "group":
        kb = await group_keyboard()
        await safe_edit(c.message, "🎓 <b>Выбор учебной группы</b>\nСначала выбери специальность:", reply_markup=kb or back_button())
    elif action == "bells":
        rows = await get_bells()
        await safe_edit(c.message, format_bells(rows), reply_markup=back_button())
    elif action == "teachers":
        await show_entity_picker(c.message, "teacher", 0, edit=True)
    elif action == "rooms":
        await show_building_picker(c.message, edit=True)
    elif action == "announcements":
        rows = await recent_announcements()
        cards = [f"📣 <b>{escape(r['group_name'] or 'Колледж')}</b> · <i>{escape(r['created_at'])}</i>\n{escape(r['body'])}" for r in rows[:5]]
        await safe_edit(c.message, "📢 <b>Последние объявления</b>\n\n" + ("\n\n──────────\n\n".join(cards) if cards else "Пока объявлений нет."), reply_markup=back_button())
    elif action == "management":
        u = await get_user(c.from_user.id)
        role = "admin" if c.from_user.id == ADMIN_ID else (u["role"] if u else "student")
        if role == "student":
            await c.answer("Недостаточно прав.", show_alert=True); return
        rows = [[InlineKeyboardButton(text="📣 Объявление группе", callback_data="manage|group")]]
        if c.from_user.id == ADMIN_ID:
            rows += [
                [InlineKeyboardButton(text="📊 Пользователи", callback_data="manage|stats")],
                [InlineKeyboardButton(text="📢 Объявление всем", callback_data="manage|all")],
                [InlineKeyboardButton(text="👥 Назначить роль", callback_data="manage|roles")],
            ]
        rows += [[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")]]
        await safe_edit(c.message, "🛠 <b>Управление</b>\nВыбери действие:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


EDUCATION_TYPES = {
    "calendar": ("Календарный график · 2026–2027, I семестр", "📅"),
    "plans": ("Учебные планы · 2026–2027", "📘"),
    "gia": ("Программы ГИА", "🎓"),
    "gia_archive": ("Архив программ ГИА · 2025–2026", "🗂️"),
}

GIA_ARCHIVE_SUMMARY = {
    "49.02.01": ("Физическая культура", "18 мая – 28 июня 2026", 21),
    "44.02.01": ("Дошкольное образование", "май – июнь 2026; точные даты — в программе", 42),
    "44.02.02": ("Преподавание в начальных классах", "май – июнь 2026; точные даты — в программе", 43),
    "44.02.03": ("Педагогика дополнительного образования", "май – июнь 2026; точные даты — в программе", 20),
    "44.02.04": ("Специальное дошкольное образование", "май – июнь 2026; точные даты — в программе", 21),
    "44.02.05": ("Коррекционная педагогика в начальном образовании", "май – июнь 2026; точные даты — в программе", 38),
    "44.02.06": ("Профессиональное обучение (по отраслям)", "май – июнь 2026; точные даты — в программе", 18),
    "39.02.01": ("Социальная работа", "май – июнь 2026; точные даты — в программе", 32),
}


def _gia_archive_summary(group_name):
    prefix = str(group_name or "").split("-", 1)[0].upper().strip()
    code = SPECIALTY_CODES.get(prefix)
    return (code, GIA_ARCHIVE_SUMMARY.get(code))


async def show_education_menu(target, group_name, back_to="home"):
    education_action = "education_schedule" if back_to == "schedule" else "education"
    buttons = [
        [InlineKeyboardButton(text="📅 Календарный график 2026–2027", callback_data=f"edu|calendar|{back_to}")],
        [InlineKeyboardButton(text="📘 Учебные планы 2026–2027", callback_data=f"edu|plans|{back_to}")],
        [InlineKeyboardButton(text="🎓 Программы ГИА", callback_data=f"edu|gia|{back_to}")],
        [InlineKeyboardButton(text="🗂️ Архив ГИА 2025–2026", callback_data=f"edu|gia_archive|{back_to}")],
        [InlineKeyboardButton(text="🔗 Официальное расписание КузПК", url="https://kuzpc.kemobl.ru/studentu/raspisanie/")],
        [InlineKeyboardButton(
            text="↩️ К расписанию" if back_to == "schedule" else "🏠 Главное меню",
            callback_data="menu|schedule" if back_to == "schedule" else "menu|home",
        )],
    ]
    await safe_edit(target,
        f"🎓 <b>Образование · {escape(str(group_name))}</b>\n\n"
        "Документы подобраны по специальности твоей группы. Выбери раздел:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("edu|"))
async def show_education_documents(c: CallbackQuery):
    user = await get_user(c.from_user.id)
    group_name = user.get("group_name") if user else None
    if not user or not is_courses_group(group_name):
        await c.answer("Сначала выбери свою учебную группу.", show_alert=True)
        return
    parts = c.data.split("|")
    kind = parts[1] if len(parts) > 1 else ""
    back_to = parts[2] if len(parts) > 2 and parts[2] in ("home", "schedule") else "home"
    education_action = "education_schedule" if back_to == "schedule" else "education"
    if kind not in EDUCATION_TYPES:
        await c.answer("Этот раздел не найден.", show_alert=True)
        return
    title, icon = EDUCATION_TYPES[kind]
    try:
        documents = await list_documents(group_name, kind)
    except EducationError as error:
        await safe_edit(c.message,
            f"{icon} <b>{escape(title)}</b>\n\n{escape(str(error))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ К разделам образования", callback_data=f"menu|{education_action}")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")],
            ]))
        await c.answer()
        return
    source_url = PLANS_URL if kind == "plans" else EDUCATION_URL
    buttons = [
        [InlineKeyboardButton(text="↩️ К разделам образования", callback_data=f"menu|{education_action}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")],
    ]
    if documents:
        text = (f"{icon} <b>{escape(title)}</b>\n"
                f"Специальность группы <b>{escape(str(group_name))}</b>\n\n"
                "Отправляю документы файлами. Источник указан под каждым документом:")
    else:
        text = (f"{icon} <b>{escape(title)}</b>\n"
                f"Для специальности группы <b>{escape(str(group_name))}</b> "
                "документы в этом разделе пока не опубликованы.\n\n"
                "Можно посмотреть страницу-источник колледжа:")
        buttons.insert(0, [InlineKeyboardButton(text="🌐 Открыть страницу-источник", url=source_url)])
    if kind == "gia_archive":
        code, summary = _gia_archive_summary(group_name)
        if summary:
            specialty, dates, pages = summary
            text = (
                f"🗂️ <b>Архив ГИА · 2025–2026</b>\n"
                f"Специальность: <b>{escape(specialty)}</b> · <code>{escape(code)}</code>\n\n"
                "Вид ГИА: защита выпускной квалификационной работы (ВКР).\n"
                f"Сроки по сводке: {escape(dates)}.\n"
                f"Объём полной программы: {pages} стр.\n\n"
            )
            if documents:
                text += "Прикрепляю архивную программу файлом."
            elif code == "44.02.06":
                text += "Полной программы этой специальности в присланном архиве нет."
            else:
                text += "Полный PDF пока не подключён к боту."
            text += f"\nИсточник: <a href=\"{escape(EDUCATION_URL, quote=True)}\">раздел «Образование» КузПК</a>."
        else:
            text = (
                f"🗂️ <b>Архив ГИА · 2025–2026</b>\n\n"
                f"Для специальности группы <b>{escape(str(group_name))}</b> программа в сводке не найдена.\n"
                f"Источник: <a href=\"{escape(EDUCATION_URL, quote=True)}\">раздел «Образование» КузПК</a>."
            )
        buttons = [
            [InlineKeyboardButton(text="↩️ К разделам образования", callback_data=f"menu|{education_action}")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")],
        ]
    await safe_edit(c.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await c.answer()
    for doc in documents[:50]:
        try:
            data, filename = await download_document(doc["url"])
            caption = (f"📄 <b>{escape(doc['name'])}</b>\n"
                       f"Источник: <a href=\"{escape(source_url, quote=True)}\">сайт КузПК · Образование</a>")
            await c.message.answer_document(
                BufferedInputFile(data, filename=filename),
                caption=caption,
                parse_mode="HTML",
            )
        except EducationError as error:
            logging.exception("Failed to download education document %r", doc.get("name"))
            await c.message.answer(
                f"⚠️ Не удалось отправить «{escape(doc['name'])}». "
                f"<a href=\"{escape(doc['url'], quote=True)}\">Открыть документ на сайте</a>.\n"
                f"Источник: <a href=\"{escape(source_url, quote=True)}\">КузПК · Образование</a>\n"
                f"{escape(str(error))}",
                parse_mode="HTML",
            )
        except Exception:
            # Telegram/network errors are different from download errors; still give
            # the student a working link instead of silently failing the callback.
            logging.exception("Failed to send education document %r", doc.get("name"))
            try:
                await c.message.answer(
                    f"⚠️ Не получилось прикрепить файл «{escape(doc['name'])}» к сообщению.\n"
                    f"<a href=\"{escape(doc['url'], quote=True)}\">Открыть и скачать документ</a> · "
                    f"<a href=\"{escape(source_url, quote=True)}\">источник: КузПК</a>",
                    parse_mode="HTML",
                )
            except Exception:
                logging.exception("Failed to send education document fallback link")


async def show_sports_sections(target):
    rows = [[InlineKeyboardButton(text=f"🏅 {section['name']}", callback_data=f"sports|{i}")]
            for i, section in enumerate(SPORTS_SECTIONS)]
    rows.append([InlineKeyboardButton(text="↩️ К расписанию", callback_data="menu|schedule")])
    await safe_edit(target,
        "🏅 <b>Спортивные секции · 2026–2027</b>\n\nВыбери секцию, чтобы посмотреть дни и время занятий:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("sports|"))
async def sports_section(c: CallbackQuery):
    raw_index = c.data.split("|", 1)[1]
    if not raw_index.isdigit() or int(raw_index) >= len(SPORTS_SECTIONS):
        await c.answer("Список секций обновился.", show_alert=True)
        return
    section = SPORTS_SECTIONS[int(raw_index)]
    schedule = "\n".join(
        f"• <b>{SPORTS_DAYS[day]}</b> · {time}"
        for day, time in section["days"].items()
    )
    rows = [[InlineKeyboardButton(text="↩️ Все секции", callback_data="menu|sports")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")]]
    await safe_edit(c.message,
        f"🏅 <b>{escape(section['name'])}</b>\n\n{schedule}\n\n👤 Ответственный: <b>{escape(section['coach'])}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()


async def show_moodle_courses(target, group_name):
    try:
        courses = await list_group_courses(group_name)
    except MoodleError as error:
        await safe_edit(target, f"📚 <b>Курсы группы {escape(str(group_name))}</b>\n\n{escape(str(error))}", reply_markup=back_button())
        return
    if not courses:
        await safe_edit(target, f"📚 <b>Курсы группы {escape(str(group_name))}</b>\n\nВ открытом списке Moodle курсы не найдены.", reply_markup=back_button())
        return
    categories = categorize_courses(courses)
    buttons = [[InlineKeyboardButton(
        text=f"📚 {category['label']} · {len(category['courses'])}",
        callback_data=f"coursecat|{key}")]
        for key, category in categories.items()]
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")])
    await safe_edit(target, f"📚 <b>Курсы группы {escape(str(group_name))}</b> · {len(courses)}\n\nВыбери раздел:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("coursecat|"))
async def show_course_category(c: CallbackQuery):
    user = await get_user(c.from_user.id)
    group_name = user.get("group_name") if user else None
    if not user or not is_courses_group(group_name):
        await c.answer("Сначала выбери свою учебную группу.", show_alert=True)
        return
    try:
        courses = await list_group_courses(group_name)
    except MoodleError as error:
        await safe_edit(c.message, f"📚 <b>Курсы группы {escape(str(group_name))}</b>\n\n{escape(str(error))}", reply_markup=back_button())
        await c.answer()
        return
    key = c.data.split("|", 1)[1]
    category = categorize_courses(courses).get(key)
    if not category:
        await safe_edit(c.message, "Раздел курсов изменился. Открой его снова.", reply_markup=back_button())
    else:
        buttons = [[InlineKeyboardButton(text=f"📘 {course['name'][:58]}", url=course["url"])]
                   for course in category["courses"][:90]]
        buttons.append([InlineKeyboardButton(text="↩️ Типы курсов", callback_data="menu|courses")])
        buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")])
        await safe_edit(c.message,
            f"📚 <b>{escape(category['label'])}</b> · группа {escape(str(group_name))}\n\nНажми на курс, чтобы открыть его в Moodle:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await c.answer()

async def show_entity_picker(target, kind: str, page: int, edit: bool = False):
    names = await get_entity_names(kind)
    label = "преподавателя" if kind == "teacher" else "кабинет"
    if not names:
        text = f"Список {label}ов пуст."
        if edit: await safe_edit(target, text, reply_markup=back_button())
        else: await target.answer(text, reply_markup=back_button())
        return
    size = 8
    pages = (len(names) + size - 1) // size
    page = max(0, min(page, pages - 1))
    today = datetime.now(TZ).date()
    buttons = [[InlineKeyboardButton(text=name[:60], callback_data=f"entity|{kind}|{i}|{today:%Y%m%d}")
                for i, name in enumerate(names[page*size:(page+1)*size], start=page*size)]
               [i:i+2] for i in range(0, len(names[page*size:(page+1)*size]), 2)]
    nav = []
    if page: nav.append(InlineKeyboardButton(text="‹", callback_data=f"pick|{kind}|{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="pick|noop|0"))
    if page + 1 < pages: nav.append(InlineKeyboardButton(text="›", callback_data=f"pick|{kind}|{page+1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")])
    heading = "👨‍🏫 <b>Выбери преподавателя</b>" if kind == "teacher" else "🚪 <b>Выбери кабинет</b>"
    text = f"{heading}\n\nСтраница {page+1} из {pages} · всего: {len(names)}"
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if edit: await safe_edit(target, text, reply_markup=markup)
    else: await target.answer(text, reply_markup=markup)

@dp.callback_query(F.data.startswith("pick|"))
async def entity_picker_page(c: CallbackQuery):
    _, kind, raw_page = c.data.split("|")
    if kind != "noop": await show_entity_picker(c.message, kind, int(raw_page), edit=True)
    await c.answer()

async def render_entity_schedule(target, kind: str, index: int, day: date, edit: bool = True):
    names = await get_entity_names(kind)
    if index < 0 or index >= len(names):
        await safe_edit(target, "Список изменился. Выбери запись ещё раз.", reply_markup=back_button())
        return
    name = names[index]
    rows = await get_entity_schedule(kind, name, day)
    text = format_schedule(name, day, rows, entity_type=kind)
    picker_page = index // 8
    nav = [InlineKeyboardButton(text="‹ День", callback_data=f"entity|{kind}|{index}|{(day-timedelta(days=1)):%Y%m%d}"),
           InlineKeyboardButton(text="Сегодня", callback_data=f"entity|{kind}|{index}|{datetime.now(TZ).date():%Y%m%d}"),
           InlineKeyboardButton(text="День ›", callback_data=f"entity|{kind}|{index}|{(day+timedelta(days=1)):%Y%m%d}")]
    return_callback = f"building|{room_building(name)}" if kind == "room" else f"pick|{kind}|{picker_page}"
    return_label = "↩️ Корпус" if kind == "room" else "↩️ К списку"
    markup = InlineKeyboardMarkup(inline_keyboard=[nav,
        [InlineKeyboardButton(text=return_label, callback_data=return_callback), InlineKeyboardButton(text="🏠 Меню", callback_data="menu|home")]])
    if edit: await safe_edit(target, text, reply_markup=markup)
    else: await target.answer(text, reply_markup=markup)

@dp.callback_query(F.data.startswith("entity|"))
async def entity_schedule_navigation(c: CallbackQuery):
    _, kind, raw_index, raw_date = c.data.split("|")
    day = datetime.strptime(raw_date, "%Y%m%d").date()
    await render_entity_schedule(c.message, kind, int(raw_index), day)
    await c.answer()

def building_title(building):
    titles = {1: "1 корпус · Главный", 2: "2 корпус · Мастерские",
              3: "3 корпус · Спортивный комплекс", 4: "4 корпус · Общежитие"}
    return titles.get(building, "Другие помещения")

async def show_building_picker(target, edit=True):
    names = await get_entity_names("room")
    buildings = sorted({room_building(name) for name in names})
    buttons = []
    for building in buildings:
        count = sum(room_building(name) == building for name in names)
        buttons.append([InlineKeyboardButton(text=f"{building_title(building)} · {count}", callback_data=f"building|{building}")])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = "🚪 <b>Расписание по корпусам</b>\n\nВыбери корпус, затем кабинет:"
    if edit: await safe_edit(target, text, reply_markup=markup)
    else: await target.answer(text, reply_markup=markup)

async def show_rooms_in_building(target, building, page=0):
    names = await get_entity_names("room")
    filtered = [(i, name) for i, name in enumerate(names) if room_building(name) == building]
    size = 10
    pages = max(1, (len(filtered) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    chunk = filtered[page*size:(page+1)*size]
    buttons = [[InlineKeyboardButton(text=name[:60], callback_data=f"entity|room|{idx}|{datetime.now(TZ).date():%Y%m%d}")]
               for idx, name in chunk]
    nav = []
    if page: nav.append(InlineKeyboardButton(text="‹", callback_data=f"roomlist|{building}|{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="pick|noop|0"))
    if page + 1 < pages: nav.append(InlineKeyboardButton(text="›", callback_data=f"roomlist|{building}|{page+1}"))
    buttons.extend([nav, [InlineKeyboardButton(text="↩️ Корпуса", callback_data="building|list")]])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = f"🚪 <b>{building_title(building)}</b>\nВыбери кабинет · {len(filtered)} всего:"
    await safe_edit(target, text, reply_markup=markup)

@dp.callback_query(F.data.startswith("building|"))
async def building_callback(c: CallbackQuery):
    value = c.data.split("|", 1)[1]
    if value == "list": await show_building_picker(c.message)
    else: await show_rooms_in_building(c.message, int(value))
    await c.answer()

@dp.callback_query(F.data.startswith("roomlist|"))
async def room_list_page(c: CallbackQuery):
    _, building, page = c.data.split("|")
    await show_rooms_in_building(c.message, int(building), int(page))
    await c.answer()

@dp.callback_query(F.data.startswith("manage|"))
async def management_action(c: CallbackQuery, state: FSMContext):
    action = c.data.split("|", 1)[1]
    if action == "group":
        u = await get_user(c.from_user.id)
        if not u or (c.from_user.id != ADMIN_ID and u["role"] != "starosta"):
            await c.answer("Недостаточно прав.", show_alert=True); return
        await state.set_state(Forms.announcement)
        await safe_edit(c.message, "📣 <b>Объявление группе</b>\n\nОтправь текст сообщения. Рассылка пойдёт в твою группу.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="flow|cancel")]]))
    elif action == "all":
        if c.from_user.id != ADMIN_ID:
            await c.answer("Недостаточно прав.", show_alert=True); return
        await state.set_state(Forms.admin_announcement)
        await safe_edit(c.message, "📢 <b>Объявление колледжа</b>\n\nОтправь текст. Перед рассылкой я покажу предпросмотр и попрошу подтверждение.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="flow|cancel")]]))
    elif action == "roles":
        if c.from_user.id != ADMIN_ID:
            await c.answer("Недостаточно прав.", show_alert=True); return
        await safe_edit(c.message, "👥 <b>Роли</b>\nКоманда: <code>/setrole TELEGRAM_ID student|starosta|teacher</code>", reply_markup=back_button("management"))
    elif action == "stats":
        if c.from_user.id != ADMIN_ID:
            await c.answer("Недостаточно прав.", show_alert=True); return
        count = len(await all_users())
        await safe_edit(c.message, f"📊 <b>Статистика бота</b>\n\n👥 Зарегистрировано пользователей: <b>{count}</b>\n\nСчитаются аккаунты, которые запускали бота; администратор тоже включён.", reply_markup=back_button("management"))
    await c.answer()

@dp.callback_query(F.data == "flow|cancel")
async def cancel_flow(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, "Действие отменено.", reply_markup=back_button())
    await c.answer()

class Forms(StatesGroup):
    choose_group = State()
    announcement = State()
    admin_announcement = State()
    role_user_id = State()
    role_name = State()
    admin_announcement_confirm = State()

async def group_keyboard(prefix=None, page=0):
    groups = await list_groups()
    if not groups:
        return None
    prefixes = sorted({str(g).split("-", 1)[0] for g in groups})
    if prefix is None:
        rows = [[InlineKeyboardButton(text=f"🎓 {p} · {sum(str(g).startswith(p+'-') for g in groups)} групп", callback_data=f"spec|{p}|0")]
                for p in prefixes]
        rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    selected = [g for g in groups if str(g).startswith(prefix + "-")]
    size = 12
    pages = max(1, (len(selected) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    chunk = selected[page*size:(page+1)*size]
    rows = [[InlineKeyboardButton(text=g, callback_data=f"group:{g}")] for g in chunk]
    nav = []
    if page: nav.append(InlineKeyboardButton(text="‹", callback_data=f"spec|{prefix}|{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="spec|noop|0"))
    if page + 1 < pages: nav.append(InlineKeyboardButton(text="›", callback_data=f"spec|{prefix}|{page+1}"))
    rows.extend([nav, [InlineKeyboardButton(text="↩️ Специальности", callback_data="spec|list|0")]])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data.startswith("spec|"))
async def specialty_groups(c: CallbackQuery):
    _, prefix, page = c.data.split("|")
    if prefix == "noop":
        await c.answer(); return
    if prefix == "list":
        markup = await group_keyboard()
        text = "🎓 <b>Выбор учебной группы</b>\nСначала выбери специальность:"
    else:
        markup = await group_keyboard(prefix, int(page))
        count = sum(str(g).startswith(prefix + "-") for g in await list_groups())
        text = f"🎓 <b>Специальность {escape(prefix)}</b> · {count} групп\nВыбери группу:"
    await safe_edit(c.message, text, reply_markup=markup or back_button())
    await c.answer()

@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    user = await get_user(m.from_user.id)
    role = "admin" if m.from_user.id == ADMIN_ID else (user["role"] if user else "student")
    await add_user(m.from_user.id, m.from_user.full_name, role)
    user = await get_user(m.from_user.id)
    if not user["group_name"]:
        kb = await group_keyboard()
        if kb:
            await remove_old_keyboard(m)
            await m.answer("🎓 <b>Выбор учебной группы</b>\nСначала выбери специальность:", reply_markup=kb)
        else:
            await m.answer("Привет! Список групп пока не загружен. Администратор должен подключить расписание.", reply_markup=ReplyKeyboardRemove())
        return
    await remove_old_keyboard(m)
    await m.answer(f"🎓 <b>ПАРА | КузПК</b>\nТвоя группа: <b>{escape(str(user['group_name']))}</b>\n\nВыбери раздел ниже:", reply_markup=main_menu(role, user["group_name"]))

@dp.callback_query(F.data.startswith("group:"))
async def choose_group(c: CallbackQuery):
    group = c.data.split(":", 1)[1]
    if group not in await list_groups():
        await c.answer("Этой группы нет в расписании.", show_alert=True); return
    user = await get_user(c.from_user.id)
    role = "admin" if c.from_user.id == ADMIN_ID else (user["role"] if user else "student")
    # A saved inline group button can outlive the user's database row (for
    # example after a service database reset). Recreate the user before saving.
    await add_user(c.from_user.id, c.from_user.full_name, role)
    await set_group(c.from_user.id, group)
    user = await get_user(c.from_user.id)
    await safe_edit(c.message, f"🎓 <b>Группа сохранена: {escape(group)}</b>\n\nВыбери раздел:", reply_markup=main_menu(role, user["group_name"]))
    await c.answer()

def schedule_keyboard(day: date, mode: str, page: int = 0):
    if mode == "w":
        monday = day - timedelta(days=day.weekday())
        next_monday = monday + timedelta(days=7)
        previous_monday = monday - timedelta(days=7)
        nav = [InlineKeyboardButton(text="‹ Неделя", callback_data=f"sc|w|{previous_monday:%Y%m%d}|0"),
               InlineKeyboardButton(text=f"{monday:%d.%m}–{(monday + timedelta(days=6)):%d.%m}", callback_data="sc|noop|0|0"),
               InlineKeyboardButton(text="Неделя ›", callback_data=f"sc|w|{next_monday:%Y%m%d}|0")]
        week_anchor = monday
        today = datetime.now(TZ).date()
        day_for_daily = today if monday <= today <= monday + timedelta(days=6) else monday
    else:
        shown = day
        nav = [InlineKeyboardButton(text="‹ День", callback_data=f"sc|d|{(day-timedelta(days=1)):%Y%m%d}|0"),
               InlineKeyboardButton(text="Сегодня", callback_data=f"sc|d|{datetime.now(TZ).date():%Y%m%d}|0"),
               InlineKeyboardButton(text="День ›", callback_data=f"sc|d|{(day+timedelta(days=1)):%Y%m%d}|0")]
        week_anchor = day - timedelta(days=day.weekday())
        day_for_daily = day
    toggle = InlineKeyboardButton(text="📆 Неделя" if mode == "d" else "📅 По дням",
                                 callback_data=f"sc|{'w' if mode == 'd' else 'd'}|{week_anchor if mode == 'd' else day_for_daily:%Y%m%d}|0")
    return InlineKeyboardMarkup(inline_keyboard=[nav, [toggle, InlineKeyboardButton(text="↩️ Назад", callback_data="sc|back|0|0")]])

async def render_schedule(target, uid: int, day: date, mode: str = "d", page: int = 0, edit: bool = False):
    u = await get_user(uid)
    if not u or not u["group_name"]:
        text = "Сначала выбери группу командой /start."
        if edit: await safe_edit(target, text, reply_markup=None)
        else: await target.answer(text)
        return
    if mode == "w":
        monday = day - timedelta(days=day.weekday())
        schedule = await get_week_schedule(u["group_name"], monday)
        text = format_week_schedule(u["group_name"], monday, schedule)
        markup = schedule_keyboard(monday, mode)
    else:
        rows = await get_schedule(u["group_name"], day)
        text = format_schedule(u["group_name"], day, rows)
        markup = schedule_keyboard(day, mode)
    if edit: await safe_edit(target, text, reply_markup=markup)
    else: await target.answer(text, reply_markup=markup)

async def send_day(m: Message, offset: int):
    day = datetime.now(TZ).date() + timedelta(days=offset)
    await render_schedule(m, m.from_user.id, day)

@dp.message(F.text == "📅 Сегодня")
async def today(m: Message): await send_day(m, 0)

@dp.message(F.text == "📆 Завтра")
async def tomorrow(m: Message): await send_day(m, 1)

@dp.message(F.text == "🗓 На неделю")
async def week(m: Message):
    today = datetime.now(TZ).date()
    monday = today - timedelta(days=today.weekday())
    await render_schedule(m, m.from_user.id, monday, "w", today.weekday())

@dp.callback_query(F.data.startswith("sc|"))
async def schedule_navigation(c: CallbackQuery):
    _, mode, raw_day, raw_page = c.data.split("|")
    if mode == "noop":
        await c.answer(); return
    if mode == "back":
        await safe_edit(c.message, "📅 <b>Расписание</b>\nВыбери, что посмотреть:", reply_markup=schedule_menu_keyboard())
        await c.answer(); return
    if mode == "x":
        await c.message.delete(); await c.answer(); return
    day = datetime.strptime(raw_day, "%Y%m%d").date()
    await render_schedule(c.message, c.from_user.id, day, mode, int(raw_page), edit=True)
    await c.answer()

@dp.message(F.text == "🔔 Звонки")
async def bells(m: Message):
    rows = await get_bells()
    if not rows:
        await m.answer("Расписание звонков ещё не загружено.")
    elif rows and "text" in rows[0]:
        await m.answer("🔔 <b>Расписание звонков</b>\n\n" + "\n".join(escape(r["text"]) for r in rows))
    else:
        text = "🔔 <b>Расписание звонков</b>\n\n" + "\n".join(
            f"{r.get('day_type','')}: {r.get('lesson','')} — {r.get('start','')}–{r.get('end','')}"
            for r in rows)
        await m.answer(text)

@dp.message(F.text == "👨‍🏫 Преподаватели")
async def teachers(m: Message):
    rows = await get_teachers()
    await m.answer("👨‍🏫 <b>Преподаватели</b>\n\n" + ("\n".join(sorted(set(r.get("teacher","") for r in rows if r.get("teacher")))) or "Справочник пока пуст."))

@dp.message(F.text == "🚪 Аудитории")
async def rooms(m: Message):
    rows = await get_rooms()
    await m.answer("🚪 <b>Аудитории</b>\n\n" + ("\n".join(sorted(set(r.get("room","") for r in rows if r.get("room")))) or "Справочник пока пуст."))

@dp.message(F.text == "⚙️ Моя группа")
async def change_group(m: Message):
    kb = await group_keyboard()
    if kb: await m.answer("Выбери новую группу:", reply_markup=kb)
    else: await m.answer("Список групп пока не загружен.")

@dp.message(F.text == "📢 Объявления")
async def announcements(m: Message):
    rows = await recent_announcements()
    if not rows:
        await m.answer("📢 <b>Объявления</b>\n\nПока объявлений нет."); return
    cards = [f"📣 <b>{escape(r['group_name'] or 'Колледж')}</b> · <i>{escape(r['created_at'])}</i>\n{escape(r['body'])}" for r in rows]
    await m.answer("📢 <b>Последние объявления</b>\n\n" + "\n\n──────────\n\n".join(cards[:5]))

@dp.message(F.text == "🛠 Управление")
async def management(m: Message):
    u = await get_user(m.from_user.id)
    role = "admin" if m.from_user.id == ADMIN_ID else (u["role"] if u else "student")
    if role == "student":
        await m.answer("Недостаточно прав."); return
    buttons = [[InlineKeyboardButton(text="📣 Объявление группе", callback_data="manage|group")]]
    if m.from_user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="📊 Пользователи", callback_data="manage|stats")])
        buttons.append([InlineKeyboardButton(text="📢 Объявление всем", callback_data="manage|all")])
        buttons.append([InlineKeyboardButton(text="👥 Назначить роль", callback_data="manage|roles")])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await m.answer("Панель управления:", reply_markup=kb)

@dp.message(F.text == "📣 Объявление группе")
async def ask_group_announcement(m: Message, state: FSMContext):
    u = await get_user(m.from_user.id)
    if not u or (m.from_user.id != ADMIN_ID and u["role"] != "starosta"):
        await m.answer("Недостаточно прав."); return
    await state.set_state(Forms.announcement)
    await m.answer("Напиши текст объявления для своей группы:")

@dp.message(Forms.announcement)
async def group_announcement(m: Message, state: FSMContext):
    u = await get_user(m.from_user.id)
    if not u or (m.from_user.id != ADMIN_ID and u["role"] != "starosta"):
        await m.answer("Недостаточно прав."); await state.clear(); return
    if not u["group_name"]:
        await m.answer("У твоего аккаунта не назначена группа."); await state.clear(); return
    if not m.text:
        await m.answer("Отправь объявление текстом."); return
    body = m.text.strip()
    await save_announcement(m.from_user.id, u["group_name"], body)
    sent = await send_group_announcement(bot, u["group_name"], body)
    await m.answer(f"Готово. Отправлено: {sent} пользователям.")
    await state.clear()

@dp.message(F.text == "📢 Объявление всем")
async def ask_all_announcement(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        await m.answer("Эта функция доступна только главному администратору."); return
    await state.set_state(Forms.admin_announcement)
    await m.answer("Напиши текст общего объявления. Оно будет отправлено всем пользователям:")

@dp.message(Forms.admin_announcement)
async def all_announcement(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        await state.clear(); return
    if not m.text:
        await m.answer("Отправь объявление текстом."); return
    await state.update_data(pending_broadcast=m.text.strip())
    await state.set_state(Forms.admin_announcement_confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтвердить рассылку", callback_data="broadcast|yes"), InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast|no")]])
    await m.answer(f"Предпросмотр сообщения для всех зарегистрированных пользователей:\n\n<blockquote>{escape(m.text.strip())}</blockquote>\n\nОтправить?", reply_markup=kb)

@dp.callback_query(F.data.startswith("broadcast|"))
async def confirm_broadcast(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Недостаточно прав.", show_alert=True); return
    if c.data == "broadcast|no":
        await state.clear(); await safe_edit(c.message, "Рассылка отменена."); await c.answer(); return
    data = await state.get_data()
    body = data.get("pending_broadcast")
    if not body:
        await safe_edit(c.message, "Черновик не найден. Начни создание объявления заново."); await state.clear(); await c.answer(); return
    await save_announcement(c.from_user.id, "Колледж", body)
    sent = await send_all_announcement(bot, body)
    await safe_edit(c.message, f"✅ Объявление отправлено {sent} пользователям.")
    await state.clear()
    await c.answer()

@dp.message(Command("setrole"))
async def setrole_cmd(m: Message):
    if m.from_user.id != ADMIN_ID:
        await m.answer("Недостаточно прав."); return
    parts = (m.text or "").split()
    if len(parts) != 3 or parts[2] not in ("student", "starosta", "teacher"):
        await m.answer("Формат: /setrole TELEGRAM_ID student|starosta|teacher"); return
    try: target_id = int(parts[1])
    except ValueError:
        await m.answer("Telegram ID должен быть числом."); return
    user = await get_user(target_id)
    if not user:
        await m.answer("Этот пользователь ещё не запускал бота."); return
    if target_id == ADMIN_ID:
        await m.answer("Нельзя менять роль главного администратора."); return
    await set_role(target_id, parts[2])
    await m.answer(f"Роль пользователя {target_id} изменена на {parts[2]}.")

@dp.message(Command("stats"))
async def stats_cmd(m: Message):
    if m.from_user.id != ADMIN_ID:
        await m.answer("Эта команда доступна только администратору.")
        return
    count = len(await all_users())
    await m.answer(f"📊 <b>Статистика бота</b>\n\n👥 Зарегистрировано пользователей: <b>{count}</b>\n\nСчитаются аккаунты, которые запускали бота; администратор тоже включён.")

@dp.message(F.text == "👥 Назначить роль")
async def role_help(m: Message):
    if m.from_user.id != ADMIN_ID:
        await m.answer("Недостаточно прав."); return
    await m.answer("Команда: /setrole TELEGRAM_ID student|starosta|teacher\nАдминистратора нельзя назначить этой командой.")

@dp.message(F.text == "↩️ Главное меню")
async def back_menu(m: Message):
    u = await get_user(m.from_user.id)
    role = "admin" if m.from_user.id == ADMIN_ID else (u["role"] if u else "student")
    await m.answer("Главное меню:", reply_markup=main_menu(role, u["group_name"] if u else None))

@dp.message(Command("help"))
async def help_cmd(m: Message):
    commands = "Команды:\n/start — регистрация или главное меню\n/help — помощь\n"
    if m.from_user.id == ADMIN_ID:
        commands += "/stats — число зарегистрированных пользователей\n/setrole ID student|starosta|teacher — назначить роль\n"
    commands += "\nРасписание листается кнопками под сообщением. Объявления доступны в меню."
    await m.answer(commands)

@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Текущее действие отменено.")

async def main():
    await init_db()
    if ADMIN_ID:
        await add_user(ADMIN_ID, "Администратор", "admin")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
