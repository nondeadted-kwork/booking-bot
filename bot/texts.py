"""Форматирование дат, цен и карточек записей."""
from __future__ import annotations

from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from .config import SERVICES_BY_CODE
from .db import Booking

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(1, "окно", "окна", "окон") → «1 окно», 3 → «3 окна», 11 → «11 окон»."""
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def price(rub: int) -> str:
    return f"{rub:,}".replace(",", " ") + " ₽"


def day_long(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def day_short(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


def local(ts: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def when(b: Booking, tz: ZoneInfo) -> str:
    start, end = local(b.start_at, tz), local(b.end_at, tz)
    return f"{day_long(start.date())}, {start:%H:%M}–{end:%H:%M}"


def service_title(code: str) -> str:
    s = SERVICES_BY_CODE.get(code)
    return s.title if s else code


def mask(name: str) -> str:
    return (name[:1] or "?") + "***"


def client_line(b: Booking, hide: bool) -> str:
    if hide:
        return mask(b.first_name)
    parts = [escape(b.first_name)]
    if b.username:
        parts.append(f"@{b.username}")
    if b.phone:
        parts.append(escape(b.phone))
    return " · ".join(parts)


def payment_line(b: Booking) -> str:
    if b.paid:
        return f"💳 оплачено онлайн {price(b.paid // 100)}"
    return f"💰 {price(b.price)} — оплата на месте"


def booking_card(b: Booking, tz: ZoneInfo, address: str) -> str:
    return (
        f"💈 <b>{escape(service_title(b.service_code))}</b>\n"
        f"🗓 {when(b, tz)}\n"
        f"{payment_line(b)}\n"
        f"📍 {escape(address)}"
    )


def admin_line(b: Booking, tz: ZoneInfo, hide: bool) -> str:
    start, end = local(b.start_at, tz), local(b.end_at, tz)
    flags = ""
    if b.paid:
        flags += " 💳"
    if b.client_confirmed:
        flags += " ✅"
    if b.status == "pending_payment":
        flags += " ⏳"
    return (
        f"<b>{start:%H:%M}–{end:%H:%M}</b> {escape(service_title(b.service_code))} · "
        f"{client_line(b, hide)}{flags}  /b{b.id}"
    )
