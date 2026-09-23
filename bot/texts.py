"""Форматирование дат, цен, карточек записей и заявок."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from .config import SERVICES_BY_CODE, Schedule, Settings
from .db import Booking, Lead

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


def days_span(days: Iterable[int]) -> str:
    """{0,1,2,3,4} → «пн-пт», {0,1,4,5,6} → «пн, вт, пт-вс», все семь → «ежедневно»."""
    ds = sorted(set(days))
    if not ds:
        return "нет рабочих дней"
    if len(ds) == 7:
        return "ежедневно"
    runs: list[tuple[int, int]] = []
    start = prev = ds[0]
    for d in ds[1:]:
        if d != prev + 1:
            runs.append((start, prev))
            start = d
        prev = d
    runs.append((start, prev))
    parts: list[str] = []
    for a, b in runs:
        if b - a >= 2:
            parts.append(f"{WEEKDAYS[a]}-{WEEKDAYS[b]}")
        else:
            parts.extend(WEEKDAYS[x] for x in range(a, b + 1))
    return ", ".join(parts)


def schedule_text(schedule: Schedule) -> str:
    return f"{days_span(schedule.workdays)} {schedule.work_start:%H:%M}-{schedule.work_end:%H:%M}"


def nearest(t: datetime, today: date) -> str:
    """Ближайшее окно на кнопке барбера: «сегодня 15:30», «завтра 10:00», «пт 11:00»."""
    if t.date() == today:
        return f"сегодня {t:%H:%M}"
    if (t.date() - today).days == 1:
        return f"завтра {t:%H:%M}"
    return f"{WEEKDAYS[t.weekday()]} {t:%H:%M}"


def when(b: Booking, tz: ZoneInfo) -> str:
    start, end = local(b.start_at, tz), local(b.end_at, tz)
    return f"{day_long(start.date())}, {start:%H:%M}-{end:%H:%M}"


def service_title(code: str) -> str:
    s = SERVICES_BY_CODE.get(code)
    return s.title if s else code


def mask(name: str) -> str:
    return (name[:1] or "?") + "***"


def person_line(first_name: str, username: str | None, phone: str | None, hide: bool) -> str:
    """Имя · @username · телефон. В демо чужие данные скрыты: «И***»."""
    if hide:
        return escape(mask(first_name))  # первая буква имени тоже может быть «<» или «&»
    parts = [escape(first_name)]
    if username:
        parts.append(f"@{username}")
    if phone:
        parts.append(escape(phone))
    return " · ".join(parts)


def client_line(b: Booking, hide: bool) -> str:
    return person_line(b.first_name, b.username, b.phone, hide)


def lead_client_line(lead: Lead, hide: bool) -> str:
    return person_line(lead.first_name, lead.username, lead.phone, hide)


def payment_line(b: Booking) -> str:
    if b.paid:
        return f"💳 оплачено онлайн {price(b.paid // 100)}"
    return f"💰 {price(b.price)}, оплата на месте"


def booking_card(b: Booking, tz: ZoneInfo, address: str) -> str:
    barber = f"✂️ {escape(b.barber_name)}\n" if b.barber_name else ""
    return (
        f"💈 <b>{escape(service_title(b.service_code))}</b>\n"
        f"{barber}"
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
        f"<b>{start:%H:%M}-{end:%H:%M}</b> {escape(service_title(b.service_code))} · "
        f"{client_line(b, hide)}{flags}  /b{b.id}"
    )


def bot_description(settings: Settings) -> str:
    """Текст в пустом чате до кнопки «Старт». Telegram принимает до 512 символов."""
    if settings.demo_mode:
        return (
            "Демо бота записи для барбершопа.\n\n"
            "• запись к барберу на свободное время\n"
            "• оплата картой прямо в боте (тестовая)\n"
            "• напоминание за час до визита\n"
            "• заявки с вопросами\n"
            "• панель владельца: расписание, барберы, рассылка, статистика\n\n"
            "Нажмите «Старт» и запишитесь. Салон ненастоящий."
        )
    return (f"Запись в {settings.business_name}: выберите услугу, мастера и удобное время. "
            f"Напомним за час до визита.")[:512]


def bot_short_description(settings: Settings) -> str:
    """Короткое описание в профиле бота и в превью ссылки. До 120 символов."""
    if settings.demo_mode:
        return "Демо: запись к барберу, оплата в боте, напоминания и панель владельца."
    return f"Онлайн-запись в {settings.business_name}"[:120]
