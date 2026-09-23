"""Демо-данные: ненастоящие клиенты, история за 30 дней, записи на неделю вперёд и заявки.

Работает только при DEMO_MODE=1. Генератор детерминированный (seed от даты и барбера),
последний заполненный день хранится в meta, поэтому перезапуск ничего не дублирует.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from .config import SERVICES, Schedule, Service, Settings
from .db import Database, SeedBooking
from .slots import day_range, free_slots

FIRST_NAMES = ("Алексей", "Дмитрий", "Иван", "Сергей", "Никита", "Павел", "Егор", "Михаил", "Артур", "Роман",
               "Кирилл", "Денис", "Андрей", "Владимир", "Олег", "Тимур", "Георгий", "Константин", "Виктор", "Антон")
INITIALS = "АБВГДЕЖЗИКЛМНОПРСТУФЧШЯ"
CLIENTS = 100
HISTORY_DAYS = 30
MIN_FREE_WINDOWS = 3  # столько часовых окон остаётся у барбера в каждый будущий день, начиная с завтра
SERVICE_WEIGHTS = {"cut": 45, "combo": 25, "beard": 20, "kids": 10}
DEMO_LEADS = (  # (сколько дней назад, текст)
    (6, "Можно записать сына 7 лет в субботу утром?"),
    (5, "Есть подарочные сертификаты? Хочу подарить брату"),
    (4, "Хочу к Максиму на бороду, но в выходные нет окон. Можно в пятницу после 19:00?"),
    (3, "Сколько стоит камуфляж седины?"),
    (2, "Работаете 31 декабря? Хочу записаться на утро"),
    (1, "Можно прийти вдвоём с другом на одно время?"),
)


def demo_clients() -> list[tuple[int, str, str]]:
    """(id, имя, телефон). Id отрицательные: у настоящих пользователей Telegram они положительные."""
    rng = random.Random("demo-clients")
    return [
        (-i, f"{rng.choice(FIRST_NAMES)} {rng.choice(INITIALS)}.",
         f"+7 900 1{rng.randint(10, 99)}-{rng.randint(10, 99)}-{rng.randint(10, 99)}")
        for i in range(1, CLIENTS + 1)
    ]


def seed_row(start: int, service: Service, barber_id: int, now: int, rng: random.Random) -> SeedBooking:
    past = start < now
    cancelled = past and rng.random() < 0.1
    paid = service.price * 100 if not cancelled and rng.random() < 0.4 else 0
    if past:
        created = start - rng.randint(1, 5) * 86400 - rng.randint(0, 36000)
    else:
        created = now - rng.randint(0, 3 * 86400)
    return SeedBooking(
        user_id=-rng.randint(1, CLIENTS),
        barber_id=barber_id,
        service_code=service.code,
        start_at=start,
        end_at=start + service.minutes * 60,
        price=service.price,
        status="cancelled" if cancelled else "confirmed",
        paid=paid,
        client_confirmed=not cancelled and rng.random() < (0.5 if past else 0.2),
        created_at=min(created, now),
    )


def plan_day(day: date, barber_id: int, schedule: Schedule, now: int, fill: float,
             rng: random.Random) -> list[SeedBooking]:
    """Записи одного барбера на один день: занимают примерно долю `fill` рабочего времени."""
    start, end = day_range(day, schedule)
    step = schedule.slot_step_min * 60
    budget = (end - start) * fill
    services = [s for s in SERVICES if s.code in SERVICE_WEIGHTS]
    weights = [SERVICE_WEIGHTS[s.code] for s in services]
    rows: list[SeedBooking] = []
    t, used = start, 0
    while t + step <= end and used < budget:
        if rng.random() < 0.55:
            service = rng.choices(services, weights)[0]
            length = service.minutes * 60
            if t + length <= end and used + length <= budget:
                rows.append(seed_row(t, service, barber_id, now, rng))
                t += length
                used += length
                continue
        t += step
    return rows


def keep_free(rows: list[SeedBooking], day: date, schedule: Schedule, rng: random.Random) -> list[SeedBooking]:
    """Убирает случайные записи, пока у барбера не останется MIN_FREE_WINDOWS часовых окон."""
    rows = list(rows)
    before_day = datetime.combine(day, schedule.work_start, tzinfo=schedule.tz) - timedelta(days=1)

    def windows() -> int:
        busy = [(r.start_at, r.end_at) for r in rows if r.status == "confirmed"]
        return len(free_slots(day, 60, busy, before_day, schedule))

    while rows and windows() < MIN_FREE_WINDOWS:
        rows.pop(rng.randrange(len(rows)))
    return rows


async def ensure_demo_data(db: Database, settings: Settings, now: int) -> int:
    """Дописывает демо-данные по сегодня + days_ahead включительно. Возвращает число новых записей."""
    sch = settings.schedule
    today = datetime.fromtimestamp(now, sch.tz).date()
    last = today + timedelta(days=sch.days_ahead)  # на день дальше, чем видят клиенты
    done = await db.get_meta("demo_seeded_until")
    if done is None:
        await db.add_demo_clients(demo_clients())
        await db.add_demo_leads([
            (-(i * 7 % CLIENTS + 1), text, now - days_ago * 86400 - (i + 2) * 3600)
            for i, (days_ago, text) in enumerate(DEMO_LEADS)
        ])
        day = today - timedelta(days=HISTORY_DAYS)
    else:
        day = date.fromisoformat(done) + timedelta(days=1)
    if day > last:
        return 0
    barbers = await db.barbers()
    off = await db.days_off()
    closed = await db.closed_days()
    added = 0
    while day <= last:
        rows: list[SeedBooking] = []
        if day.weekday() in sch.workdays and day.isoformat() not in closed:
            for b in barbers:
                if day.weekday() not in b.workdays or day in off.get(b.id, frozenset()):
                    continue
                rng = random.Random(f"{day.isoformat()}:{b.id}")
                fill = rng.uniform(0.3, 0.5) if day >= today else rng.uniform(0.5, 0.75)
                planned = plan_day(day, b.id, sch, now, fill, rng)
                rows += keep_free(planned, day, sch, rng) if day > today else planned
        added += await db.add_demo_day(day.isoformat(), rows, now)
        day += timedelta(days=1)
    return added
