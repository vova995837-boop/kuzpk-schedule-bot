import asyncio, logging, os, hashlib, re
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, BufferedInputFile
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from db import init_db, add_user, get_user, set_group, set_college, set_college_branch, set_role, list_groups, users_for_group, all_users, save_announcement, recent_announcements, save_schedule_update, save_bells_update, schedule_override_groups, get_user_settings, update_user_setting, toggle_sport_subscription, get_sport_subscriptions, users_for_sport, users_for_group_preference, users_with_preference, claim_notification, get_auto_schedule_snapshots, save_auto_schedule_snapshots
from sheets import get_schedule, get_week_schedule, get_teachers, get_rooms, get_bells, get_entity_names, get_entity_schedule, room_building
from services import format_schedule, format_week_schedule, format_bells, send_group_announcement, send_college_announcement, send_all_announcement
from moodle import list_group_courses, categorize_courses, MoodleError, SPECIALTY_CODES
from education import list_documents, download_document, EducationError, EDUCATION_URL, PLANS_URL
from schedule_imports import parse_schedule_file, parse_bells_file, analyze_schedule_image, parse_bells_text, detect_college, apply_file_instruction, identify_text_target, interpret_text_instruction

load_dotenv()
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Novokuznetsk"))

if not TOKEN or TOKEN in ("ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER", "PASTE_TOKEN_HERE", "YOUR_BOT_TOKEN"):
    raise RuntimeError("Укажи действующий BOT_TOKEN в файле .env (не отправляй его в чат).")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

SUBSCRIPTION_CHANNEL = "@prostoviva"
SUBSCRIPTION_URL = "https://t.me/prostoviva"

def subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=SUBSCRIPTION_URL)],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="subscription|check")],
    ])

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(SUBSCRIPTION_CHANNEL, user_id)
        return member.status in {"member", "administrator", "creator"}
    except Exception:
        logging.exception("Не удалось проверить подписку на канал %s", SUBSCRIPTION_CHANNEL)
        return False

async def show_subscription_prompt(message: Message):
    await message.answer(
        "🔒 <b>Чтобы пользоваться ботом, подпишись на наш Telegram-канал.</b>\n"
        "После подписки нажми кнопку «Проверить подписку».",
        reply_markup=subscription_keyboard(),
    )

class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        if user.id == ADMIN_ID:
            return await handler(event, data)
        if isinstance(event, Message) and event.text and event.text.split() and event.text.split()[0].split("@")[0] == "/start":
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "subscription|check":
            return await handler(event, data)
        if await is_subscribed(user.id):
            return await handler(event, data)
        if isinstance(event, Message):
            await show_subscription_prompt(event)
        elif isinstance(event, CallbackQuery):
            await event.answer("Сначала подпишись на канал.", show_alert=True)
        return None

dp.message.outer_middleware(SubscriptionMiddleware())
dp.callback_query.outer_middleware(SubscriptionMiddleware())

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

def main_menu(role="student", group_name=None, college_id="kuzpk", hidden_menu=()):
    hidden = set(hidden_menu or ())
    rows = []
    if "schedule" not in hidden:
        rows.append([InlineKeyboardButton(text="📅 Расписание", callback_data="menu|schedule")])
    if "announcements" not in hidden:
        rows.append([InlineKeyboardButton(text="📢 Объявления", callback_data="menu|announcements")])
    if "group" not in hidden:
        rows.append([InlineKeyboardButton(text="🎓 Моя группа", callback_data="menu|group")])
    if "notifications" not in hidden:
        rows.append([InlineKeyboardButton(text="🔔 Уведомления", callback_data="settings|notifications")])
    if is_courses_group(group_name) and college_id == "kuzpk":
        if "courses" not in hidden: rows.append([InlineKeyboardButton(text="📚 Курсы", callback_data="menu|courses")])
        if "education" not in hidden: rows.append([InlineKeyboardButton(text="🎓 Образование", callback_data="menu|education")])
    if role in ("admin", "starosta", "teacher"):
        rows.append([InlineKeyboardButton(text="🛠 Управление", callback_data="menu|management")])
    rows.append([InlineKeyboardButton(text="🏫 Сменить колледж", callback_data="menu|college")])
    rows.append([InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings|home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_button(destination="home"):
    label = "🏠 Главное меню" if destination == "home" else "↩️ Назад"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"menu|{destination}")]])

def schedule_menu_keyboard(college_id="kuzpk", hidden_menu=()):
    hidden = set(hidden_menu or ())
    rows = [
        [InlineKeyboardButton(text="📅 Моё расписание", callback_data="menu|today")] if "schedule" not in hidden else [],
        [InlineKeyboardButton(text="🔔 Звонки", callback_data="menu|bells")] if "bells" not in hidden and college_id != "pgt" else [],
        ([InlineKeyboardButton(text="👨‍🏫 Преподаватель", callback_data="menu|teachers")] if "teachers" not in hidden else []) + ([InlineKeyboardButton(text="🚪 Кабинет", callback_data="menu|rooms")] if "rooms" not in hidden else []),
        [InlineKeyboardButton(text="🎓 Группа / специальность", callback_data="menu|group")] if "group" not in hidden else [],
        [InlineKeyboardButton(text="👀 Посмотреть другую группу", callback_data="menu|browse_group")] if "schedule" not in hidden else [],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")],
    ]
    rows = [row for row in rows if row]
    if college_id == "kuzpk":
        if "education" not in hidden: rows.insert(-1, [InlineKeyboardButton(text="📚 Образование и документы", callback_data="menu|education_schedule")])
        if "sports" not in hidden: rows.insert(-1, [InlineKeyboardButton(text="🏅 Спортивные секции", callback_data="menu|sports")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


COLLEGES = {"kuzpk": "КузПК", "kuztagis": "КузТАГиС", "pgt": "ПГТ им. В. П. Романова", "yamk": "ЯМК"}
COLLEGE_CITIES = {"kuzpk": "Кемерово", "kuztagis": "Кемерово", "pgt": "Прокопьевск", "yamk": "Салехард"}
COLLEGE_REGIONS = {"kuzpk": "kuzbass", "kuztagis": "kuzbass", "pgt": "kuzbass", "yamk": "yanao"}

def college_label(college_id):
    return f"{COLLEGES.get(college_id, 'КузПК')} · {COLLEGE_CITIES.get(college_id, 'Кемерово')}"

def college_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎓 КузПК · Кемерово", callback_data="college|kuzpk")],
        [InlineKeyboardButton(text="🏫 КузТАГиС · Кемерово", callback_data="college|kuztagis")],
        [InlineKeyboardButton(text="🏢 ПГТ им. В. П. Романова · Прокопьевск", callback_data="college|pgt")],
        [InlineKeyboardButton(text="🏫 ЯМК · Салехард", callback_data="college|yamk")],
        [InlineKeyboardButton(text="↩️ Главное меню", callback_data="menu|home")],
    ])

def city_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛏ Кузбасс · Кемерово и Прокопьевск", callback_data="city|kuzbass")],
        [InlineKeyboardButton(text="❄️ ЯНАО · Салехард", callback_data="city|yanao")],
    ])

def colleges_in_city(city):
    return [key for key, value in COLLEGE_CITIES.items() if value == city]

def city_college_keyboard(city):
    if city == "kuzbass":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏙 Кемерово", callback_data="city|kemerovo")],
            [InlineKeyboardButton(text="🏙 Прокопьевск", callback_data="city|prokopyevsk")],
            [InlineKeyboardButton(text="↩️ Выбрать регион", callback_data="menu|city")],
        ])
    rows = [[InlineKeyboardButton(text=f"🎓 {COLLEGES[key]} · {COLLEGE_CITIES[key]}", callback_data=f"college|{key}")]
            for key in colleges_in_city(city)]
    rows.append([InlineKeyboardButton(text="↩️ Выбрать город", callback_data="city|kuzbass" if city in ("Кемерово", "Прокопьевск") else "menu|city")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def yamk_menu_keyboard():
    from yamk import DEPARTMENTS
    rows = [[InlineKeyboardButton(text=f"📚 {label}", callback_data=f"yamk|department|{key}")]
            for key, (label, _description) in DEPARTMENTS.items()]
    rows += [
        [InlineKeyboardButton(text="👨‍🏫 Преподаватели", callback_data="yamk|teachers")],
        [InlineKeyboardButton(text="📄 Расписания и документы", callback_data="yamk|docs")],
        [InlineKeyboardButton(text="🌐 Расписание и документы ЯМК", url="https://ymk.yanao.ru/activity/38207/")],
        [InlineKeyboardButton(text="🏙 Сменить регион или колледж", callback_data="menu|college")],
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
    if action in ("home", "schedule"):
        user = await get_user(c.from_user.id)
        if user and user.get("college_id") == "yamk":
            await safe_edit(c.message, "🏫 <b>ЯМК · Салехард</b>\n\nВыбери отделение или преподавателя:", reply_markup=yamk_menu_keyboard())
            await c.answer()
            return
    if action == "home":
        u = await get_user(c.from_user.id)
        prefs = await get_user_settings(c.from_user.id)
        role = "admin" if c.from_user.id == ADMIN_ID else (u["role"] if u else "student")
        college = (u or {}).get("college_id", "kuzpk")
        await safe_edit(c.message, f"🎓 <b>Расписание · {college_label(college)}</b>\nВыбери раздел:", reply_markup=main_menu(role, u["group_name"] if u else None, college, prefs["hidden_menu"]))
    elif action == "college":
        await safe_edit(c.message, "🗺 <b>Выбери регион</b>", reply_markup=city_keyboard())
    elif action == "city":
        await safe_edit(c.message, "🗺 <b>Выбери регион</b>", reply_markup=city_keyboard())
    elif action == "schedule":
        u = await get_user(c.from_user.id)
        prefs = await get_user_settings(c.from_user.id)
        college_id = (u or {}).get("college_id", "kuzpk")
        await safe_edit(c.message, f"📅 <b>Расписание · {college_label(college_id)}</b>\nВыбери, что посмотреть:", reply_markup=schedule_menu_keyboard(college_id, prefs["hidden_menu"]))
    elif action == "courses":
        user = await get_user(c.from_user.id)
        if not user or user.get("college_id") != "kuzpk" or not is_courses_group(user.get("group_name")):
            await c.answer("Сначала выбери свою учебную группу.", show_alert=True)
            return
        await show_moodle_courses(c.message, user.get("group_name"))
    elif action in ("education", "education_schedule"):
        user = await get_user(c.from_user.id)
        if not user or user.get("college_id") != "kuzpk" or not is_courses_group(user.get("group_name")):
            await c.answer("Сначала выбери свою учебную группу.", show_alert=True)
            return
        await show_education_menu(
            c.message, user.get("group_name"),
            back_to="schedule" if action == "education_schedule" else "home",
        )
    elif action == "sports":
        await show_sports_sections(c.message, c.from_user.id)
    elif action in ("today", "tomorrow"):
        await render_schedule(c.message, c.from_user.id, datetime.now(TZ).date() + timedelta(days=0 if action == "today" else 1), edit=True)
    elif action == "week":
        today = datetime.now(TZ).date()
        monday = today - timedelta(days=today.weekday())
        await render_schedule(c.message, c.from_user.id, monday, "w", today.weekday(), edit=True)
    elif action == "group":
        user = await get_user(c.from_user.id)
        college_id = (user or {}).get("college_id", "kuzpk")
        if college_id == "yamk":
            await safe_edit(c.message,
                "🏫 <b>ЯМК · Салехард</b>\n\nВыбери отделение, чтобы открыть его специальности и группы:",
                reply_markup=yamk_menu_keyboard())
            await c.answer()
            return
        kb = await group_keyboard(college_id=college_id)
        await safe_edit(c.message, "🎓 <b>Выбор учебной группы</b>\nСначала выбери специальность:", reply_markup=kb or back_button())
    elif action == "browse_group":
        user = await get_user(c.from_user.id)
        college_id = (user or {}).get("college_id", "kuzpk")
        if college_id == "yamk":
            await safe_edit(c.message, "🏫 <b>ЯМК</b>\nДля просмотра расписания выбери отделение:", reply_markup=yamk_menu_keyboard())
        else:
            kb = await group_keyboard(college_id=college_id, view_mode=True)
            await safe_edit(c.message, "👀 <b>Временный просмотр другой группы</b>\nВыбери специальность. Основная группа профиля не изменится:", reply_markup=kb or back_button("schedule"))
    elif action == "settings":
        await show_settings(c.message, c.from_user.id, "home")
    elif action == "notifications":
        await show_settings(c.message, c.from_user.id, "notifications")
    elif action == "bells":
        user = await get_user(c.from_user.id)
        await show_bells_schedule(c.message, (user or {}).get("college_id", "kuzpk"),
                                  datetime.now(TZ).date(), full=False, edit=True)
    elif action == "teachers":
        user = await get_user(c.from_user.id)
        await show_entity_picker(c.message, "teacher", 0, edit=True, college_id=(user or {}).get("college_id", "kuzpk"), college_branch=(user or {}).get("college_branch"))
    elif action == "rooms":
        user = await get_user(c.from_user.id)
        college_id = (user or {}).get("college_id", "kuzpk")
        if college_id in ("pgt", "kuztagis", "yamk"):
            await show_entity_picker(c.message, "room", 0, edit=True, college_id=college_id, college_branch=(user or {}).get("college_branch"))
        else:
            await show_building_picker(c.message, edit=True, college_id=college_id)
    elif action == "announcements":
        user = await get_user(c.from_user.id)
        college_id = (user or {}).get("college_id", "kuzpk")
        rows = await recent_announcements(college_id=college_id)
        cards = [f"📣 <b>{escape(r['group_name'] or 'Колледж')}</b> · <i>{escape(r['created_at'])}</i>\n{escape(r['body'])}" for r in rows[:5]]
        await safe_edit(c.message, "📢 <b>Последние объявления</b>\n\n" + ("\n\n──────────\n\n".join(cards) if cards else "Пока объявлений нет."), reply_markup=back_button())
    elif action == "management":
        u = await get_user(c.from_user.id)
        role = "admin" if c.from_user.id == ADMIN_ID else (u["role"] if u else "student")
        college_id = (u or {}).get("college_id", "kuzpk")
        if role == "student":
            await c.answer("Недостаточно прав.", show_alert=True); return
        rows = [[InlineKeyboardButton(text="📣 Объявление группе", callback_data="manage|group")]]
        if c.from_user.id == ADMIN_ID:
            rows += [[InlineKeyboardButton(text="📊 Пользователи", callback_data="manage|stats")]]
            rows += [[InlineKeyboardButton(text="🤖 ИИ · обновить расписание", callback_data="manage|schedule_update")]]
            rows += [[InlineKeyboardButton(text=f"📢 Объявление · {college_name}", callback_data=f"manage|college:{key}")]
                     for key, college_name in COLLEGES.items()]
            rows += [
                [InlineKeyboardButton(text="📢 Объявление всем колледжам", callback_data="manage|all")],
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

MENU_SETTING_LABELS = {
    "schedule": "📅 Расписание", "bells": "🔔 Звонки", "teachers": "👨‍🏫 Преподаватели",
    "rooms": "🚪 Аудитории", "group": "🎓 Моя группа", "courses": "📚 Курсы",
    "education": "🎓 Образование", "sports": "🏅 Спортивные секции",
    "announcements": "📢 Объявления", "notifications": "🔔 Уведомления",
}
NOTIFICATION_LABELS = {
    "notifications_enabled": "🔔 Все уведомления",
    "schedule_changes": "📅 Изменения расписания",
    "cancellations": "❌ Отмена занятий",
    "transfers": "↪️ Переносы занятий",
    "teacher_changes": "👨‍🏫 Замены преподавателей",
    "sports_reminders": "🏅 Напоминания о секциях",
    "class_bells": "⏰ Начало пар и перемены",
}


async def show_settings(target, uid, page="home"):
    prefs = await get_user_settings(uid)
    if page == "menu":
        buttons = []
        for key, label in MENU_SETTING_LABELS.items():
            visible = key not in prefs["hidden_menu"]
            buttons.append([InlineKeyboardButton(text=f"{'✅' if visible else '▫️'} {label}", callback_data=f"settings|menu_toggle|{key}")])
        buttons += [[InlineKeyboardButton(text="↩️ Настройки", callback_data="settings|home")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")]]
        text = "⚙️ <b>Кнопки главного меню</b>\n\nНажимай на раздел, чтобы показать или скрыть его. Скрытые функции останутся доступны в настройках."
    elif page == "notifications":
        buttons = []
        for key, label in NOTIFICATION_LABELS.items():
            enabled = bool(prefs[key])
            buttons.append([InlineKeyboardButton(text=f"{'✅' if enabled else '▫️'} {label}", callback_data=f"settings|notify_toggle|{key}")])
        buttons += [[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")]]
        text = "🔔 <b>Уведомления</b>\n\nРасписание и напоминания приходят только по выбранной основной группе и секциям. Начало пар и перемены можно включить отдельно."
    else:
        buttons = [[InlineKeyboardButton(text="🧩 Кнопки главного меню", callback_data="settings|menu")],
                   [InlineKeyboardButton(text="🔔 Уведомления", callback_data="settings|notifications")],
                   [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")]]
        text = "⚙️ <b>Настройки Степаши</b>\n\nНастрой меню и уведомления под себя."
    await safe_edit(target, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("settings|"))
async def settings_action(c: CallbackQuery):
    _, action, *args = c.data.split("|")
    if action == "menu_toggle" and args and args[0] in MENU_SETTING_LABELS:
        key = args[0]
        prefs = await get_user_settings(c.from_user.id)
        hidden = set(prefs["hidden_menu"])
        hidden.remove(key) if key in hidden else hidden.add(key)
        await update_user_setting(c.from_user.id, "hidden_menu", hidden)
        await show_settings(c.message, c.from_user.id, "menu")
    elif action == "notify_toggle" and args and args[0] in NOTIFICATION_LABELS:
        key = args[0]
        prefs = await get_user_settings(c.from_user.id)
        await update_user_setting(c.from_user.id, key, not bool(prefs[key]))
        await show_settings(c.message, c.from_user.id, "notifications")
    elif action in ("home", "menu", "notifications"):
        await show_settings(c.message, c.from_user.id, action)
    else:
        await c.answer("Настройка не найдена.", show_alert=True)
        return
    await c.answer()

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
    if not user or user.get("college_id") != "kuzpk" or not is_courses_group(group_name):
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


async def show_sports_sections(target, uid):
    selected = await get_sport_subscriptions(uid)
    rows = [[InlineKeyboardButton(text=f"{'✅' if str(i) in selected else '▫️'} {section['name']}", callback_data=f"sports|{i}")]
            for i, section in enumerate(SPORTS_SECTIONS)]
    rows.append([InlineKeyboardButton(text="↩️ К расписанию", callback_data="menu|schedule")])
    await safe_edit(target,
        "🏅 <b>Спортивные секции · 2026–2027</b>\n\nВыбери секцию, чтобы посмотреть дни и время занятий:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("sports|"))
async def sports_section(c: CallbackQuery):
    bits = c.data.split("|")
    answer_text = None
    if len(bits) == 3 and bits[1] == "toggle" and bits[2].isdigit():
        index = int(bits[2])
        if index >= len(SPORTS_SECTIONS):
            await c.answer("Список секций обновился.", show_alert=True); return
        enabled = await toggle_sport_subscription(c.from_user.id, str(index))
        answer_text = "Напоминания о секции включены 🐾" if enabled else "Секция удалена из избранного."
        section = SPORTS_SECTIONS[index]
    else:
        raw_index = bits[1] if len(bits) > 1 else ""
        if not raw_index.isdigit() or int(raw_index) >= len(SPORTS_SECTIONS):
            await c.answer("Список секций обновился.", show_alert=True)
            return
        section = SPORTS_SECTIONS[int(raw_index)]
        index = int(raw_index)
    schedule = "\n".join(
        f"• <b>{SPORTS_DAYS[day]}</b> · {time}"
        for day, time in section["days"].items()
    )
    selected = await get_sport_subscriptions(c.from_user.id)
    rows = [[InlineKeyboardButton(text="✅ Напоминать мне" if str(index) not in selected else "🔕 Отключить напоминания", callback_data=f"sports|toggle|{index}")],
            [InlineKeyboardButton(text="↩️ Все секции", callback_data="menu|sports")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")]]
    await safe_edit(c.message,
        f"🏅 <b>{escape(section['name'])}</b>\n\n{schedule}\n\n📍 Место: спортивный комплекс\n👤 Ответственный: <b>{escape(section['coach'])}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer(answer_text)


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
    if not user or user.get("college_id") != "kuzpk" or not is_courses_group(group_name):
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

async def show_entity_picker(target, kind: str, page: int, edit: bool = False, college_id="kuzpk", college_branch=None):
    names = await get_entity_names(kind, college_id, college_branch)
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
    buttons = [[InlineKeyboardButton(text=name[:60], callback_data=f"entitystart|{kind}|{i}|{today:%Y%m%d}")
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
    if kind != "noop":
        user = await get_user(c.from_user.id)
        await show_entity_picker(c.message, kind, int(raw_page), edit=True, college_id=(user or {}).get("college_id", "kuzpk"), college_branch=(user or {}).get("college_branch"))
    await c.answer()

async def render_entity_schedule(target, kind: str, index: int, day: date, edit: bool = True, college_id="kuzpk", college_branch=None, nearest_if_empty=False):
    names = await get_entity_names(kind, college_id, college_branch)
    if index < 0 or index >= len(names):
        await safe_edit(target, "Список изменился. Выбери запись ещё раз.", reply_markup=back_button())
        return
    name = names[index]
    rows = await get_entity_schedule(kind, name, day, college_id, college_branch)
    if nearest_if_empty and not rows and college_id in ("pgt", "kuztagis"):
        available = await get_entity_schedule(kind, name, None, college_id, college_branch)
        dates = sorted({date.fromisoformat(str(row["date"])) for row in available if row.get("date")})
        if dates:
            upcoming = [schedule_day for schedule_day in dates if schedule_day >= day]
            day = upcoming[0] if upcoming else dates[-1]
            rows = [row for row in available if row.get("date") == day.isoformat()]
    text = format_schedule(name, day, rows, entity_type=kind)
    picker_page = index // 8
    nav = [InlineKeyboardButton(text="‹ День", callback_data=f"entity|{kind}|{index}|{(day-timedelta(days=1)):%Y%m%d}"),
           InlineKeyboardButton(text="Сегодня", callback_data=f"entity|{kind}|{index}|{datetime.now(TZ).date():%Y%m%d}"),
           InlineKeyboardButton(text="День ›", callback_data=f"entity|{kind}|{index}|{(day+timedelta(days=1)):%Y%m%d}")]
    return_callback = f"building|{room_building(name)}" if kind == "room" and college_id == "kuzpk" else f"pick|{kind}|{picker_page}"
    return_label = "↩️ Корпус" if kind == "room" and college_id == "kuzpk" else "↩️ К списку"
    markup = InlineKeyboardMarkup(inline_keyboard=[nav,
        [InlineKeyboardButton(text=return_label, callback_data=return_callback), InlineKeyboardButton(text="🏠 Меню", callback_data="menu|home")]])
    if edit: await safe_edit(target, text, reply_markup=markup)
    else: await target.answer(text, reply_markup=markup)

@dp.callback_query(F.data.startswith("entity|"))
async def entity_schedule_navigation(c: CallbackQuery):
    _, kind, raw_index, raw_date = c.data.split("|")
    day = datetime.strptime(raw_date, "%Y%m%d").date()
    user = await get_user(c.from_user.id)
    await render_entity_schedule(c.message, kind, int(raw_index), day, college_id=(user or {}).get("college_id", "kuzpk"), college_branch=(user or {}).get("college_branch"))
    await c.answer()

@dp.callback_query(F.data.startswith("entitystart|"))
async def entity_schedule_start(c: CallbackQuery):
    _, kind, raw_index, raw_date = c.data.split("|")
    day = datetime.strptime(raw_date, "%Y%m%d").date()
    user = await get_user(c.from_user.id)
    await render_entity_schedule(c.message, kind, int(raw_index), day, college_id=(user or {}).get("college_id", "kuzpk"), college_branch=(user or {}).get("college_branch"), nearest_if_empty=True)
    await c.answer()

def building_title(building):
    titles = {1: "1 корпус · Главный", 2: "2 корпус · Мастерские",
              3: "3 корпус · Спортивный комплекс", 4: "4 корпус · Общежитие"}
    return titles.get(building, "Другие помещения")

async def show_building_picker(target, edit=True, college_id="kuzpk"):
    names = await get_entity_names("room", college_id)
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

async def show_rooms_in_building(target, building, page=0, college_id="kuzpk"):
    names = await get_entity_names("room", college_id)
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
    user = await get_user(c.from_user.id)
    college_id = (user or {}).get("college_id", "kuzpk")
    if value == "list": await show_building_picker(c.message, college_id=college_id)
    else: await show_rooms_in_building(c.message, int(value), college_id=college_id)
    await c.answer()

@dp.callback_query(F.data.startswith("roomlist|"))
async def room_list_page(c: CallbackQuery):
    _, building, page = c.data.split("|")
    user = await get_user(c.from_user.id)
    await show_rooms_in_building(c.message, int(building), int(page), (user or {}).get("college_id", "kuzpk"))
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
    elif action == "schedule_update":
        if c.from_user.id != ADMIN_ID:
            await c.answer("Недостаточно прав.", show_alert=True); return
        await state.clear()
        await state.set_state(Forms.schedule_update_waiting)
        await safe_edit(c.message, "🤖 <b>Обновление расписания</b>\n\nВыбери колледж, затем отправь Excel/PDF или фото расписания. Я сам определю, это расписание групп или звонков, покажу предпросмотр и попрошу подтверждение. Время звонков можно прислать списком в сообщении.", reply_markup=schedule_college_keyboard())
    elif action.startswith("college:"):
        if c.from_user.id != ADMIN_ID:
            await c.answer("Недостаточно прав.", show_alert=True); return
        college_id = action.split(":", 1)[1]
        if college_id not in COLLEGES:
            await c.answer("Неизвестный колледж.", show_alert=True); return
        await state.update_data(pending_college=college_id)
        await state.set_state(Forms.admin_announcement)
        await safe_edit(c.message, f"📢 <b>Объявление · {escape(COLLEGES[college_id])}</b>\n\nОтправь текст. Перед рассылкой покажу предпросмотр.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="flow|cancel")]]))
    elif action == "all":
        if c.from_user.id != ADMIN_ID:
            await c.answer("Недостаточно прав.", show_alert=True); return
        await state.update_data(pending_college=None)
        await state.set_state(Forms.admin_announcement)
        await safe_edit(c.message, "📢 <b>Объявление всем колледжам</b>\n\nОтправь текст. Перед рассылкой я покажу предпросмотр и попрошу подтверждение.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="flow|cancel")]]))
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
    schedule_update_waiting = State()
    schedule_update_confirm = State()


def schedule_college_keyboard():
    rows = [[InlineKeyboardButton(text=name, callback_data=f"scheduleimport|college|{key}")]
            for key, name in COLLEGES.items()]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="flow|cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bells_preview(rows, college_id, source):
    shifts = sorted({str(row.get("shift") or "Смена не указана") for row in rows})
    sections = list(dict.fromkeys(str(row.get("section") or "") for row in rows if row.get("section")))
    sample = "\n".join(
        f"• {escape(str(row.get('section') or ''))} · {escape(str(row.get('shift') or ''))}, {escape(str(row.get('lesson') or 'пара'))}: "
        f"{escape(str(row.get('first') or ''))}"
        + (f"; {escape(str(row.get('second')))}" if row.get("second") else "")
        for row in rows[:8])
    if len(rows) > 8:
        sample += f"\n… и ещё {len(rows) - 8} пар"
    text = (f"🔔 <b>Предпросмотр звонков · {escape(COLLEGES[college_id])}</b>\n\n"
            f"Файл: <code>{escape(source[:100])}</code>\n"
            f"Распознано временных слотов: <b>{len(rows)}</b> · смены: <b>{escape(', '.join(shifts))}</b>\n"
            f"Дни: {escape(', '.join(sections))}\n\n"
            f"{sample}\n\nПодтвердить замену расписания звонков для этого колледжа?")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Обновить звонки", callback_data="scheduleimport|confirm|yes"),
         InlineKeyboardButton(text="❌ Отменить", callback_data="scheduleimport|confirm|no")]])
    return text, keyboard


def bells_keyboard(day, full=False):
    keyboard = [
        [InlineKeyboardButton(text="‹ День", callback_data=f"bell|prev|{day:%Y%m%d}"),
         InlineKeyboardButton(text=f"{day:%d.%m}", callback_data="bell|noop|00000000"),
         InlineKeyboardButton(text="День ›", callback_data=f"bell|next|{day:%Y%m%d}")],
        [InlineKeyboardButton(text="📅 По расписанию", callback_data=f"bell|day|{day:%Y%m%d}" if full else "bell|full|" + day.strftime("%Y%m%d"))],
        [InlineKeyboardButton(text="↩️ К разделу расписания", callback_data="menu|schedule")],
    ]
    if not full:
        keyboard[1][0] = InlineKeyboardButton(text="📖 Показать полностью", callback_data=f"bell|full|{day:%Y%m%d}")
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _message_chunks(text, limit=3900):
    paragraphs = text.split("\n\n")
    chunks, current = [], ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > limit and current:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def show_bells_schedule(target, college_id, day, full=False, edit=False):
    rows = await get_bells(college_id)
    text = format_bells(rows, None if full else day)
    chunks = _message_chunks(text)
    markup = bells_keyboard(day, full)
    if edit:
        await safe_edit(target, chunks[0], reply_markup=markup)
    else:
        await target.answer(chunks[0], reply_markup=markup)
    for chunk in chunks[1:]:
        await target.answer(chunk)


@dp.callback_query(F.data.startswith("bell|"))
async def bells_navigation(c: CallbackQuery):
    _, action, raw_day = c.data.split("|")
    if action == "noop":
        await c.answer(); return
    user = await get_user(c.from_user.id)
    college_id = (user or {}).get("college_id", "kuzpk")
    day = datetime.strptime(raw_day, "%Y%m%d").date()
    if action == "prev":
        day -= timedelta(days=1)
        full = False
    elif action == "next":
        day += timedelta(days=1)
        full = False
    elif action == "full":
        full = True
    elif action == "day":
        full = False
    else:
        await c.answer("Кнопка устарела. Открой звонки ещё раз.", show_alert=True); return
    await show_bells_schedule(c.message, college_id, day, full=full, edit=True)
    await c.answer()


async def _show_bells_preview(message, state, rows, college_id, source):
    text, keyboard = _bells_preview(rows, college_id, source)
    await state.update_data(pending_schedule_kind="bells", pending_bells_rows=rows,
                            pending_schedule_college=college_id, pending_schedule_source=source)
    await state.set_state(Forms.schedule_update_confirm)
    await message.answer(text, reply_markup=keyboard)


async def _route_bells_preview(message, state, rows, source, hint=""):
    data = await state.get_data()
    college_id = data.get("schedule_input_college") or detect_college(f"{source} {hint}")
    if college_id not in COLLEGES:
        await state.update_data(pending_schedule_kind="bells", pending_bells_rows=rows,
                                pending_schedule_source=source)
        await state.set_state(Forms.schedule_update_waiting)
        await message.answer("🔔 Понял: это расписание звонков. Выбери колледж, для которого его обновить:", reply_markup=schedule_college_keyboard())
        return
    await _show_bells_preview(message, state, rows, college_id, source)


def _image_kind_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓 Занятия", callback_data="scheduleimage|lessons"),
         InlineKeyboardButton(text="🔔 Звонки", callback_data="scheduleimage|bells")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow|cancel")]])


async def _process_schedule_image(message, state, file_id, mime_type, source, hint="", expected_kind=""):
    await state.update_data(pending_image_file_id=file_id, pending_image_mime=mime_type,
                            pending_image_source=source, pending_image_hint=hint,
                            pending_image_kind=expected_kind)
    downloaded = await bot.download(file_id)
    result = await asyncio.to_thread(analyze_schedule_image, downloaded.getvalue(), mime_type,
                                     hint, expected_kind)
    kind, rows = result.get("kind"), result.get("rows") or []
    if kind == "unknown":
        await state.set_state(Forms.schedule_update_waiting)
        await message.answer(result.get("question") or "Уточни, что на фото:", reply_markup=_image_kind_keyboard())
        return
    if not rows:
        await state.set_state(Forms.schedule_update_waiting)
        await state.update_data(pending_image_kind=kind)
        await message.answer(result.get("question") or "Не смог уверенно разобрать фото. Напиши дату и группу или уточни, что именно нужно прочитать.")
        return
    await state.update_data(pending_image_file_id="", pending_image_kind="")
    data = await state.get_data()
    college_id = data.get("schedule_input_college") or detect_college(f"{source} {hint}")
    if kind == "bells":
        if college_id not in COLLEGES:
            await state.update_data(pending_schedule_kind="bells", pending_bells_rows=rows,
                                    pending_schedule_source=source)
            await state.set_state(Forms.schedule_update_waiting)
            await message.answer("🔔 Распознал расписание звонков. Выбери колледж для обновления:", reply_markup=schedule_college_keyboard())
            return
        await _show_bells_preview(message, state, rows, college_id, source)
        return
    if college_id not in COLLEGES:
        await state.update_data(pending_schedule_kind="lessons", pending_schedule_rows=rows,
                                pending_schedule_source=source, pending_schedule_summary="")
        await state.set_state(Forms.schedule_update_waiting)
        await message.answer("🗓 Распознал расписание занятий. Выбери колледж для обновления:", reply_markup=schedule_college_keyboard())
        return
    await _show_schedule_preview(message, state, rows, college_id, source)


@dp.callback_query(F.data.startswith("scheduleimage|"))
async def schedule_image_kind(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Недостаточно прав.", show_alert=True); return
    kind = c.data.rsplit("|", 1)[1]
    data = await state.get_data()
    file_id = data.get("pending_image_file_id")
    if kind not in {"lessons", "bells"} or not file_id:
        await c.answer("Фото устарело. Отправь его ещё раз.", show_alert=True); return
    await c.answer()
    try:
        hint = data.get("pending_image_hint", "")
        await _process_schedule_image(c.message, state, file_id, data.get("pending_image_mime", "image/jpeg"),
                                      data.get("pending_image_source", "Фото расписания"), hint, kind)
    except Exception as exc:
        logging.exception("Не удалось обработать фото после уточнения типа")
        await c.message.answer("Не получилось разобрать фото. Проверь модель распознавания изображений в Railway и отправь более чёткий снимок. " + escape(str(exc)))


def _schedule_preview(rows, college_id, source, summary="", replace_days=None):
    groups = sorted({str(row.get("group", "")).strip() for row in rows if row.get("group")})
    dates = sorted({str(row.get("date", "")).strip() for row in rows if row.get("date")})
    dates = sorted(set(dates) | {str(day) for _group, day in (replace_days or [])})
    date_span = dates[0] if len(dates) == 1 else (f"{dates[0]} — {dates[-1]}" if dates else "даты не найдены")
    group_preview = ", ".join(escape(name) for name in groups[:15])
    if len(groups) > 15:
        group_preview += f" и ещё {len(groups) - 15}"
    text = (f"📋 <b>Предпросмотр расписания · {escape(COLLEGES[college_id])}</b>\n\n"
            f"Файл: <code>{escape(source[:100])}</code>\n"
            f"Занятий: <b>{len(rows)}</b>\nДаты: <b>{escape(date_span)}</b>\n"
            f"Группы ({len(groups)}): {group_preview or 'не найдены'}")
    if summary:
        text += f"\n\n🤖 {escape(summary[:800])}"
    text += "\n\nПодтвердить замену расписания для этих групп и дат?"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Применить", callback_data="scheduleimport|confirm|yes"),
         InlineKeyboardButton(text="❌ Отменить", callback_data="scheduleimport|confirm|no")]])
    return text, keyboard


async def _show_schedule_preview(message, state, rows, college_id, source, summary="", branch="", replace_days=None):
    text, keyboard = _schedule_preview(rows, college_id, source, summary, replace_days)
    await state.update_data(pending_schedule_rows=rows, pending_schedule_college=college_id,
                            pending_schedule_source=source, pending_schedule_branch=branch,
                            pending_schedule_replace_days=replace_days or [], pending_schedule_kind="lessons",
                            pending_bells_rows=[])
    await state.set_state(Forms.schedule_update_confirm)
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("scheduleimport|college|"))
async def schedule_import_college(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Недостаточно прав.", show_alert=True); return
    college_id = c.data.rsplit("|", 1)[1]
    if college_id not in COLLEGES:
        await c.answer("Неизвестный колледж.", show_alert=True); return
    data = await state.get_data()
    await state.update_data(schedule_input_college=college_id)
    if data.get("pending_schedule_kind") == "bells" and data.get("pending_bells_rows"):
        await _show_bells_preview(c.message, state, data["pending_bells_rows"], college_id,
                                  data.get("pending_schedule_source", "Загруженный файл"))
        await c.answer(); return
    rows = data.get("pending_schedule_rows")
    if rows:
        await _show_schedule_preview(c.message, state, rows, college_id,
                                     data.get("pending_schedule_source", "Загруженный файл"),
                                     data.get("pending_schedule_summary", ""))
    else:
        await state.set_state(Forms.schedule_update_waiting)
        await c.message.answer(f"Выбран {escape(COLLEGES[college_id])}. Теперь отправь файл расписания или напиши правку с группой и датой.")
    await c.answer()


@dp.callback_query(F.data.startswith("scheduleimport|confirm|"))
async def schedule_import_confirm(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Недостаточно прав.", show_alert=True); return
    if c.data.endswith("|no"):
        await state.clear()
        await safe_edit(c.message, "Изменения отменены.", reply_markup=back_button("management"))
        await c.answer(); return
    data = await state.get_data()
    if data.get("pending_schedule_kind") == "bells":
        college_id = data.get("pending_schedule_college")
        rows = data.get("pending_bells_rows") or []
        if college_id not in COLLEGES or not rows:
            await state.clear()
            await safe_edit(c.message, "Предпросмотр звонков устарел. Начни обновление ещё раз.", reply_markup=back_button("management"))
            await c.answer(); return
        try:
            await save_bells_update(college_id, rows, data.get("pending_schedule_source", ""), c.from_user.id)
        except Exception as exc:
            logging.exception("Не удалось сохранить расписание звонков")
            await c.answer("Не удалось сохранить звонки.", show_alert=True)
            await c.message.answer(escape(str(exc)))
            return
        await state.clear()
        await safe_edit(c.message, f"✅ Расписание звонков обновлено ({len(rows)} пар) · {escape(COLLEGES[college_id])}.", reply_markup=back_button("management"))
        await c.answer("Готово"); return
    rows = data.get("pending_schedule_rows") or []
    college_id = data.get("pending_schedule_college")
    if (not rows and not data.get("pending_schedule_replace_days")) or college_id not in COLLEGES:
        await state.clear()
        await safe_edit(c.message, "Предпросмотр устарел. Начни обновление ещё раз.", reply_markup=back_button("management"))
        await c.answer(); return
    try:
        branch = data.get("pending_schedule_branch", "")
        affected = {(str(row.get("group") or "").strip(), str(row.get("date") or "").strip())
                    for row in rows if row.get("group") and row.get("date")}
        affected.update((str(group).strip(), str(day).strip()) for group, day in data.get("pending_schedule_replace_days", []) if group and day)
        old_schedule = {}
        for group, raw_day in affected:
            try:
                old_schedule[(group, raw_day)] = await get_schedule(group, date.fromisoformat(raw_day), college_id, branch or None)
            except ValueError:
                continue
        count = await save_schedule_update(college_id, rows, data.get("pending_schedule_source", ""),
                                          c.from_user.id, branch,
                                          data.get("pending_schedule_replace_days", []))
        new_schedule = {key: [] for key in affected}
        for row in rows:
            key = (str(row.get("group") or "").strip(), str(row.get("date") or "").strip())
            if key in new_schedule:
                new_schedule[key].append(row)
        for (group, raw_day), previous in old_schedule.items():
            updated = new_schedule.get((group, raw_day), [])
            if _schedule_signature(previous) != _schedule_signature(updated):
                await _send_schedule_change(group, raw_day, previous, updated, college_id)
    except Exception as exc:
        logging.exception("Не удалось сохранить обновление расписания")
        await c.answer("Не удалось сохранить расписание.", show_alert=True)
        await c.message.answer(escape(str(exc)))
        return
    await state.clear()
    await safe_edit(c.message, f"✅ Расписание обновлено. Заменено {len(rows)} занятий в {count} днях для {escape(COLLEGES[college_id])}.", reply_markup=back_button("management"))
    await c.answer("Готово")


@dp.message(F.document)
async def admin_schedule_file(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    filename = m.document.file_name or "расписание.xlsx"
    if m.document.file_size and m.document.file_size > 12 * 1024 * 1024:
        await m.answer("Файл больше 12 МБ. Отправь уменьшенную версию."); return
    try:
        downloaded = await bot.download(m.document.file_id)
        content = downloaded.getvalue()
        image_mimes = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                       ".webp": "image/webp", ".gif": "image/gif"}
        image_mime = (m.document.mime_type or "").casefold()
        if not image_mime.startswith("image/"):
            image_mime = image_mimes.get(os.path.splitext(filename)[1].casefold(), "")
        if image_mime.startswith("image/"):
            await _process_schedule_image(m, state, m.document.file_id, image_mime,
                                          filename, m.caption or "")
            return
        data = await state.get_data()
        college_id = data.get("schedule_input_college") or detect_college(f"{filename} {m.caption or ''}")

        # The bell parser now requires a clear “звонки” clue in the filename,
        # caption, or workbook text, so it cannot mistake numeric lesson cells
        # for times.
        bell_rows = await asyncio.to_thread(parse_bells_file, content, filename, m.caption or "")
        if bell_rows:
            if college_id not in COLLEGES:
                await state.update_data(pending_schedule_kind="bells", pending_bells_rows=bell_rows,
                                        pending_schedule_source=filename)
                await state.set_state(Forms.schedule_update_waiting)
                await m.answer("🔔 Понял: это расписание звонков. Выбери колледж, для которого его обновить:", reply_markup=schedule_college_keyboard())
                return
            await _show_bells_preview(m, state, bell_rows, college_id, filename)
            return
        rows = await asyncio.to_thread(parse_schedule_file, content, filename)
        caption = m.caption or ""
        summary = ""
        if any(word in caption.casefold() for word in ("замени", "исправь", "добавь", "убери", "отмени", "перенеси")):
            rows, summary = await asyncio.to_thread(apply_file_instruction, rows, caption, "расписание колледжа")
    except Exception as exc:
        logging.exception("Не удалось обработать файл расписания")
        hint = f"{filename} {m.caption or ''}".casefold()
        if "звон" in hint:
            await m.answer("🔔 Понял, что это новое расписание звонков, но не смог извлечь пары и времена. Пришли Excel/PDF с номером пары и временем начала/окончания; если таблица необычная, добавь в подпись «это расписание звонков». Ошибка: " + escape(str(exc)))
            return
        await m.answer(f"Не получилось разобрать файл: {escape(str(exc))}"); return
    data = await state.get_data()
    college_id = data.get("schedule_input_college") or detect_college(f"{filename} {m.caption or ''}")
    if college_id not in COLLEGES:
        await state.update_data(pending_schedule_rows=rows, pending_schedule_source=filename,
                                pending_schedule_summary=summary)
        await state.set_state(Forms.schedule_update_waiting)
        await m.answer("Файл разобран. Выбери колледж, к которому относится расписание:", reply_markup=schedule_college_keyboard())
        return
    await _show_schedule_preview(m, state, rows, college_id, filename, summary)


@dp.message(F.photo)
async def admin_schedule_photo(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        await _process_schedule_image(m, state, m.photo[-1].file_id, "image/jpeg",
                                      "Фото расписания", m.caption or "")
    except Exception as exc:
        logging.exception("Не удалось определить и прочитать расписание с фото")
        await m.answer("Не получилось определить расписание на фото. Проверь модель распознавания изображений в Railway и пришли снимок целиком, без бликов и размытия. " + escape(str(exc)))


@dp.message(Forms.schedule_update_waiting, F.text)
async def admin_schedule_text(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    if data.get("pending_image_file_id"):
        text_kind = "bells" if "звон" in m.text.casefold() else (
            "lessons" if any(word in m.text.casefold() for word in ("занят", "групп", "предмет", "пары")) else "")
        try:
            await _process_schedule_image(m, state, data["pending_image_file_id"],
                                          data.get("pending_image_mime", "image/jpeg"),
                                          data.get("pending_image_source", "Фото расписания"),
                                          f"{data.get('pending_image_hint', '')}\nУточнение администратора: {m.text}",
                                          data.get("pending_image_kind") or text_kind)
        except Exception as exc:
            logging.exception("Не удалось обработать фото после текстового уточнения")
            await m.answer("Не получилось разобрать фото с учётом уточнения. Проверь модель распознавания изображений в Railway или отправь более чёткий снимок. " + escape(str(exc)))
        return
    college_id = data.get("schedule_input_college")
    if college_id not in COLLEGES:
        await m.answer("Сначала выбери колледж:", reply_markup=schedule_college_keyboard()); return
    try:
        bell_rows = await asyncio.to_thread(parse_bells_text, m.text)
        if bell_rows:
            await _show_bells_preview(m, state, bell_rows, college_id, "Расписание звонков из сообщения")
            return
        if "звон" in m.text.casefold():
            await m.answer("Похоже, ты про звонки. Пришли файл Excel/PDF или напиши пары построчно, например: «1 пара — 08:30–09:15», «2 пара — 09:25–10:10». Я покажу предпросмотр перед сохранением.")
            return
        today = datetime.now(TZ).date()
        target = await asyncio.to_thread(identify_text_target, m.text, COLLEGES[college_id], today.isoformat())
        if target.get("question"):
            await m.answer(escape(target["question"])); return
        group, target_date = target["group"], date.fromisoformat(target["date"])
        known_groups = await list_groups(college_id)
        if known_groups and group.casefold() not in {str(item).casefold() for item in known_groups}:
            await m.answer(f"Не нашёл группу «{escape(group)}» в {escape(COLLEGES[college_id])}. Проверь её написание или сначала загрузи расписание этой группы.")
            return
        branch = None
        if college_id == "yamk":
            from yamk import get_groups as yamk_groups
            for candidate in ("osgp", "oenp", "otp", "osep"):
                branch_groups = await asyncio.to_thread(yamk_groups, candidate)
                if any(str(item.get("name", "")).casefold() == group.casefold() for item in branch_groups):
                    branch = candidate; break
        current = await get_schedule(group, target_date, college_id, branch)
        if not current:
            await m.answer("Для этой группы и даты пока нет расписания. Сначала отправь файл с расписанием, затем пришли текстовую правку.")
            return
        result = await asyncio.to_thread(interpret_text_instruction, m.text, COLLEGES[college_id], today.isoformat(), current)
        if result.get("question"):
            await m.answer(escape(result["question"])); return
        if result["group"].casefold() != group.casefold() or result["date"] != target_date.isoformat():
            await m.answer("ИИ определил другую группу или дату. Изменения не применены; напиши группу и дату точнее."); return
        for row in result["rows"]:
            row["group"] = group
            row["date"] = target_date.isoformat()
        await _show_schedule_preview(m, state, result["rows"], college_id,
                                     f"Правка от {m.from_user.full_name}", result.get("summary", ""), branch or "",
                                     [(group, target_date.isoformat())])
    except Exception as exc:
        logging.exception("Не удалось подготовить правку расписания")
        await m.answer(f"Не удалось подготовить изменение: {escape(str(exc))}")

async def group_keyboard(prefix=None, page=0, college_id="kuzpk", view_mode=False):
    groups = await list_groups(college_id)
    if not groups:
        return None
    prefixes = sorted({str(g).split("-", 1)[0] for g in groups})
    if prefix is None:
        prefix_action = "specview" if view_mode else "spec"
        rows = [[InlineKeyboardButton(text=f"🎓 {p} · {sum(str(g).startswith(p+'-') for g in groups)} групп", callback_data=f"{prefix_action}|{p}|0")]
                for p in prefixes]
        if view_mode:
            rows.append([InlineKeyboardButton(text="↩️ К расписанию", callback_data="menu|schedule")])
            return InlineKeyboardMarkup(inline_keyboard=rows)
        rows.extend([[InlineKeyboardButton(text="🏫 Сменить колледж", callback_data="menu|college")],
                     [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu|home")]])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    selected = [g for g in groups if str(g).startswith(prefix + "-")]
    size = 12
    pages = max(1, (len(selected) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    chunk = selected[page*size:(page+1)*size]
    rows = [[InlineKeyboardButton(text=g, callback_data=f"groupview:{g}" if view_mode else f"group:{g}")] for g in chunk]
    nav = []
    action = "specview" if view_mode else "spec"
    if page: nav.append(InlineKeyboardButton(text="‹", callback_data=f"{action}|{prefix}|{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data=f"{action}|noop|0"))
    if page + 1 < pages: nav.append(InlineKeyboardButton(text="›", callback_data=f"{action}|{prefix}|{page+1}"))
    rows.extend([nav, [InlineKeyboardButton(text="↩️ Специальности", callback_data=f"{action}|list|0")]])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data.startswith("spec|"))
async def specialty_groups(c: CallbackQuery):
    _, prefix, page = c.data.split("|")
    if prefix == "noop":
        await c.answer(); return
    user = await get_user(c.from_user.id)
    if user and user.get("college_id") == "yamk":
        await safe_edit(c.message,
            "🏫 <b>ЯМК · Салехард</b>\n\nЭтот список групп устарел. Выбери отделение, чтобы открыть актуальные группы:",
            reply_markup=yamk_menu_keyboard())
        await c.answer("Открыл актуальный выбор отделения ЯМК.")
        return
    if prefix == "list":
        markup = await group_keyboard(college_id=(user or {}).get("college_id", "kuzpk"))
        text = "🎓 <b>Выбор учебной группы</b>\nСначала выбери специальность:"
    else:
        college_id = (user or {}).get("college_id", "kuzpk")
        markup = await group_keyboard(prefix, int(page), college_id)
        count = sum(str(g).startswith(prefix + "-") for g in await list_groups(college_id))
        text = f"🎓 <b>Специальность {escape(prefix)}</b> · {count} групп\nВыбери группу:"
    await safe_edit(c.message, text, reply_markup=markup or back_button())
    await c.answer()


@dp.callback_query(F.data.startswith("specview|"))
async def specialty_groups_view(c: CallbackQuery):
    _, prefix, raw_page = c.data.split("|")
    if prefix == "noop":
        await c.answer(); return
    user = await get_user(c.from_user.id)
    college_id = (user or {}).get("college_id", "kuzpk")
    if prefix == "list":
        markup = await group_keyboard(college_id=college_id, view_mode=True)
        text = "👀 <b>Временный просмотр</b>\nВыбери специальность:"
    else:
        markup = await group_keyboard(prefix, int(raw_page), college_id, view_mode=True)
        text = f"👀 <b>Специальность {escape(prefix)}</b>\nВыбери группу для временного просмотра:"
    await safe_edit(c.message, text, reply_markup=markup or back_button("schedule"))
    await c.answer()


@dp.callback_query(F.data.startswith("groupview:"))
async def view_other_group(c: CallbackQuery):
    group = c.data.split(":", 1)[1]
    user = await get_user(c.from_user.id)
    college_id = (user or {}).get("college_id", "kuzpk")
    if group not in await list_groups(college_id):
        await c.answer("Эта группа больше не найдена.", show_alert=True); return
    await render_schedule(c.message, c.from_user.id, datetime.now(TZ).date(), edit=True, group_override=group, temporary=True)
    await c.answer("Открыт временный просмотр. Основная группа не менялась.")

@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    if not await is_subscribed(m.from_user.id):
        await remove_old_keyboard(m)
        await show_subscription_prompt(m)
        return
    user = await get_user(m.from_user.id)
    role = "admin" if m.from_user.id == ADMIN_ID else (user["role"] if user else "student")
    await add_user(m.from_user.id, m.from_user.full_name, role)
    user = await get_user(m.from_user.id)
    if user.get("college_id") == "yamk" and user.get("college_selected"):
        await remove_old_keyboard(m)
        await m.answer("🏫 <b>ЯМК · Салехард</b>\n\nВыбери отделение или преподавателя:", reply_markup=yamk_menu_keyboard())
        return
    if not user["group_name"]:
        if user.get("college_selected"):
            markup = await group_keyboard(college_id=user.get("college_id", "kuzpk"))
            await remove_old_keyboard(m)
            await m.answer(f"🎓 <b>{college_label(user.get('college_id', 'kuzpk'))}</b>\nВыбери специальность, затем свою группу:", reply_markup=markup or back_button())
        else:
            await remove_old_keyboard(m)
            await m.answer("👋 Привет! Сначала выбери город:", reply_markup=city_keyboard())
        return
    await remove_old_keyboard(m)
    college_id = user.get("college_id", "kuzpk")
    await m.answer(f"🎓 <b>Расписание · {college_label(college_id)}</b>\nТвоя группа: <b>{escape(str(user['group_name']))}</b>\n\nВыбери раздел ниже:", reply_markup=main_menu(role, user["group_name"], college_id, (await get_user_settings(m.from_user.id))["hidden_menu"]))


@dp.callback_query(F.data == "subscription|check")
async def check_subscription(c: CallbackQuery):
    if await is_subscribed(c.from_user.id):
        await c.answer("Подписка подтверждена ✅")
        await c.message.answer("✅ Отлично, подписка подтверждена! Теперь нажми /start, чтобы открыть меню.")
    else:
        await c.answer("Подписка не найдена. Подпишись на канал и проверь ещё раз.", show_alert=True)


@dp.callback_query(F.data.startswith("city|"))
async def choose_city(c: CallbackQuery):
    city = c.data.split("|", 1)[1]
    if city not in ("kuzbass", "yanao", "kemerovo", "prokopyevsk"):
        await c.answer("Неизвестный город или регион.", show_alert=True)
        return
    labels = {"kuzbass": "Кузбасс", "yanao": "ЯНАО · Салехард", "kemerovo": "Кемерово", "prokopyevsk": "Прокопьевск"}
    city_key = {
        "kemerovo": "Кемерово",
        "prokopyevsk": "Прокопьевск",
        "yanao": "Салехард",
    }.get(city, city)
    prompt = "Выбери город:" if city == "kuzbass" else "Выбери учебное заведение:"
    await safe_edit(c.message, f"🗺 <b>{labels[city]}</b>\n\n{prompt}", reply_markup=city_college_keyboard(city_key))
    await c.answer()


@dp.callback_query(F.data.startswith("college|"))
async def choose_college(c: CallbackQuery):
    college_id = c.data.split("|", 1)[1]
    if college_id not in COLLEGES:
        await c.answer("Неизвестный колледж.", show_alert=True)
        return
    user = await get_user(c.from_user.id)
    role = "admin" if c.from_user.id == ADMIN_ID else (user["role"] if user else "student")
    await add_user(c.from_user.id, c.from_user.full_name, role)
    await set_college(c.from_user.id, college_id)
    await c.answer()
    if college_id == "yamk":
        await safe_edit(c.message, "🏫 <b>ЯМК · Салехард</b>\n\nВыбери отделение или преподавателя:", reply_markup=yamk_menu_keyboard())
        return
    groups = await list_groups(college_id)
    if not groups:
        await safe_edit(c.message,
            f"🏫 <b>{college_label(college_id)}</b>\n\nНе удалось загрузить группы. Проверь, что источник расписания доступен.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Выбрать город", callback_data="menu|city")]]))
        return
    markup = await group_keyboard(college_id=college_id)
    await safe_edit(c.message,
        f"🏫 <b>{college_label(college_id)}</b>\n\nВыбери специальность, затем свою группу:",
        reply_markup=markup)


def yamk_specialties(groups):
    return sorted({item.get("specialty") or "Специальность не указана" for item in groups})


async def _yamk_groups_with_import(branch):
    from yamk import get_groups
    try:
        groups = await asyncio.to_thread(get_groups, branch)
    except Exception:
        groups = []
    known = {str(item.get("name", "")).casefold() for item in groups}
    imported = await schedule_override_groups("yamk", branch)
    groups.extend({"name": name, "specialty": "Загруженное расписание"}
                  for name in imported if name.casefold() not in known)
    return groups


async def show_yamk_specialties(target, branch="otp"):
    from yamk import DEPARTMENTS
    try:
        groups = await _yamk_groups_with_import(branch)
    except Exception as error:
        logging.exception("Не удалось загрузить группы ЯМК (%s)", branch)
        message = ("Не удалось загрузить расписание с сайта ЯМК. Попробуй ещё раз позже "
                   "или открой официальный сайт колледжа.")
        await safe_edit(target,
            f"📚 <b>{DEPARTMENTS.get(branch, ('ЯМК', ''))[0]} · ЯМК</b>\n\n{escape(message)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌐 Открыть расписание ЯМК", url="https://ymk.yanao.ru/activity/38207/")],
                [InlineKeyboardButton(text="↩️ Отделения ЯМК", callback_data="menu|home")],
            ]))
        return
    if not groups:
        await safe_edit(target,
            f"📚 <b>{DEPARTMENTS.get(branch, ('ЯМК', ''))[0]} · группы</b>\n\nНа сайте ЯМК сейчас не удалось загрузить список групп.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌐 Открыть расписание ЯМК", url="https://ymk.yanao.ru/activity/38207/")],
                [InlineKeyboardButton(text="↩️ Отделения ЯМК", callback_data="menu|home")],
            ]))
        return
    specialties = yamk_specialties(groups)
    rows = [[InlineKeyboardButton(
        text=f"🎓 {specialty[:58]}",
        callback_data=f"yamk|specialty|{branch}|{index}")]
        for index, specialty in enumerate(specialties)]
    rows += [
        [InlineKeyboardButton(text="↩️ Отделения ЯМК", callback_data="menu|home")],
        [InlineKeyboardButton(text="🌐 Сайт ЯМК", url="https://ymk.yanao.ru/activity/38207/")],
    ]
    await safe_edit(target,
        f"📚 <b>{DEPARTMENTS.get(branch, ('ЯМК', ''))[0]} · специальности</b>\n\n"
        f"Сначала выбери специальность · всего {len(specialties)}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def show_yamk_groups(target, branch="otp", specialty_index=0, page=0):
    from yamk import DEPARTMENTS
    try:
        groups = await _yamk_groups_with_import(branch)
    except Exception:
        await show_yamk_specialties(target, branch)
        return
    specialties = yamk_specialties(groups)
    if not specialties:
        await show_yamk_specialties(target, branch)
        return
    specialty_index = max(0, min(specialty_index, len(specialties) - 1))
    specialty = specialties[specialty_index]
    groups = [item for item in groups if (item.get("specialty") or "Специальность не указана") == specialty]
    size = 12
    pages = (len(groups) + size - 1) // size
    page = max(0, min(page, pages - 1))
    rows = [[InlineKeyboardButton(text=item["name"], callback_data=f"yamk|group|{branch}|{specialty_index}|{item['name']}")]
            for item in groups[page * size:(page + 1) * size]]
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"yamk|groups|{branch}|{specialty_index}|{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="yamk|noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"yamk|groups|{branch}|{specialty_index}|{page+1}"))
    rows.extend([nav,
        [InlineKeyboardButton(text="↩️ Специальности", callback_data=f"yamk|department|{branch}")],
        [InlineKeyboardButton(text="🌐 Сайт ЯМК", url="https://ymk.yanao.ru/activity/38207/")],
    ])
    await safe_edit(target,
        f"📚 <b>{DEPARTMENTS.get(branch, ('ЯМК', ''))[0]} · {escape(specialty)}</b>\n\n"
        f"Выбери группу · всего {len(groups)}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


YAMK_DOCUMENT_CATEGORIES = {
    "schedules": "📅 Расписания отделений",
    "debts": "📌 Задолженности",
    "sessions": "🗓 Сессии",
    "attestation": "📝 Аттестация и экзамены",
    "extra": "🎨 Дополнительное образование",
    "other": "📎 Другие документы",
}


async def show_yamk_documents(target, category=None):
    from yamk import get_public_documents
    try:
        documents = await asyncio.to_thread(get_public_documents)
    except Exception:
        logging.exception("Не удалось загрузить документы с сайта ЯМК")
        await safe_edit(target,
            "📄 <b>Документы ЯМК</b>\n\nНе удалось загрузить список документов. Попробуй позже или открой страницу ЯМК:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌐 Открыть расписания и документы", url="https://ymk.yanao.ru/activity/38207/")],
                [InlineKeyboardButton(text="↩️ Назад к ЯМК", callback_data="menu|home")],
            ]))
        return

    if category is None:
        rows = []
        for key, label in YAMK_DOCUMENT_CATEGORIES.items():
            count = sum(item["category"] == key for item in documents)
            if count:
                rows.append([InlineKeyboardButton(text=f"{label} · {count}", callback_data=f"yamk|docs|{key}")])
        rows += [
            [InlineKeyboardButton(text="🌐 Открыть источник ЯМК", url="https://ymk.yanao.ru/activity/38207/")],
            [InlineKeyboardButton(text="↩️ Назад к ЯМК", callback_data="menu|home")],
        ]
        await safe_edit(target,
            "📄 <b>Расписания и документы ЯМК</b>\n\nВыбери, что открыть:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return

    selected = [(index, item) for index, item in enumerate(documents) if item["category"] == category]
    if not selected:
        await safe_edit(target,
            f"📄 <b>{YAMK_DOCUMENT_CATEGORIES.get(category, 'Документы ЯМК')}</b>\n\nНа сайте пока нет документов в этом разделе.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Разделы документов", callback_data="yamk|docs")],
                [InlineKeyboardButton(text="↩️ Назад к ЯМК", callback_data="menu|home")],
            ]))
        return

    rows = []
    details = []
    for index, item in selected:
        title = item["title"]
        date_label = item.get("date", "")
        if date_label:
            try:
                date_label = datetime.strptime(date_label, "%Y-%m-%d").strftime("%d.%m.%Y")
            except ValueError:
                pass
        details.append(f"<b>{escape(date_label)}</b>\n{escape(title)}" if date_label else escape(title))
        short_title = title if len(title) <= 42 else title[:39].rstrip() + "…"
        rows.append([InlineKeyboardButton(text=f"📎 Скачать · {short_title}", callback_data=f"yamk|doc|{index}")])
        rows.append([InlineKeyboardButton(text="🌐 Источник на сайте ЯМК", url=item["source_url"])])
    rows += [
        [InlineKeyboardButton(text="↩️ Разделы документов", callback_data="yamk|docs")],
        [InlineKeyboardButton(text="↩️ Назад к ЯМК", callback_data="menu|home")],
    ]
    await safe_edit(target,
        f"📄 <b>{YAMK_DOCUMENT_CATEGORIES.get(category, 'Документы ЯМК')}</b>\n\n" + "\n\n".join(details),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("yamk|"))
async def yamk_action(c: CallbackQuery):
    parts = c.data.split("|")
    action = parts[1] if len(parts) > 1 else ""
    if action == "noop":
        await c.answer()
        return
    if action == "docs":
        category = parts[2] if len(parts) > 2 else None
        if category is not None and category not in YAMK_DOCUMENT_CATEGORIES:
            await c.answer("Такой раздел документов не найден.", show_alert=True)
            return
        await show_yamk_documents(c.message, category)
        await c.answer()
        return
    if action == "doc":
        try:
            index = int(parts[2])
            from yamk import download_public_document
            await c.answer("Загружаю PDF…")
            document, data, filename = await asyncio.to_thread(download_public_document, index)
            date_label = document.get("date", "")
            if date_label:
                try:
                    date_label = datetime.strptime(date_label, "%Y-%m-%d").strftime("%d.%m.%Y")
                except ValueError:
                    pass
            caption = f"📄 <b>{escape(document['title'])}</b>"
            if date_label:
                caption += f"\nДата документа: {escape(date_label)}"
            caption += f"\nИсточник: <a href=\"{escape(document['source_url'], quote=True)}\">страница ЯМК</a>"
            await c.message.answer_document(BufferedInputFile(data, filename=filename), caption=caption)
        except Exception:
            logging.exception("Не удалось отправить документ ЯМК")
            try:
                from yamk import get_public_documents
                documents = await asyncio.to_thread(get_public_documents)
                document = documents[index]
                await c.message.answer(
                    f"Не получилось прикрепить PDF файлом. Открой его на сайте ЯМК:\n"
                    f"<a href=\"{escape(document['file_url'], quote=True)}\">📄 Скачать PDF</a> · "
                    f"<a href=\"{escape(document['source_url'], quote=True)}\">источник</a>"
                )
            except Exception:
                logging.exception("Не удалось сформировать ссылку на документ ЯМК")
                await c.message.answer("Не удалось загрузить PDF с сайта ЯМК. Вернись к списку документов и открой источник.")
        return
    if action == "department":
        branch = parts[2] if len(parts) > 2 else ""
        from yamk import DEPARTMENTS
        if branch not in DEPARTMENTS:
            await c.answer("Такое отделение не найдено.", show_alert=True)
            return
        await show_yamk_specialties(c.message, branch)
        await c.answer()
        return
    if action == "specialty":
        branch = parts[2] if len(parts) > 2 else "otp"
        try:
            specialty_index = int(parts[3]) if len(parts) > 3 else 0
        except ValueError:
            specialty_index = 0
        await show_yamk_groups(c.message, branch, specialty_index)
        await c.answer()
        return
    if action == "groups":
        try:
            branch = parts[2] if len(parts) > 2 else "otp"
            specialty_index = int(parts[3]) if len(parts) > 3 else 0
            page = int(parts[4]) if len(parts) > 4 else 0
        except ValueError:
            branch = "otp"
            specialty_index = 0
            page = 0
        await show_yamk_groups(c.message, branch, specialty_index, page)
        await c.answer()
        return
    if action == "group":
        branch = parts[2] if len(parts) > 4 else "otp"
        try:
            specialty_index = int(parts[3]) if len(parts) > 4 else 0
        except ValueError:
            specialty_index = 0
        group = parts[4] if len(parts) > 4 else (parts[2] if len(parts) > 2 else "")
        try:
            selected = next((item for item in await _yamk_groups_with_import(branch)
                             if item["name"] == group), None)
        except Exception:
            logging.exception("Не удалось проверить список групп ЯМК (%s)", branch)
            await show_yamk_groups(c.message, branch)
            await c.answer()
            return
        if not selected:
            await c.answer("Группа не найдена в актуальном списке ЯМК.", show_alert=True)
            return
        all_groups = await _yamk_groups_with_import(branch)
        specialties = yamk_specialties(all_groups)
        specialty_index = specialties.index(selected.get("specialty") or "Специальность не указана")
        user = await get_user(c.from_user.id)
        role = "admin" if c.from_user.id == ADMIN_ID else (user["role"] if user else "student")
        await add_user(c.from_user.id, c.from_user.full_name, role)
        await set_college(c.from_user.id, "yamk")
        await set_college_branch(c.from_user.id, branch)
        await set_group(c.from_user.id, group)
        await render_schedule(c.message, c.from_user.id, datetime.now(TZ).date(), edit=True)
        await c.answer()
        return
    if action == "teachers":
        user = await get_user(c.from_user.id)
        await show_entity_picker(c.message, "teacher", 0, edit=True, college_id="yamk", college_branch=(user or {}).get("college_branch"))
        await c.answer()
        return
    await c.answer("Действие не найдено.", show_alert=True)

@dp.callback_query(F.data.startswith("group:"))
async def choose_group(c: CallbackQuery):
    await c.answer()
    group = c.data.split(":", 1)[1]
    user = await get_user(c.from_user.id)
    college_id = (user or {}).get("college_id", "kuzpk")
    if college_id == "yamk":
        from yamk import DEPARTMENTS, get_groups as yamk_groups
        branch = None
        for candidate in DEPARTMENTS:
            try:
                items = await asyncio.to_thread(yamk_groups, candidate)
            except Exception:
                logging.exception("Не удалось проверить старую кнопку группы ЯМК (%s)", candidate)
                continue
            if any(item.get("name") == group for item in items):
                branch = candidate
                break
        if not branch:
            await safe_edit(c.message,
                "🏫 <b>ЯМК · Салехард</b>\n\nНе удалось определить отделение для этой старой кнопки. Выбери отделение заново:",
                reply_markup=yamk_menu_keyboard())
            return
        await set_college_branch(c.from_user.id, branch)
        await set_group(c.from_user.id, group)
        await render_schedule(c.message, c.from_user.id, datetime.now(TZ).date(), edit=True)
        return
    if group not in await list_groups(college_id):
        await c.message.answer("Этой группы нет в расписании.")
        return
    role = "admin" if c.from_user.id == ADMIN_ID else (user["role"] if user else "student")
    # A saved inline group button can outlive the user's database row (for
    # example after a service database reset). Recreate the user before saving.
    await add_user(c.from_user.id, c.from_user.full_name, role)
    await set_group(c.from_user.id, group)
    user = await get_user(c.from_user.id)
    await safe_edit(c.message, f"🎓 <b>Группа сохранена: {escape(group)}</b>\n\nВыбери раздел:", reply_markup=main_menu(role, user["group_name"], college_id, (await get_user_settings(c.from_user.id))["hidden_menu"]))

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

def temporary_schedule_keyboard(group_name, day, mode):
    if mode == "w":
        monday = day - timedelta(days=day.weekday())
        nav = [InlineKeyboardButton(text="‹ Неделя", callback_data=f"gsc|{group_name}|w|{(monday-timedelta(days=7)):%Y%m%d}|0"),
               InlineKeyboardButton(text=f"{monday:%d.%m}–{(monday+timedelta(days=6)):%d.%m}", callback_data="sc|noop|0|0"),
               InlineKeyboardButton(text="Неделя ›", callback_data=f"gsc|{group_name}|w|{(monday+timedelta(days=7)):%Y%m%d}|0")]
        anchor = monday
        toggle = InlineKeyboardButton(text="📅 По дням", callback_data=f"gsc|{group_name}|d|{datetime.now(TZ).date():%Y%m%d}|0")
    else:
        nav = [InlineKeyboardButton(text="‹ День", callback_data=f"gsc|{group_name}|d|{(day-timedelta(days=1)):%Y%m%d}|0"),
               InlineKeyboardButton(text="Сегодня", callback_data=f"gsc|{group_name}|d|{datetime.now(TZ).date():%Y%m%d}|0"),
               InlineKeyboardButton(text="День ›", callback_data=f"gsc|{group_name}|d|{(day+timedelta(days=1)):%Y%m%d}|0")]
        anchor = day - timedelta(days=day.weekday())
        toggle = InlineKeyboardButton(text="📆 Неделя", callback_data=f"gsc|{group_name}|w|{anchor:%Y%m%d}|0")
    return InlineKeyboardMarkup(inline_keyboard=[nav, [toggle],
        [InlineKeyboardButton(text="↩️ Выбор другой группы", callback_data="menu|browse_group"),
         InlineKeyboardButton(text="🎓 Моя группа", callback_data=f"gsc|return|d|{datetime.now(TZ).date():%Y%m%d}|0")]])


async def render_schedule(target, uid: int, day: date, mode: str = "d", page: int = 0, edit: bool = False, group_override=None, temporary=False):
    u = await get_user(uid)
    if not u or not u["group_name"]:
        text = "Сначала выбери группу командой /start."
        if edit: await safe_edit(target, text, reply_markup=None)
        else: await target.answer(text)
        return
    group_name = group_override or u["group_name"]
    if mode == "w":
        monday = day - timedelta(days=day.weekday())
        schedule = await get_week_schedule(group_name, monday, u.get("college_id", "kuzpk"), u.get("college_branch"))
        text = ("👀 <b>Временный просмотр</b>\n" if temporary else "") + f"🏫 <b>{college_label(u.get('college_id', 'kuzpk'))}</b>\n" + format_week_schedule(group_name, monday, schedule)
        if u.get("college_id") == "yamk" and not any(schedule.values()):
            published = await _yamk_published_dates(u)
            if published:
                text += f"\n\nℹ️ На сайте ЯМК сейчас доступно расписание за {published[0]:%d.%m}–{published[-1]:%d.%m.%Y}."
        markup = temporary_schedule_keyboard(group_name, monday, mode) if temporary else schedule_keyboard(monday, mode)
    else:
        rows = await get_schedule(group_name, day, u.get("college_id", "kuzpk"), u.get("college_branch"))
        text = ("👀 <b>Временный просмотр</b>\n" if temporary else "") + f"🏫 <b>{college_label(u.get('college_id', 'kuzpk'))}</b>\n" + format_schedule(group_name, day, rows)
        if u.get("college_id") == "yamk" and not rows:
            published = await _yamk_published_dates(u)
            if published and day.isoformat() not in {value.isoformat() for value in published}:
                text += f"\n\nℹ️ На сайте ЯМК сейчас доступно расписание за {published[0]:%d.%m}–{published[-1]:%d.%m.%Y}."
        markup = temporary_schedule_keyboard(group_name, day, mode) if temporary else schedule_keyboard(day, mode)
    if edit: await safe_edit(target, text, reply_markup=markup)
    else: await target.answer(text, reply_markup=markup)


async def _yamk_published_dates(user):
    try:
        from yamk import get_group_schedule
        rows = await asyncio.to_thread(
            get_group_schedule, user["group_name"], user.get("college_branch") or "otp"
        )
        values = sorted({date.fromisoformat(row["date"]) for row in rows if row.get("date")})
        return values
    except Exception:
        logging.exception("Не удалось определить даты опубликованного расписания ЯМК")
        return []

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
        user = await get_user(c.from_user.id)
        college_id = (user or {}).get("college_id", "kuzpk")
        await safe_edit(c.message, f"📅 <b>Расписание · {college_label(college_id)}</b>\nВыбери, что посмотреть:", reply_markup=schedule_menu_keyboard(college_id, (await get_user_settings(c.from_user.id))["hidden_menu"]))
        await c.answer(); return
    if mode == "x":
        await c.message.delete(); await c.answer(); return
    day = datetime.strptime(raw_day, "%Y%m%d").date()
    await render_schedule(c.message, c.from_user.id, day, mode, int(raw_page), edit=True)
    await c.answer()


@dp.callback_query(F.data.startswith("gsc|"))
async def temporary_schedule_navigation(c: CallbackQuery):
    _, group, mode, raw_day, _raw_page = c.data.split("|")
    day = datetime.strptime(raw_day, "%Y%m%d").date()
    user = await get_user(c.from_user.id)
    if not user:
        await c.answer("Нажми /start, чтобы открыть профиль.", show_alert=True); return
    if group == "return":
        await render_schedule(c.message, c.from_user.id, day, edit=True)
    elif group not in await list_groups(user.get("college_id", "kuzpk")):
        await c.answer("Группа больше не найдена.", show_alert=True); return
    else:
        await render_schedule(c.message, c.from_user.id, day, mode, edit=True, group_override=group, temporary=True)
    await c.answer()

@dp.message(F.text == "🔔 Звонки")
async def bells(m: Message):
    user = await get_user(m.from_user.id)
    await show_bells_schedule(m, (user or {}).get("college_id", "kuzpk"), datetime.now(TZ).date())

@dp.message(F.text == "👨‍🏫 Преподаватели")
async def teachers(m: Message):
    user = await get_user(m.from_user.id)
    rows = await get_teachers((user or {}).get("college_id", "kuzpk"))
    await m.answer("👨‍🏫 <b>Преподаватели</b>\n\n" + ("\n".join(sorted(set(r.get("teacher","") for r in rows if r.get("teacher")))) or "Справочник пока пуст."))

@dp.message(F.text == "🚪 Аудитории")
async def rooms(m: Message):
    user = await get_user(m.from_user.id)
    rows = await get_rooms((user or {}).get("college_id", "kuzpk"))
    await m.answer("🚪 <b>Аудитории</b>\n\n" + ("\n".join(sorted(set(r.get("room","") for r in rows if r.get("room")))) or "Справочник пока пуст."))

@dp.message(F.text == "⚙️ Моя группа")
async def change_group(m: Message):
    user = await get_user(m.from_user.id)
    college_id = (user or {}).get("college_id", "kuzpk")
    kb = await group_keyboard(college_id=college_id)
    if kb: await m.answer("Выбери новую группу:", reply_markup=kb)
    else: await m.answer("Список групп пока не загружен.")

@dp.message(F.text == "📢 Объявления")
async def announcements(m: Message):
    user = await get_user(m.from_user.id)
    rows = await recent_announcements(college_id=(user or {}).get("college_id", "kuzpk"))
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
        buttons.append([InlineKeyboardButton(text="🤖 ИИ · обновить расписание", callback_data="manage|schedule_update")])
        for college_id, college_name in COLLEGES.items():
            buttons.append([InlineKeyboardButton(text=f"📢 Объявление · {college_name}", callback_data=f"manage|college:{college_id}")])
        buttons.append([InlineKeyboardButton(text="📢 Объявление всем колледжам", callback_data="manage|all")])
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
    college_id = u.get("college_id", "kuzpk")
    await save_announcement(m.from_user.id, u["group_name"], body, college_id)
    sent = await send_group_announcement(bot, u["group_name"], body, college_id)
    await m.answer(f"Готово. Отправлено: {sent} пользователям.")
    await state.clear()

@dp.message(F.text == "📢 Объявление всем")
async def ask_all_announcement(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        await m.answer("Эта функция доступна только главному администратору."); return
    await state.update_data(pending_college=None)
    await state.set_state(Forms.admin_announcement)
    await m.answer("Напиши текст общего объявления. Оно будет отправлено всем пользователям:")

@dp.message(Forms.admin_announcement)
async def all_announcement(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        await state.clear(); return
    if not m.text:
        await m.answer("Отправь объявление текстом."); return
    await state.update_data(pending_broadcast=m.text.strip())
    data = await state.get_data()
    college_id = data.get("pending_college")
    audience = f"колледжу {COLLEGES[college_id]}" if college_id in COLLEGES else "всем колледжам"
    await state.set_state(Forms.admin_announcement_confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтвердить рассылку", callback_data="broadcast|yes"), InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast|no")]])
    await m.answer(f"Предпросмотр сообщения для {audience}:\n\n<blockquote>{escape(m.text.strip())}</blockquote>\n\nОтправить?", reply_markup=kb)

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
    college_id = data.get("pending_college")
    if college_id in COLLEGES:
        await save_announcement(c.from_user.id, f"Колледж · {COLLEGES[college_id]}", body, college_id)
        sent = await send_college_announcement(bot, college_id, COLLEGES[college_id], body)
    else:
        await save_announcement(c.from_user.id, "Все колледжи", body, "all")
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
    await m.answer("Главное меню:", reply_markup=main_menu(role, u["group_name"] if u else None, (u or {}).get("college_id", "kuzpk"), (await get_user_settings(m.from_user.id))["hidden_menu"]))

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


def _schedule_row_label(row):
    parts = [f"{row.get('lesson_no') or '?'} пара", str(row.get("subject") or "Занятие")]
    if row.get("room"): parts.append(f"кабинет {row['room']}")
    if row.get("teacher"): parts.append(str(row["teacher"]))
    return ", ".join(parts)


def _schedule_signature(rows):
    fields = ("lesson_no", "lesson", "subject", "teacher", "room")
    return sorted(tuple(str(row.get(field) or "").strip() for field in fields) for row in rows)


def _schedule_change_kind(old_rows, new_rows):
    has_transfer = has_teacher_change = False
    for old in old_rows:
        same_subject = [new for new in new_rows if str(old.get("subject") or "") == str(new.get("subject") or "")]
        if not same_subject:
            return "cancellations"
        same_pair = [new for new in same_subject if str(old.get("lesson_no")) == str(new.get("lesson_no"))]
        if not same_pair:
            has_transfer = True
            continue
        if any(old.get("room") != new.get("room") for new in same_pair):
            has_transfer = True
        if any(old.get("teacher") != new.get("teacher") and old.get("room") == new.get("room") for new in same_pair):
            has_teacher_change = True
    if has_transfer:
        return "transfers"
    if has_teacher_change:
        return "teacher_changes"
    return "schedule_changes"


async def _send_schedule_change(group, schedule_date, old_rows, new_rows, college_id="kuzpk"):
    preference = _schedule_change_kind(old_rows, new_rows)
    audience = await users_for_group_preference(group, college_id, preference)
    if not audience:
        return
    previous = "\n".join("• " + escape(_schedule_row_label(row)) for row in old_rows[:4]) or "занятий не было"
    updated = "\n".join("• " + escape(_schedule_row_label(row)) for row in new_rows[:4]) or "занятия сняты"
    day_label = date.fromisoformat(schedule_date).strftime("%d.%m.%Y")
    message = ("🐶 <b>Гав-гав! Изменения в расписании!</b>\n\n"
               f"У группы <b>{escape(group)}</b> обновилось расписание · {escape(COLLEGES.get(college_id, college_id))}.\n\n"
               f"📅 {day_label}\n❌ <b>Было:</b>\n{previous}\n\n🐾 <b>Стало:</b>\n{updated}\n\n"
               "Я уже обновил расписание — загляни проверить ❤️")
    raw_key = f"schedule:{group}:{schedule_date}:{hashlib.sha256((repr(old_rows)+repr(new_rows)).encode()).hexdigest()}"
    for uid in audience:
        try:
            if not await claim_notification(raw_key + f":{uid}"):
                continue
            await bot.send_message(uid, message)
        except Exception:
            logging.exception("Не удалось отправить уведомление об изменении расписания группе %s", group)


async def sync_kuzpk_schedule():
    from schedule_sync import fetch_kuzpk_schedule
    today = datetime.now(TZ).date()
    rows, buckets = await asyncio.to_thread(fetch_kuzpk_schedule, today)
    old_buckets = await get_auto_schedule_snapshots("kuzpk")
    first_sync = not old_buckets
    changed_keys = {key for key, value in buckets.items() if old_buckets.get(key) != value}
    # Don't rewrite an administrator's manual file on every poll when the site
    # is unchanged. A later change on the official feed will take precedence.
    if changed_keys:
        updated_rows = [row for key in changed_keys for row in buckets[key]]
        await save_schedule_update("kuzpk", updated_rows, "raspis.kuzpcdesk.ru/cg.htm", 0,
                                   replace_days=list(changed_keys))
    if not first_sync:
        for key in changed_keys:
            new_rows = buckets[key]
            old_rows = old_buckets.get(key)
            if old_rows is not None and old_rows != new_rows:
                await _send_schedule_change(key[0], key[1], old_rows, new_rows, "kuzpk")
    await save_auto_schedule_snapshots("kuzpk", buckets)
    logging.info("Обновлён источник расписания КузПК: %s групповых дней, %s занятий", len(buckets), len(rows))


def _time_value(value):
    match = re.search(r"(\d{1,2}:\d{2})", str(value or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%H:%M").time()
    except ValueError:
        return None


def _bell_pair_number(row):
    match = re.search(r"(\d+)\s*пара", str(row.get("lesson") or ""), re.I)
    return int(match.group(1)) if match else None


def _day_bell_rows(rows, day):
    weekday = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")[day.weekday()]
    matched = [r for r in rows if weekday in str(r.get("section", "")).casefold()
               and _bell_pair_number(r) is not None]
    # Prefer a day-specific rule over combined-weekday or curator-hour variants.
    matched.sort(key=lambda r: (weekday not in str(r.get("section", "")).casefold(), len(str(r.get("section", "")))))
    return matched


async def _process_class_bell_notifications(now):
    today = now.date()
    subscribers = await users_with_preference("class_bells")
    if not subscribers:
        return
    cache = {}
    for user in subscribers:
        group, college = user["group_name"], user["college_id"]
        cache_key = (group, college, user.get("college_branch"))
        if cache_key not in cache:
            try:
                cache[cache_key] = (await get_schedule(group, today, college, user.get("college_branch")), await get_bells(college))
            except Exception:
                logging.exception("Не удалось подготовить звонки для группы %s", group)
                cache[cache_key] = ([], [])
        schedule_rows, bells = cache[cache_key]
        if not schedule_rows or not bells:
            continue
        lessons_by_pair = {}
        for row in schedule_rows:
            try:
                lessons_by_pair.setdefault(int(str(row.get("lesson_no") or "")), []).append(row)
            except ValueError:
                continue
        if not lessons_by_pair:
            continue
        # Use the first lesson's shift label for each actual pair when available;
        # if the source does not label shifts, the pair itself still determines its bell slot.
        for pair in sorted(lessons_by_pair):
            group_shift = next((str(row.get("shift") or "").strip() for row in lessons_by_pair[pair]
                                if str(row.get("shift") or "").strip()), "")
            day_bells = _day_bell_rows(bells, today)
            if group_shift:
                shifted = [bell for bell in day_bells
                           if not str(bell.get("shift") or "").strip()
                           or str(bell.get("shift") or "").strip().casefold() in group_shift.casefold()]
                day_bells = shifted or day_bells
            matching_bells = [bell for bell in day_bells if _bell_pair_number(bell) == pair]
            for bell in matching_bells[:1]:
                segments = []
                first_times = re.findall(r"\d{1,2}:\d{2}", str(bell.get("first") or ""))
                second_times = re.findall(r"\d{1,2}:\d{2}", str(bell.get("second") or ""))
                if first_times:
                    segments.append((first_times[0], "Пара", None))
                    if str(bell.get("break_1") or "").strip() not in ("", "—", "-"):
                        try: duration = int(re.search(r"\d+", str(bell["break_1"])).group())
                        except (AttributeError, ValueError): duration = 0
                        if duration: segments.append((first_times[-1], "Перемена", duration))
                if second_times:
                    segments.append((second_times[0], "Пара продолжается", None))
                    if str(bell.get("break_2") or "").strip() not in ("", "—", "-"):
                        try: duration = int(re.search(r"\d+", str(bell["break_2"])).group())
                        except (AttributeError, ValueError): duration = 0
                        if duration: segments.append((second_times[-1], "Перемена", duration))
                for raw_time, label, duration in segments:
                    event_time = _time_value(raw_time)
                    if event_time != now.time().replace(second=0, microsecond=0):
                        continue
                    if label == "Пара":
                        description = f"Пара {pair} начинается"
                    elif label.startswith("Пара"):
                        description = f"Начинается вторая часть пары {pair}"
                    else:
                        description = f"Началась перемена на {duration} мин."
                    key = f"class:{user['telegram_id']}:{today}:{pair}:{label}:{raw_time}"
                    try:
                        if not await claim_notification(key):
                            continue
                        await bot.send_message(user["telegram_id"], f"🐶 <b>Тяф! {escape(description)}</b>\n\n🎓 {escape(group)} · {raw_time}\nУдачного дня ❤️")
                    except Exception:
                        logging.exception("Не удалось отправить напоминание о звонке пользователю %s", user["telegram_id"])


async def _process_sports_reminders(now):
    if now.weekday() >= len(SPORTS_DAYS):
        return
    for index, section in enumerate(SPORTS_SECTIONS):
        period = section["days"].get(now.weekday())
        if not period:
            continue
        start = _time_value(period)
        if not start:
            continue
        training = datetime.combine(now.date(), start, tzinfo=TZ)
        if training - timedelta(minutes=30) != now.replace(second=0, microsecond=0):
            continue
        for uid in await users_for_sport(str(index)):
            key = f"sport:{uid}:{now.date()}:{index}:{start}"
            try:
                if not await claim_notification(key):
                    continue
                await bot.send_message(uid,
                    f"🐶 <b>Тяф-тяф! Не забудь про тренировку!</b>\n\nСегодня у тебя {escape(section['name'].lower())}! 🏅\n"
                    f"🕒 {start.strftime('%H:%M')} · через 30 минут\n📍 Спортивный комплекс\n👤 {escape(section['coach'])}\nДо встречи ❤️")
            except Exception:
                logging.exception("Не удалось отправить напоминание о секции %s", section["name"])


async def background_tasks():
    next_sync = datetime.now(TZ)
    while True:
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        if now >= next_sync:
            try:
                await sync_kuzpk_schedule()
            except Exception:
                logging.exception("Не удалось обновить расписание КузПК с сайта; используется сохранённая версия.")
            next_sync = datetime.now(TZ) + timedelta(seconds=max(300, int(os.getenv("KUZPK_SYNC_INTERVAL", "600"))))
        try:
            await _process_class_bell_notifications(now)
            await _process_sports_reminders(now)
        except Exception:
            logging.exception("Ошибка фоновой отправки напоминаний.")
        await asyncio.sleep(20)

async def main():
    await init_db()
    if ADMIN_ID:
        await add_user(ADMIN_ID, "Администратор", "admin")
    task = asyncio.create_task(background_tasks())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    asyncio.run(main())
