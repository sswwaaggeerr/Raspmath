"""
Асинхронная загрузка расписания ИМИТ ИГУ (raspmath.isu.ru): HTML-справочник групп + JSON /fillSchedule.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, time
from typing import Any, Optional

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

RASPMATH_SCHEDULE_PAGE: str = "https://raspmath.isu.ru/schedule/"
RASPMATH_FILL_SCHEDULE: str = "https://raspmath.isu.ru/fillSchedule"

# timeId в ответе API — строки расписания на сайте (как в таблице #scheduleTableTop)
TIME_BY_ID: dict[int, tuple[time, time]] = {
    1: (time(8, 30), time(10, 0)),
    2: (time(10, 10), time(11, 40)),
    3: (time(11, 50), time(13, 20)),
    4: (time(13, 50), time(15, 20)),
    5: (time(15, 30), time(17, 0)),
    6: (time(17, 10), time(18, 40)),
    7: (time(18, 50), time(20, 20)),
}


@dataclass(frozen=True)
class ParsedInstitute:
    """Уровень optgroup на странице (например, бакалавриат / магистратура)."""

    subdiv_id: int
    title: str


@dataclass(frozen=True)
class ParsedGroup:
    """Группа: value селекта — внутренний числовой id на сервере ИМИТ."""

    group_id: int
    title: str


def _parse_iso_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


async def fetch_schedule_page_html(session: aiohttp.ClientSession) -> str:
    """Загружает HTML страницы /schedule/ (селект групп)."""
    async with session.get(
        RASPMATH_SCHEDULE_PAGE,
        timeout=aiohttp.ClientTimeout(total=20),
        headers={"User-Agent": "TG-IMMIT-ScheduleBot/1.0"},
    ) as resp:
        resp.raise_for_status()
        return await resp.text()


def parse_institutes_and_groups(
    html: str,
) -> tuple[list[ParsedInstitute], dict[int, dict[int, list[ParsedGroup]]]]:
    """
    Парсит #inputGroupSelect: optgroup → «институт», option → группа.
    Курсы в UI бота не используются: все группы кладём в «курс» 1.
    """
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.select_one("#inputGroupSelect")
    if not sel:
        raise RuntimeError("Не найден список групп (#inputGroupSelect)")

    institutes: list[ParsedInstitute] = []
    by_subdiv: dict[int, dict[int, list[ParsedGroup]]] = {}

    for idx, og in enumerate(sel.find_all("optgroup", recursive=False), start=1):
        label = (og.get("label") or f"Раздел {idx}").strip()
        institutes.append(ParsedInstitute(subdiv_id=idx, title=label))
        groups: list[ParsedGroup] = []
        for opt in og.find_all("option"):
            val = (opt.get("value") or "").strip()
            title = opt.get_text(strip=True)
            if not val or not title:
                continue
            try:
                gid = int(val)
            except ValueError:
                continue
            groups.append(ParsedGroup(group_id=gid, title=title))
        groups.sort(key=lambda g: g.title.lower())
        by_subdiv[idx] = {1: groups}

    institutes.sort(key=lambda i: i.title.lower())
    return institutes, by_subdiv


async def fetch_schedule_json(session: aiohttp.ClientSession, group_id: int) -> list[dict[str, Any]]:
    """POST /fillSchedule — полный набор пар группы (как на сайте)."""
    body = aiohttp.FormData()
    body.add_field("groupId", str(group_id))
    async with session.post(
        RASPMATH_FILL_SCHEDULE,
        data=body,
        timeout=aiohttp.ClientTimeout(total=25),
        headers={"User-Agent": "TG-IMMIT-ScheduleBot/1.0"},
    ) as resp:
        resp.raise_for_status()
        text = await resp.text()
    data = json.loads(text)
    if not isinstance(data, list):
        raise RuntimeError("Ответ fillSchedule не является JSON-массивом")
    return data


def _parity_matches(week_label: str, d: date) -> bool:
    """
    Верхняя неделя = нечётная по ISO, нижняя = чётная (как в подписи на сайте).
    Пустая строка week — занятие на обе недели.
    """
    w = (week_label or "").strip().lower()
    if not w:
        return True
    odd = d.isocalendar().week % 2 == 1
    if "верх" in w:
        return odd
    if "ниж" in w:
        return not odd
    return True


def _weekday_matches(weekday_id: int, d: date) -> bool:
    # API: 1 = понедельник … 6 = суббота; в Python понедельник = 0
    return weekday_id == d.weekday() + 1


def lessons_dicts_for_date(raw: list[dict[str, Any]], d: date) -> list[dict[str, Any]]:
    """
    Отбирает пары, относящиеся к календарной дате d, и возвращает плоские dict для JSON-кэша.
    """
    lessons_out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            wid = int(item.get("weekdayId") or 0)
        except (TypeError, ValueError):
            continue
        if not _weekday_matches(wid, d):
            continue
        if not _parity_matches(str(item.get("week") or ""), d):
            continue

        bp, ep = _parse_iso_date(item.get("beginDatePairs")), _parse_iso_date(item.get("endDatePairs"))
        if bp and ep and not (bp <= d <= ep):
            continue

        b0, e0 = _parse_iso_date(item.get("beginDate")), _parse_iso_date(item.get("endDate"))
        if b0 and e0 and not (b0 <= d <= e0):
            continue

        if int(item.get("checkedGroup") or 0) == 0:
            continue

        try:
            tid = int(item.get("timeId") or 0)
        except (TypeError, ValueError):
            continue
        if tid not in TIME_BY_ID:
            continue
        start_t, _end_slot = TIME_BY_ID[tid]

        kind_src = str(item.get("typeSubjectName") or "")
        kind = ""
        low = kind_src.lower()
        if "лек" in low:
            kind = "лекция"
        elif "практ" in low:
            kind = "практика"
        elif "лаб" in low:
            kind = "лаба"

        teacher = str(item.get("teacherName") or "—").strip() or "—"
        room = str(item.get("className") or "—").strip() or "—"
        subject = str(item.get("subjectName") or "").strip()
        if not subject:
            continue

        lessons_out.append(
            {
                "start": start_t.strftime("%H:%M"),
                "subject": subject,
                "kind": kind,
                "subgroup": "",
                "room": room,
                "teacher": teacher,
                "group_name": str(item.get("groupName") or "").strip(),
            }
        )

    lessons_out.sort(
        key=lambda x: (x["start"], x["subject"].lower(), x["kind"], x["room"], x["teacher"])
    )
    return lessons_out
