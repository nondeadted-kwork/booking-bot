import asyncio

import pytest

from bot.db import Database, SlotTaken

NOW = 1_790_000_000
HOUR = 3600


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
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
