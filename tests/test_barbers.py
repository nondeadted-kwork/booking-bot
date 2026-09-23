"""Барберы в панели: добавление, замок для посетителя, запреты при записях, скрытие, переименование."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText

from bot import keyboards as kb
from bot.db import Booking
from bot.handlers.barbers import conflict_text
from tests.harness import CLIENT, OTHER, OWNER_USER, Harness, at_day, book, first, make_settings, open_db, texts_of


@pytest.fixture
async def db(tmp_path):
    database = await open_db(str(tmp_path / "barbers.db"))
    await database.upsert_user(OTHER.id, OTHER.first_name, None)
    yield database
    await database.close()


def alert_of(calls) -> str:
    return next(c.text for c in calls if isinstance(c, AnswerCallbackQuery) and c.text)


async def add_barber_via_bot(h: Harness, user, name: str = "Олег", about: str = "Фейды и бритьё"):
    await h.feed(**h.callback(user, kb.BarbCb(action="new").pack()))
    await h.feed(**h.message(user, name))
    await h.feed(**h.message(user, about))
    await h.feed(**h.callback(user, kb.BarbCb(action="ndone").pack()))
    return await h.feed(**h.callback(user, kb.BarbCb(action="save").pack()))


async def test_owner_adds_barber_and_clients_can_pick_him(db):
    h = Harness(make_settings(), db)
    calls = await add_barber_via_bot(h, OWNER_USER)
    assert "Сохранено" in texts_of(calls)
    assert [b.name for b in await db.barbers()] == ["Артём", "Максим", "Олег"]

    await h.feed(**h.message(CLIENT, "/start"))
    calls = await h.feed(**h.message(CLIENT, kb.BTN_BOOK))
    calls = await h.feed(**h.callback(CLIENT, first(calls[0], "svc")))
    assert "Олег" in next(c for c in calls if isinstance(c, EditMessageText)).text


async def test_visitor_walks_through_but_cannot_save(db):
    h = Harness(make_settings(), db)
    calls = await add_barber_via_bot(h, CLIENT)
    assert "демо" in alert_of(calls).lower()
    assert len(await db.barbers()) == 2


async def test_double_tap_on_save_creates_one_barber(db):
    h = Harness(make_settings(), db)
    await add_barber_via_bot(h, OWNER_USER)
    calls = await h.feed(**h.callback(OWNER_USER, kb.BarbCb(action="save").pack()))
    assert "потерялись" in alert_of(calls)
    assert len(await db.barbers()) == 3


async def test_workday_with_bookings_cannot_be_removed(db):
    h = Harness(make_settings(), db)
    artem, _ = await db.barbers()
    start = at_day(h.settings, 1, 12)
    await book(db, OTHER.id, artem.id, start)
    weekday = datetime.fromtimestamp(start, h.settings.schedule.tz).weekday()

    calls = await h.feed(**h.callback(OWNER_USER, kb.BarbCb(action="day", id=artem.id, arg=str(weekday)).pack()))
    alert = alert_of(calls)
    assert "есть записи" in alert and len(alert) <= 200
    assert weekday in (await db.get_barber(artem.id)).workdays

    free_weekday = (weekday + 3) % 7
    await h.feed(**h.callback(OWNER_USER, kb.BarbCb(action="day", id=artem.id, arg=str(free_weekday)).pack()))
    assert free_weekday not in (await db.get_barber(artem.id)).workdays


async def test_vacation_blocked_on_booked_date_and_set_on_free_one(db):
    h = Harness(make_settings(), db)
    artem, _ = await db.barbers()
    await book(db, OTHER.id, artem.id, at_day(h.settings, 1, 12))
    tomorrow = datetime.now(h.settings.schedule.tz).date() + timedelta(days=1)
    after = tomorrow + timedelta(days=1)

    calls = await h.feed(**h.callback(OWNER_USER, kb.BarbCb(action="offday", id=artem.id,
                                                             arg=tomorrow.isoformat()).pack()))
    assert "есть записи" in alert_of(calls)
    await h.feed(**h.callback(OWNER_USER, kb.BarbCb(action="offday", id=artem.id, arg=after.isoformat()).pack()))
    assert await db.days_off() == {artem.id: frozenset({after})}


async def test_last_active_barber_cannot_be_hidden(db):
    h = Harness(make_settings(), db)
    artem, maxim = await db.barbers()
    await h.feed(**h.callback(OWNER_USER, kb.BarbCb(action="hide", id=artem.id).pack()))
    assert not (await db.get_barber(artem.id)).active
    calls = await h.feed(**h.callback(OWNER_USER, kb.BarbCb(action="hide", id=maxim.id).pack()))
    assert "последний" in alert_of(calls)
    assert (await db.get_barber(maxim.id)).active


async def test_visitor_sees_screens_but_changes_are_locked(db):
    h = Harness(make_settings(), db)
    artem, _ = await db.barbers()
    for action, arg in (("day", "0"), ("offday", "2030-01-01"), ("hide", ""), ("name", "")):
        calls = await h.feed(**h.callback(CLIENT, kb.BarbCb(action=action, id=artem.id, arg=arg).pack()))
        assert "демо" in alert_of(calls).lower(), action
    calls = await h.feed(**h.callback(CLIENT, kb.BarbCb(action="days", id=artem.id).pack()))
    assert any(isinstance(c, EditMessageText) for c in calls)
    assert (await db.get_barber(artem.id)).workdays == frozenset(range(7))


async def test_owner_renames_barber_and_html_is_escaped(db):
    h = Harness(make_settings(), db)
    artem, _ = await db.barbers()
    await h.feed(**h.callback(OWNER_USER, kb.BarbCb(action="name", id=artem.id).pack()))
    calls = await h.feed(**h.message(OWNER_USER, "<Тёма>"))
    assert (await db.get_barber(artem.id)).name == "<Тёма>"
    assert "&lt;Тёма&gt;" in texts_of(calls)


def test_conflict_text_fits_alert_limit():
    tz = make_settings().schedule.tz
    start = int(datetime(2026, 9, 24, 10, 0, tzinfo=tz).timestamp())
    clash = [Booking(id=i, user_id=1, barber_id=1, service_code="cut", start_at=start + i * 3600,
                     end_at=start + i * 3600 + 3600, price=1500, status="confirmed", paid=0, hold_until=None,
                     reminded_at=None, client_confirmed=False, cancelled_by=None, created_at=start,
                     first_name="Иван", username=None, phone=None, barber_name="Артём") for i in range(10)]
    assert len(conflict_text("На " + "очень длинный заголовок " * 10, clash, tz)) <= 200
    short = conflict_text("На чт у барбера есть записи", clash, tz)
    assert "24.09 10:00" in short and "и ещё 7" in short
