"""Мелкие утилиты: даты, деньги, склонения, HTML-эскейп."""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta

WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_FULL = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
MONTHS_NOM = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


def parse_date(value) -> date | None:
    """Принимает date/datetime/ISO-строку/'2026-08-09T10:00:00+03:00'."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[: len(fmt) + 2].rstrip("+-"), fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        d = parse_date(s)
        return datetime.combine(d, datetime.min.time()) if d else None


def fmt_date(d: date | None) -> str:
    if not d:
        return "—"
    return f"{d.day} {MONTHS_GEN[d.month - 1]} ({WEEKDAYS_SHORT[d.weekday()]})"


def fmt_date_short(d: date | None) -> str:
    return d.strftime("%d.%m") if d else "—"


def fmt_money(value: float | int | None, currency: str = "₽") -> str:
    if value is None:
        return "—"
    v = int(round(float(value)))
    return f"{v:,}".replace(",", " ") + f" {currency}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def nights_word(n: int) -> str:
    return f"{n} {plural(n, 'ночь', 'ночи', 'ночей')}"


def days_word(n: int) -> str:
    return f"{n} {plural(n, 'день', 'дня', 'дней')}"


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def distance_band(km: float | None) -> str:
    if not km:
        return "medium"
    if km < 2500:
        return "short"
    if km < 5500:
        return "medium"
    if km < 9000:
        return "long"
    return "ultra"


def rail_distance_band(km: float | None) -> str:
    if not km:
        return "medium"
    if km < 700:
        return "short"
    if km < 2000:
        return "medium"
    return "long"


def daterange(start: date, end: date, step_days: int = 1):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=step_days)


def chunk_text(text: str, limit: int = 3900) -> list[str]:
    """Режет длинное сообщение по границам абзацев под лимит Telegram."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for block in text.split("\n\n"):
        candidate = f"{buf}\n\n{block}" if buf else block
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            parts.append(buf)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        buf = block
    if buf:
        parts.append(buf)
    return parts


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def season_of(d: date) -> str:
    m = d.month
    if m in (12, 1, 2):
        return "winter"
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    return "autumn"
