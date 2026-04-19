#!/usr/bin/env python3
import asyncio
import csv
import io
import logging
import json
import html
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, BotCommand, BotCommandScopeChat, KeyboardButton, Message, ReplyKeyboardMarkup
import schedule_raspmath

from database import Database, UserSettingsStore, MySQLStorage, ScheduleCacheStore

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

# ==================== CONFIG ====================
try:
    IRKUTSK_TZ = ZoneInfo("Asia/Irkutsk")
except ZoneInfoNotFoundError:
    IRKUTSK_TZ = timezone(timedelta(hours=8))

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID") or "1307617601")

# Path to the file with user IDs for initial import
USER_IDS_FILE = Path(__file__).with_name("telegram_ids.txt")

# FSM cleanup interval
FSM_CLEANUP_INTERVAL = 3 * 60 * 60  # 3 часа

# ==================== REGEX ====================
RE_SUBGROUP = re.compile(r"подгруппа\s*(\d+)", re.IGNORECASE)

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# Родительный падеж для заголовка дня (расписание ИМИТ)
RU_MONTHS_GEN: dict[int, str] = {v: k for k, v in RU_MONTHS.items()}

WD_RU: tuple[str, ...] = (
    "Понедельник", "Вторник", "Среда", "Четвер", "Пятница", "Суббота", "Воскресенье",
)

# ==================== BUTTONS ====================
BTN_TODAY = "📆 На сегодня"
BTN_TOMORROW = "⏭️ На завтра"
BTN_THIS_WEEK = "📆 На текущую неделю"
BTN_NEXT_WEEK = "⏭️ На следующую неделю"
BTN_CHANGE_GROUP = "🔁 Изменить группу"
BTN_REPORT = "🐞 Сообщить о проблеме"
BTN_BACK = "⬅️ Назад"
BTN_PAGE_PREV = "⬅️"
BTN_PAGE_NEXT = "➡️"
BTN_CANCEL = "❌ Отмена"

ALL_BTNS = {
    BTN_TODAY,
    BTN_TOMORROW,
    BTN_THIS_WEEK,
    BTN_NEXT_WEEK,
    BTN_CHANGE_GROUP,
    BTN_REPORT,
    BTN_BACK,
    BTN_PAGE_PREV,
    BTN_PAGE_NEXT,
    BTN_CANCEL,
}

MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
        [KeyboardButton(text=BTN_THIS_WEEK), KeyboardButton(text=BTN_NEXT_WEEK)],
        [KeyboardButton(text=BTN_CHANGE_GROUP), KeyboardButton(text=BTN_REPORT)],
    ],
    resize_keyboard=True,
)

MENU_KB_GROUP = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
        [KeyboardButton(text=BTN_THIS_WEEK), KeyboardButton(text=BTN_NEXT_WEEK)],
        [KeyboardButton(text=BTN_CHANGE_GROUP), KeyboardButton(text=BTN_REPORT)],
    ],
    resize_keyboard=True,
)

# ==================== FSM ====================
class SetupFlow(StatesGroup):
    institute = State()
    course = State()
    group = State()

class ReportFlow(StatesGroup):
    report = State()

# FSM для рассылки
class BroadcastFlow(StatesGroup):
    waiting_text = State()

# ==================== MODELS ====================
class Institute:
    __slots__ = ('subdiv_id', 'title')
    def __init__(self, subdiv_id: int, title: str):
        self.subdiv_id = subdiv_id
        self.title = title

class Group:
    __slots__ = ('group_id', 'title')
    def __init__(self, group_id: int, title: str):
        self.group_id = group_id
        self.title = title

class Lesson:
    __slots__ = ('start', 'subject', 'kind', 'subgroup', 'room', 'teacher', 'group_name')
    def __init__(self, start: time, subject: str, kind: str, subgroup: str, room: str, teacher: str, group_name: str = ""):
        self.start = start
        self.subject = subject
        self.kind = kind
        self.subgroup = subgroup
        self.room = room
        self.teacher = teacher
        self.group_name = group_name

# ==================== HELPERS ====================
def _extract_subgroup(text: str) -> str:
    m = RE_SUBGROUP.search(text)
    return f"подгруппа {m.group(1)}" if m else ""

def _subgroup_sort_key(subgroup: str) -> tuple[int, int]:
    if not subgroup:
        return (1, 0)
    m = RE_SUBGROUP.search(subgroup) or re.search(r"(\d+)", subgroup)
    if not m:
        return (1, 0)
    return (0, int(m.group(1)))

def _format_day_heading(d: date) -> str:
    """Заголовок дня для расписания ИМИТ."""
    return f"{WD_RU[d.weekday()]}, {d.day} {RU_MONTHS_GEN[d.month]}"


def _lessons_from_cache_payload(payload: str) -> List[Lesson]:
    """Восстанавливает список Lesson из JSON, сохранённого в schedule_cache."""
    items = json.loads(payload)
    if not isinstance(items, list):
        return []
    out: List[Lesson] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        start_s = str(it.get("start") or "00:00")
        try:
            hh, mm = (int(x) for x in start_s.split(":")[:2])
            st = time(hh, mm)
        except (ValueError, TypeError):
            continue
        out.append(
            Lesson(
                st,
                str(it.get("subject") or ""),
                str(it.get("kind") or ""),
                str(it.get("subgroup") or ""),
                str(it.get("room") or "—") or "—",
                str(it.get("teacher") or "—") or "—",
                str(it.get("group_name") or ""),
            )
        )
    return out


async def _ensure_cached_days_for_group(
    session: aiohttp.ClientSession,
    cache: ScheduleCacheStore,
    group_id: int,
    dates: List[date],
) -> None:
    """
    Для всех дат из dates: если в БД нет валидной записи — один запрос fillSchedule,
    затем заполнение кэша по каждому отсутствующему дню.
    """
    missing: List[date] = []
    for d in dates:
        if await cache.get_day_payload(group_id, d) is None:
            missing.append(d)
    if not missing:
        return
    raw = await schedule_raspmath.fetch_schedule_json(session, group_id)
    for d in missing:
        serialized = schedule_raspmath.lessons_dicts_for_date(raw, d)
        await cache.upsert_day(group_id, d, json.dumps(serialized, ensure_ascii=False))


def _format_day_message(heading: str, lessons: List[Lesson]) -> str:
    sep = "· · · · · · · · · · · · · · · · · ·"
    out: List[str] = [f"🧭 <b>{html.escape(heading)}</b>", f"<i>ИМИТ ИГУ · raspmath.isu.ru</i>", sep]
    
    if not lessons:
        out.append("✨ Свободный день — пар нет.")
        return "\n".join(out)
    
    lessons.sort(key=lambda l: (l.start, _subgroup_sort_key(l.subgroup), l.subject.lower(), l.kind, l.room, l.teacher))
    
    blocks: dict[time, List[Lesson]] = {}
    order: List[time] = []
    
    for lesson in lessons:
        if lesson.start not in blocks:
            blocks[lesson.start] = []
            order.append(lesson.start)
        blocks[lesson.start].append(lesson)
    
    for i, start_t in enumerate(order):
        start_dt = datetime.combine(date(2000, 1, 1), start_t)
        end_dt = start_dt + timedelta(minutes=90)
        
        for j, lesson in enumerate(blocks[start_t]):
            if j > 0:
                out.append("▫️ ▫️ ▫️")
            kind = f" · <u>{html.escape(lesson.kind)}</u>" if lesson.kind else ""
            out.append(
                f"🕒 <code>{start_t.strftime('%H:%M')}–{end_dt.time().strftime('%H:%M')}</code> "
                f"<b>{html.escape(lesson.subject)}</b>{kind}"
            )
            
            details = [d for d in [
                html.escape(lesson.subgroup) if lesson.subgroup else None,
                f"📍 {html.escape(lesson.room)}" if lesson.room != "—" else None,
                f"👤 {html.escape(lesson.teacher)}" if lesson.teacher != "—" else None
            ] if d]
            
            if details:
                out.append("　└ " + " · ".join(details))
        
        if i < len(order) - 1:
            out.append(sep)
    
    return "\n".join(out)

def _chunk(items: List[str], size: int) -> List[List[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]

def _is_sshg(title: str) -> bool:
    t = title.lower()
    return "сибирская школа геонаук" in t or "siberian school of geosciences" in t

# ==================== KEYBOARD BUILDERS ====================
def build_paged_kb(options: List[str], page: int, page_size: int, row_size: int, show_back: bool) -> ReplyKeyboardMarkup:
    start = page * page_size
    slice_opts = options[start:start + page_size]
    keyboard: List[List[KeyboardButton]] = []
    
    for row in _chunk(slice_opts, row_size):
        keyboard.append([KeyboardButton(text=t) for t in row])
    
    controls = []
    if page > 0:
        controls.append(KeyboardButton(text=BTN_PAGE_PREV))
    if start + page_size < len(options):
        controls.append(KeyboardButton(text=BTN_PAGE_NEXT))
    if controls:
        keyboard.append(controls)
    
    keyboard.append([KeyboardButton(text=BTN_REPORT)])
    if show_back:
        keyboard.append([KeyboardButton(text=BTN_BACK)])
    keyboard.append([KeyboardButton(text=BTN_CANCEL)])
    
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# ==================== REFERENCE DATA CACHE ====================
class ReferenceDataCache:
    """
    In-memory кэш справочника групп ИМИТ (страница /schedule/, селект #inputGroupSelect).
    «Институт» = optgroup (например бакалавриат/магистратура); все группы лежат в «курсе» 1.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._institutes: Optional[List[Institute]] = None
        self._institutes_loaded_at: Optional[float] = None
        self._institutes_ttl = 1800.0  # 30 минут
        self._groups_cache: Dict[int, Dict[int, List[Group]]] = {}
        self._groups_loaded_at: Dict[int, float] = {}
        self._groups_ttl = 600.0  # 10 минут
        self._inst_by_label: Dict[str, Institute] = {}

    def _current_time(self) -> float:
        return asyncio.get_running_loop().time()

    async def _reload_from_site(self) -> None:
        html = await schedule_raspmath.fetch_schedule_page_html(self._session)
        parsed_insts, by_subdiv = schedule_raspmath.parse_institutes_and_groups(html)
        now = self._current_time()
        self._institutes = [Institute(pi.subdiv_id, pi.title) for pi in parsed_insts]
        self._institutes_loaded_at = now
        self._inst_by_label = {}
        self._groups_cache = {}
        self._groups_loaded_at = {}
        for inst in self._institutes:
            label = "СШГ" if _is_sshg(inst.title) else inst.title
            self._inst_by_label[label] = inst
            raw = by_subdiv.get(inst.subdiv_id, {}).get(1, [])
            self._groups_cache[inst.subdiv_id] = {1: [Group(g.group_id, g.title) for g in raw]}
            self._groups_loaded_at[inst.subdiv_id] = now

    async def get_institutes(self) -> List[Institute]:
        now = self._current_time()
        if self._institutes is not None and self._institutes_loaded_at is not None:
            if now - self._institutes_loaded_at < self._institutes_ttl:
                return self._institutes
        await self._reload_from_site()
        assert self._institutes is not None
        return self._institutes

    def get_institute_labels(self) -> List[str]:
        return list(self._inst_by_label.keys())

    def find_institute_by_label(self, label: str) -> Optional[Institute]:
        return self._inst_by_label.get(label)

    async def get_groups_by_course(self, subdiv_id: int) -> Dict[int, List[Group]]:
        now = self._current_time()
        if subdiv_id in self._groups_cache:
            loaded_at = self._groups_loaded_at.get(subdiv_id, 0)
            if now - loaded_at < self._groups_ttl:
                return self._groups_cache[subdiv_id]
        await self._reload_from_site()
        return self._groups_cache.get(subdiv_id, {1: []})

    def get_cached_groups(self, subdiv_id: int) -> Optional[Dict[int, List[Group]]]:
        return self._groups_cache.get(subdiv_id)

# ==================== NAVIGATION LOGIC ====================
async def handle_navigation(
    message: Message,
    state: FSMContext,
    options: List[str],
    page_key: str,
    page_size: int,
    row_size: int,
    back_state: Optional[State] = None,
    back_options: Optional[List[str]] = None,
    back_layout: Optional[tuple[int, int]] = None,
) -> bool:
    text = message.text
    
    if text == BTN_CANCEL:
        await state.clear()
        await message.answer("Ок", reply_markup=MENU_KB_GROUP)
        return True
    
    if text == BTN_BACK and back_state:
        await state.set_state(back_state)
        if back_options:
            ps, rs = back_layout if back_layout is not None else (page_size, row_size)
            await message.answer("Выбери:", reply_markup=build_paged_kb(back_options, 0, ps, rs, False))
        return True
    
    if text in (BTN_PAGE_PREV, BTN_PAGE_NEXT):
        data = await state.get_data()
        current_page = data.get(page_key, 0)
        
        if text == BTN_PAGE_PREV:
            new_page = max(0, current_page - 1)
        else:
            max_page = (len(options) - 1) // page_size
            new_page = min(max_page, current_page + 1)
        
        await state.update_data({page_key: new_page})
        await message.answer("Выбери:", reply_markup=build_paged_kb(options, new_page, page_size, row_size, bool(back_state)))
        return True
    
    return False

async def safe_send(message: Message, text: str, limit: int = 3800):
    while text:
        await message.answer(text[:limit])
        text = text[limit:]


# ==================== DATABASE EXTENSIONS ====================

# [ADDED] Инициализация таблицы зарегистрированных пользователей (для рассылки)
async def init_registered_users_table(db: Database):
    await db.execute("""
        CREATE TABLE IF NOT EXISTS registered_users (
            user_id BIGINT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    logger = logging.getLogger(__name__)
    logger.info("registered_users table initialized")

# [ADDED] Добавить user_id в registered_users
async def register_user(db: Database, user_id: int):
    try:
        await db.execute(
            "INSERT IGNORE INTO registered_users (user_id) VALUES (%s)",
            (user_id,)
        )
    except Exception:
        logging.getLogger(__name__).exception(f"Failed to register user {user_id}")

# [ADDED] Импорт user_id из txt-файла в registered_users
async def import_users_from_file(db: Database, filepath: Path):
    if not filepath.exists():
        logging.getLogger(__name__).warning(f"User IDs file not found: {filepath}")
        return
    
    count = 0
    errors = 0
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                uid = int(line)
                await db.execute(
                    "INSERT IGNORE INTO registered_users (user_id) VALUES (%s)",
                    (uid,)
                )
                count += 1
            except Exception:
                errors += 1
    
    logging.getLogger(__name__).info(f"Imported {count} user IDs from file ({errors} errors)")


# ==================== HANDLERS ====================
async def cmd_start(message: Message, state: FSMContext, ref_cache: ReferenceDataCache, db: Database):
    # [EXTENDED] Регистрируем пользователя при старте
    if message.from_user:
        await register_user(db, message.from_user.id)
    
    await state.clear()
    await state.set_state(SetupFlow.institute)
    
    try:
        await ref_cache.get_institutes()
    except Exception:
        logging.exception("Failed to load institutes")
        await message.answer("⚠️ Не удалось загрузить страницу raspmath.isu.ru/schedule/. Попробуй позже.")
        return
    
    labels = ref_cache.get_institute_labels()
    await state.update_data(inst_page=0)
    await message.answer("Выбери раздел (как на сайте ИМИТ):", reply_markup=build_paged_kb(labels, 0, 12, 1, False))

async def on_setup_institute(message: Message, state: FSMContext, ref_cache: ReferenceDataCache):
    labels = ref_cache.get_institute_labels()
    
    if await handle_navigation(message, state, labels, "inst_page", 12, 1):
        return
    
    selected = ref_cache.find_institute_by_label(message.text)
    if not selected:
        await message.answer("Выбери раздел кнопкой.", reply_markup=build_paged_kb(labels, 0, 12, 1, False))
        return
    
    try:
        await ref_cache.get_groups_by_course(selected.subdiv_id)
    except Exception:
        logging.exception(f"Failed to load groups for subdiv {selected.subdiv_id}")
        await message.answer("⚠️ Не удалось загрузить список групп. Попробуй позже.")
        return
    
    by_course = ref_cache.get_cached_groups(selected.subdiv_id)
    groups = (by_course or {}).get(1, [])
    if not groups:
        await message.answer("В этом разделе нет групп (пустой список на сайте).")
        return

    # На ИМИТ нет шага «курс» — сразу выбор группы (курс в БД фиксируем как 1).
    await state.set_state(SetupFlow.group)
    await state.update_data(subdiv_id=selected.subdiv_id, course=1, group_page=0, courses=[1])
    await message.answer("Выбери группу:", reply_markup=build_paged_kb([g.title for g in groups], 0, 10, 2, True))

async def on_setup_course(message: Message, state: FSMContext, ref_cache: ReferenceDataCache):
    data = await state.get_data()
    courses = data.get("courses", [])
    subdiv_id = data.get("subdiv_id")
    course_labels = [str(c) for c in courses]
    
    if await handle_navigation(
        message,
        state,
        course_labels,
        "course_page",
        12,
        3,
        SetupFlow.institute,
        ref_cache.get_institute_labels(),
        back_layout=(12, 1),
    ):
        return
    
    try:
        course = int(message.text)
    except ValueError:
        await message.answer("Выбери курс кнопкой.", reply_markup=build_paged_kb(course_labels, 0, 12, 3, True))
        return
    
    if course not in courses:
        await message.answer("Выбери курс кнопкой.", reply_markup=build_paged_kb(course_labels, 0, 12, 3, True))
        return
    
    by_course = ref_cache.get_cached_groups(subdiv_id)
    if not by_course:
        await message.answer("⚠️ Данные устарели. Начни заново /start")
        await state.clear()
        return
    
    groups = by_course.get(course, [])
    if not groups:
        await message.answer("Нет групп на этом курсе.")
        return
    
    await state.set_state(SetupFlow.group)
    await state.update_data(course=course, group_page=0)
    await message.answer("Выбери группу:", reply_markup=build_paged_kb([g.title for g in groups], 0, 10, 2, True))

async def on_setup_group(message: Message, state: FSMContext, ref_cache: ReferenceDataCache, store: UserSettingsStore):
    data = await state.get_data()
    subdiv_id = data.get("subdiv_id")
    course = data.get("course") or 1
    
    by_course = ref_cache.get_cached_groups(subdiv_id)
    if not by_course:
        await message.answer("⚠️ Данные устарели. Начни заново /start")
        await state.clear()
        return
    
    groups = by_course.get(course, [])
    titles = [g.title for g in groups]
    
    back_inst = ref_cache.get_institute_labels()
    if await handle_navigation(
        message,
        state,
        titles,
        "group_page",
        10,
        2,
        SetupFlow.institute,
        back_inst,
        back_layout=(12, 1),
    ):
        return
    
    selected = next((g for g in groups if g.title == message.text), None)
    if not selected:
        page = data.get("group_page", 0)
        await message.answer("Выбери группу кнопкой.", reply_markup=build_paged_kb(titles, page, 10, 2, True))
        return
    
    await store.set(message.from_user.id, {
        "group_id": selected.group_id,
        "group_title": selected.title,
        "subdiv_id": subdiv_id,
        "course": course,
    })
    
    await state.clear()
    # [EXTENDED] Показываем меню группы с кнопкой преподавателей
    await message.answer(f"Ок, группа: {selected.title}", reply_markup=MENU_KB_GROUP)

async def cmd_report(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ReportFlow.report)
    await state.update_data(report_text="", report_photo=None)
    await message.answer(
        "Опиши проблему. Можно с фото.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
            resize_keyboard=True
        )
    )

async def on_report_message(message: Message, state: FSMContext, store: UserSettingsStore, bot: Bot):
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer("Ок", reply_markup=MENU_KB_GROUP)
        return
    
    data = await state.get_data()
    text = data.get("report_text", "")
    photo = data.get("report_photo")
    
    new_text = message.caption or message.text or ""
    if new_text and new_text not in ALL_BTNS:
        text = f"{text}\n{new_text}".strip()
    if message.photo:
        photo = message.photo[-1].file_id
    
    if not text and not photo:
        await message.answer("Нужен текст или фото.")
        return
    
    await state.update_data(report_text=text, report_photo=photo)
    
    if text:
        user = message.from_user
        u_line = f"{user.full_name} (@{user.username})" if user else "Unknown"
        settings = await store.get(user.id) if user else {}
        
        body = html.escape(text[:3000])
        msg = (
            f"<b>Баг-репорт</b>\n"
            f"<b>User:</b> {html.escape(u_line)}\n"
            f"<b>Group:</b> {html.escape(str(settings.get('group_title', '-')))}\n\n"
            f"{body}"
        )
        
        try:
            if photo:
                await bot.send_photo(ADMIN_USER_ID, photo, caption=msg[:1024])
            else:
                await bot.send_message(ADMIN_USER_ID, msg)
        except Exception:
            logging.exception("Send report error")
        
        await state.clear()
        await message.answer("Спасибо! Передал.", reply_markup=MENU_KB_GROUP)
    else:
        await message.answer("Фото принято. Теперь опиши проблему текстом.")

async def on_menu(
    message: Message,
    state: FSMContext,
    store: UserSettingsStore,
    schedule_cache: ScheduleCacheStore,
    http_session: aiohttp.ClientSession,
):
    now = datetime.now(IRKUTSK_TZ)

    settings = await store.get(message.from_user.id) if message.from_user else {}
    gid = settings.get("group_id")

    if not gid:
        await message.answer("Сначала выбери группу: /start", reply_markup=MENU_KB_GROUP)
        return

    gid_int = int(gid)

    if message.text == BTN_TODAY:
        await send_day(message, http_session, schedule_cache, gid_int, now.date())
    elif message.text == BTN_TOMORROW:
        await send_day(message, http_session, schedule_cache, gid_int, now.date() + timedelta(days=1))
    elif message.text == BTN_THIS_WEEK:
        await send_week(message, http_session, schedule_cache, gid_int, now.date() - timedelta(days=now.date().weekday()))
    elif message.text == BTN_NEXT_WEEK:
        await send_week(
            message,
            http_session,
            schedule_cache,
            gid_int,
            now.date() - timedelta(days=now.date().weekday()) + timedelta(days=7),
        )


async def send_day(
    message: Message,
    http_session: aiohttp.ClientSession,
    schedule_cache: ScheduleCacheStore,
    gid: int,
    d: date,
) -> None:
    """Расписание группы: сначала MySQL-кэш, при промахе — POST fillSchedule и запись в кэш."""
    try:
        await _ensure_cached_days_for_group(http_session, schedule_cache, gid, [d])
    except Exception:
        logging.exception("Raspmath schedule fetch failed (day)")
        await message.answer("⚠️ Не удалось получить данные с raspmath.isu.ru. Попробуй позже.")
        return
    payload = await schedule_cache.get_day_payload(gid, d)
    if payload is None:
        await message.answer("Нет данных на этот день.")
        return
    lessons = _lessons_from_cache_payload(payload)
    heading = _format_day_heading(d)
    await safe_send(message, _format_day_message(heading, lessons))


async def send_week(
    message: Message,
    http_session: aiohttp.ClientSession,
    schedule_cache: ScheduleCacheStore,
    gid: int,
    monday: date,
) -> None:
    dates = [monday + timedelta(days=i) for i in range(7)]
    try:
        await _ensure_cached_days_for_group(http_session, schedule_cache, gid, dates)
    except Exception:
        logging.exception("Raspmath schedule fetch failed (week)")
        await message.answer("⚠️ Не удалось получить данные с raspmath.isu.ru. Попробуй позже.")
        return

    await message.answer("📅 <b>Неделя</b> · ИМИТ ИГУ · raspmath.isu.ru")
    shown = False
    for d in dates:
        payload = await schedule_cache.get_day_payload(gid, d)
        if payload is None:
            continue
        shown = True
        lessons = _lessons_from_cache_payload(payload)
        heading = _format_day_heading(d)
        await safe_send(message, _format_day_message(heading, lessons))
    if not shown:
        await message.answer("Не удалось собрать расписание на эту неделю.")


# ==================== ADMIN HANDLERS ====================

# [ADDED] /broadcast — рассылка всем пользователям
async def cmd_broadcast(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_USER_ID:
        return
    
    await state.set_state(BroadcastFlow.waiting_text)
    await message.answer(
        "Введи текст для рассылки (HTML разметка поддерживается):",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
            resize_keyboard=True
        )
    )

# [ADDED] Получение текста и выполнение рассылки
async def on_broadcast_text(message: Message, state: FSMContext, bot: Bot, db: Database):
    if not message.from_user or message.from_user.id != ADMIN_USER_ID:
        return
    
    if message.text == BTN_CANCEL:
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=MENU_KB_GROUP)
        return
    
    text = message.text or ""
    if not text.strip():
        await message.answer("Текст пустой. Введи снова или нажми Отмена.")
        return
    
    await state.clear()
    await message.answer("Начинаю рассылку...")
    
    rows = await db.fetchall("SELECT user_id FROM registered_users")
    user_ids = [row[0] for row in rows] if rows else []
    
    sent = 0
    failed = 0
    
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception as e:
            failed += 1
            logging.getLogger(__name__).warning(f"Broadcast failed for {uid}: {e}")
        await asyncio.sleep(0.05)  # не давим на Telegram API
    
    await message.answer(
        f"✅ Рассылка завершена.\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}\n"
        f"Всего пользователей: {len(user_ids)}",
        reply_markup=MENU_KB_GROUP
    )

# [ADDED] /stats — статистика + CSV
async def cmd_stats(message: Message, bot: Bot, db: Database, store: UserSettingsStore):
    if not message.from_user or message.from_user.id != ADMIN_USER_ID:
        return
    
    # Общее количество пользователей
    total_row = await db.fetchone("SELECT COUNT(*) FROM registered_users")
    total = total_row[0] if total_row else 0
    
    # Краткое сообщение — только итоговая цифра
    await message.answer(f"<b>📊 Статистика бота</b>\nВсего пользователей: <b>{total}</b>")
    
    # CSV: ВСЕ пользователи из registered_users, даже без группы
    all_rows = await db.fetchall(
        "SELECT ru.user_id, us.group_title, us.course "
        "FROM registered_users ru "
        "LEFT JOIN user_settings us ON ru.user_id = us.user_id "
        "ORDER BY ru.user_id"
    )
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["user_id", "group_name", "course"])
    
    for row in (all_rows or []):
        user_id = row[0]
        group_name = row[1] if row[1] is not None else ""
        course = row[2] if row[2] is not None else ""
        writer.writerow([user_id, group_name, course])
    
    csv_bytes = output.getvalue().encode("utf-8-sig")
    filename = f"users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    await bot.send_document(
        ADMIN_USER_ID,
        BufferedInputFile(csv_bytes, filename=filename)
    )


# ==================== FSM CLEANUP TASK ====================
async def fsm_cleanup_task(fsm_storage: MySQLStorage, schedule_cache: ScheduleCacheStore) -> None:
    """Периодическая очистка FSM и просроченного кэша расписания раз в 3 часа."""
    while True:
        await asyncio.sleep(FSM_CLEANUP_INTERVAL)
        try:
            await fsm_storage.cleanup()
        except Exception:
            logging.exception("FSM cleanup error")
        try:
            await schedule_cache.purge_expired()
        except Exception:
            logging.exception("schedule_cache purge error")

# ==================== MAIN ====================
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN required")
    
    bot = Bot(
        token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=AiohttpSession(proxy=os.getenv("TELEGRAM_PROXY"))
    )
    
    db = Database(
        os.getenv("DB_HOST", "localhost"),
        int(os.getenv("DB_PORT", "3306")),
        os.getenv("DB_USER", "istu_bot"),
        os.getenv("DB_PASSWORD", ""),
        os.getenv("DB_NAME", "istu_bot")
    )
    await db.connect()
    
    store = UserSettingsStore(db)
    await store.initialize()
    
    # [ADDED] Инициализируем таблицу зарегистрированных пользователей
    await init_registered_users_table(db)
    
    # [ADDED] Импортируем user_id из файла (INSERT IGNORE — безопасно при повторных запусках)
    await import_users_from_file(db, USER_IDS_FILE)
    
    fsm_storage = MySQLStorage(db)
    await fsm_storage.initialize()
    
    dp = Dispatcher(storage=fsm_storage)
    
    schedule_cache = ScheduleCacheStore(db)
    await schedule_cache.initialize()

    async with aiohttp.ClientSession(headers={"User-Agent": "IMMIT-ScheduleBot/3.0 (aiogram+aiohttp)"}) as http:
        ref_cache = ReferenceDataCache(http)

        dp["store"] = store
        dp["ref_cache"] = ref_cache
        dp["schedule_cache"] = schedule_cache
        dp["http_session"] = http
        dp["db"] = db  # [ADDED] передаём db для admin handlers

        dp.message.register(cmd_start, Command("start"))
        dp.message.register(cmd_start, F.text == BTN_CHANGE_GROUP)
        dp.message.register(cmd_report, F.text == BTN_REPORT)
        dp.message.register(on_setup_institute, SetupFlow.institute)
        dp.message.register(on_setup_course, SetupFlow.course)
        dp.message.register(on_setup_group, SetupFlow.group)
        dp.message.register(on_report_message, ReportFlow.report)
        dp.message.register(on_menu, F.text.in_({BTN_TODAY, BTN_TOMORROW, BTN_THIS_WEEK, BTN_NEXT_WEEK}))

        # Админ-команды
        dp.message.register(cmd_broadcast, Command("broadcast"))
        dp.message.register(cmd_stats, Command("stats"))
        dp.message.register(on_broadcast_text, BroadcastFlow.waiting_text)
        
        cleanup_task = asyncio.create_task(fsm_cleanup_task(fsm_storage, schedule_cache))
        
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            me = await bot.get_me()
            logging.info(f"Bot @{me.username} started")
            
            # [ADDED] Регистрируем команды в Telegram
            # Для обычных пользователей — только /start
            await bot.set_my_commands(
                [BotCommand(command="start", description="Запустить бота")]
            )
            # Для администратора — все команды включая /broadcast и /stats
            try:
                await bot.set_my_commands(
                    [
                        BotCommand(command="start", description="Запустить бота"),
                        BotCommand(command="broadcast", description="📢 Рассылка всем"),
                        BotCommand(command="stats", description="📊 Статистика + CSV"),
                    ],
                    scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID)
                )
            except Exception:
                logging.warning("Не удалось установить команды администратора — продолжаю")
            
            await dp.start_polling(bot)
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await db.disconnect()
            await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
