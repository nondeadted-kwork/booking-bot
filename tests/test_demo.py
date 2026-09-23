"""Демо-данные: не дублируются, оставляют свободные окна, не получают напоминаний и рассылок."""
from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pytest

from bot import availability, demo, reminders
from bot.config import DEMO_BARBERS, SERVICES_BY_CODE
from tests.harness import Harness, make_settings, open_db

MSK = ZoneInfo("Europe/Moscow")
NOW = int(datetime(2026, 9, 23, 12, 0, tzinfo=MSK).timestamp())  # среда
TODAY = datetime.fromtimestamp(NOW, MSK).date()


@pytest.fixture
async def db(tmp_path):
    database = await open_db(str(tmp_path / "demo.db"), DEMO_BARBERS)
    yield database
    await database.close()


async def count(db, table: str) -> int:
    return (await db._one(f"SELECT COUNT(*) AS n FROM {table}"))["n"]


async def test_seed_is_idempotent(db):
    settings = make_settings()
    assert await demo.ensure_demo_data(db, settings, NOW) > 0
    before = [await count(db, t) for t in ("bookings", "users", "leads")]
    assert await demo.ensure_demo_data(db, settings, NOW) == 0
    assert [await count(db, t) for t in ("bookings", "users", "leads")] == before
    assert before[1:] == [demo.CLIENTS, len(demo.DEMO_LEADS)]


async def test_every_future_barber_day_keeps_free_hours(db):
    settings = make_settings()
    await demo.ensure_demo_data(db, settings, NOW)
    now = datetime.fromtimestamp(NOW, MSK)
    cut = SERVICES_BY_CODE["cut"]
    for barber in await db.barbers():
        for i in range(1, settings.schedule.days_ahead):
            day = TODAY + timedelta(days=i)
            if day.weekday() in barber.workdays:
                free = await availability.free_times(db, settings, cut, barber.id, day, now)
                assert len(free) >= demo.MIN_FREE_WINDOWS, (barber.name, day)


async def test_history_fills_stats(db):
    await demo.ensure_demo_data(db, make_settings(), NOW)
    stats = await db.stats(NOW, NOW - 30 * 86400)
    assert stats["visits"] > 100 and stats["paid_online"] > 0 and stats["cancelled"] > 0
    assert [name for name, _ in await db.barber_load(NOW - 30 * 86400)] == ["Артём", "Максим", "Илья"]


async def test_fake_clients_get_no_reminders_or_broadcasts(db):
    await demo.ensure_demo_data(db, make_settings(), NOW)
    assert await db.due_reminders(NOW, 7 * 86400) == []
    assert await db.active_user_ids() == []


async def test_next_day_adds_only_the_new_day(db):
    settings = make_settings()
    await demo.ensure_demo_data(db, settings, NOW)
    before = await count(db, "bookings")
    added = await demo.ensure_demo_data(db, settings, NOW + 86400)
    assert added > 0 and await count(db, "bookings") == before + added
    expected = TODAY + timedelta(days=settings.schedule.days_ahead + 1)
    assert await db.get_meta("demo_seeded_until") == expected.isoformat()


async def test_hidden_barber_and_vacation_are_not_seeded(db):
    artem, _, ilya = await db.barbers()
    await db.update_barber(ilya.id, active=False)
    tomorrow = TODAY + timedelta(days=1)
    await db.toggle_day_off(artem.id, tomorrow.isoformat())
    await demo.ensure_demo_data(db, make_settings(), NOW)
    rows = await db._all("SELECT barber_id, start_at FROM bookings")
    assert all(r["barber_id"] != ilya.id for r in rows)
    start = int(datetime.combine(tomorrow, dtime(0), tzinfo=MSK).timestamp())
    assert not [r for r in rows if r["barber_id"] == artem.id and start <= r["start_at"] < start + 86400]


async def test_background_step_seeds_only_in_demo(tmp_path, db):
    prod = await open_db(str(tmp_path / "prod.db"))
    try:
        h = Harness(make_settings(demo_mode=False), prod)
        await reminders.step(h.bot, prod, h.settings, NOW)
        assert await count(prod, "bookings") == 0
    finally:
        await prod.close()
    h = Harness(make_settings(), db)
    await reminders.step(h.bot, db, h.settings, NOW)
    assert await count(db, "bookings") > 0
