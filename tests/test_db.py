import asyncio
import sqlite3
from datetime import date

import pytest

from bot.config import DEMO_BARBERS, BarberSeed
from bot.db import Database, SlotTaken

NOW = 1_790_000_000
HOUR = 3600
EVERY_DAY = frozenset(range(7))
TWO = (BarberSeed("Артём", "Фейды", EVERY_DAY), BarberSeed("Максим", "Борода", EVERY_DAY))


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    await database.ensure_barbers(TWO)
    for uid in (1, 2, 3):
        await database.upsert_user(uid, f"User{uid}", None)
    yield database
    await database.close()


def booking(user_id: int, start: int, minutes: int = 60, **kw):
    return dict(user_id=user_id, service_code="cut", start_at=start, end_at=start + minutes * 60,
                price=1500, now=NOW, **kw)


async def test_overlapping_booking_is_rejected(db):
    await db.create_booking(**booking(1, NOW + 2 * HOUR))
    with pytest.raises(SlotTaken):
        await db.create_booking(**booking(2, NOW + 2 * HOUR + 1800))
    # Впритык после — можно
    await db.create_booking(**booking(2, NOW + 3 * HOUR))


async def test_race_for_one_slot_has_single_winner(db):
    """10 человек одновременно жмут одно и то же время — запись получает ровно один."""
    async def attempt(uid):
        try:
            await db.create_booking(**booking(uid % 3 + 1, NOW + 5 * HOUR))
            return True
        except SlotTaken:
            return False

    results = await asyncio.gather(*(attempt(i) for i in range(10)))
    assert results.count(True) == 1


async def test_unpaid_hold_expires_and_frees_slot(db):
    b = await db.create_booking(**booking(1, NOW + 2 * HOUR, hold_until=NOW + 900))
    assert b.status == "pending_payment"
    with pytest.raises(SlotTaken):
        await db.create_booking(**booking(2, NOW + 2 * HOUR))

    expired = await db.expire_holds(NOW + 901)
    assert [x.id for x in expired] == [b.id]
    later = dict(booking(2, NOW + 2 * HOUR), now=NOW + 901)
    assert (await db.create_booking(**later)).status == "confirmed"


async def test_payment_after_slot_was_taken_is_conflict(db):
    b = await db.create_booking(**booking(1, NOW + 2 * HOUR, hold_until=NOW + 900))
    await db.expire_holds(NOW + 901)
    await db.create_booking(**dict(booking(2, NOW + 2 * HOUR), now=NOW + 901))
    assert await db.confirm_payment(b.id, "charge", 150000, NOW + 902) == "conflict"
    assert (await db.get_booking(b.id)).status == "cancelled"


async def test_payment_confirms_and_is_idempotent(db):
    b = await db.create_booking(**booking(1, NOW + 2 * HOUR, hold_until=NOW + 900))
    assert await db.confirm_payment(b.id, "charge", 150000, NOW + 60) == "ok"
    assert await db.confirm_payment(b.id, "charge", 150000, NOW + 61) == "already"
    saved = await db.get_booking(b.id)
    assert saved.status == "confirmed" and saved.paid == 150000


async def test_reminders_are_sent_once_and_only_in_window(db):
    soon = await db.create_booking(**booking(1, NOW + 50 * 60))
    await db.create_booking(**booking(2, NOW + 5 * HOUR))
    due = await db.due_reminders(NOW, 60 * 60)
    assert [b.id for b in due] == [soon.id]
    await db.mark_reminded(soon.id, NOW)
    assert await db.due_reminders(NOW, 60 * 60) == []


async def test_cancelled_booking_frees_slot(db):
    b = await db.create_booking(**booking(1, NOW + 2 * HOUR))
    assert await db.cancel_booking(b.id, by="client")
    assert not await db.cancel_booking(b.id, by="client")  # повторная отмена — no-op
    await db.create_booking(**booking(2, NOW + 2 * HOUR))


async def test_closed_day_toggle(db):
    assert await db.toggle_closed_day("2026-09-23") is True
    assert await db.closed_days() == {"2026-09-23"}
    assert await db.toggle_closed_day("2026-09-23") is False
    assert await db.closed_days() == set()


async def test_ensure_barbers_only_fills_an_empty_base(db):
    await db.ensure_barbers(DEMO_BARBERS)  # барберы уже есть: ничего не добавляется
    barbers = await db.barbers()
    assert [b.name for b in barbers] == ["Артём", "Максим"]
    assert barbers[0].workdays == EVERY_DAY and barbers[0].about == "Фейды" and barbers[0].active


async def test_add_update_and_hide_barber(db):
    oleg = await db.add_barber("Олег", "Фейды и бритьё", [0, 2, 4])
    assert oleg.sort == 2 and oleg.workdays == frozenset({0, 2, 4}) and oleg.active
    await db.update_barber(oleg.id, name="Олег П.", workdays=[1], active=False)
    assert [b.name for b in await db.barbers()] == ["Артём", "Максим"]
    hidden = (await db.barbers(include_hidden=True))[-1]
    assert (hidden.name, hidden.workdays, hidden.active) == ("Олег П.", frozenset({1}), False)


async def test_barber_day_off_toggle(db):
    artem = (await db.barbers())[0]
    assert await db.toggle_day_off(artem.id, "2026-09-24") is True
    assert await db.days_off() == {artem.id: frozenset({date(2026, 9, 24)})}
    assert await db.toggle_day_off(artem.id, "2026-09-24") is False
    assert await db.days_off() == {}


OLD_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, first_name TEXT NOT NULL, username TEXT, phone TEXT,
    created_at INTEGER NOT NULL DEFAULT 0, is_blocked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, service_code TEXT NOT NULL,
    start_at INTEGER NOT NULL, end_at INTEGER NOT NULL, price INTEGER NOT NULL, status TEXT NOT NULL,
    paid INTEGER NOT NULL DEFAULT 0, charge_id TEXT, hold_until INTEGER, reminded_at INTEGER,
    client_confirmed INTEGER NOT NULL DEFAULT 0, cancelled_by TEXT, created_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE closed_days (day TEXT PRIMARY KEY);
INSERT INTO users (id, first_name) VALUES (1, 'Иван');
INSERT INTO bookings (user_id, service_code, start_at, end_at, price, status)
VALUES (1, 'cut', 100, 3700, 1500, 'confirmed');
"""


async def test_old_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.close()
    database = Database(str(path))
    await database.connect()
    try:
        await database.ensure_barbers((BarberSeed("Мастер", "", EVERY_DAY),))
        [booking] = await database.all_bookings()
        assert (booking.user_id, booking.barber_name) == (1, "Мастер")
    finally:
        await database.close()
