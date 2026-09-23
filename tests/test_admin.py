"""Панель владельца: день по барберам, неделя сводкой, заявки, статистика, выгрузка."""
from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime
from datetime import time as dtime

import pytest
from aiogram.methods import EditMessageText, SendDocument
from aiogram.types import User

from bot import keyboards as kb
from bot.db import Barber, Booking
from bot.handlers.admin import MAX_TEXT, day_view
from tests.harness import CLIENT, OTHER, OWNER, OWNER_USER, Harness, at_day, book, make_settings, open_db


@pytest.fixture
async def db(tmp_path):
    database = await open_db(str(tmp_path / "admin.db"))
    await database.upsert_user(OTHER.id, OTHER.first_name, None)
    yield database
    await database.close()


def edited(calls) -> EditMessageText:
    return next(c for c in calls if isinstance(c, EditMessageText))


async def tomorrow_bookings(db, settings) -> None:
    artem, maxim = await db.barbers()
    await book(db, OTHER.id, artem.id, at_day(settings, 1, 10))
    await book(db, OTHER.id, artem.id, at_day(settings, 1, 12))
    await book(db, OTHER.id, maxim.id, at_day(settings, 1, 10))


async def test_day_view_groups_bookings_by_barber(db):
    h = Harness(make_settings(), db)
    await tomorrow_bookings(db, h.settings)
    text = edited(await h.feed(**h.callback(OWNER_USER, kb.AdmCb(action="day", arg="1").pack()))).text
    assert "3 зап." in text
    assert "Артём</b> · 2 зап." in text and "Максим</b> · 1 зап." in text
    assert text.index("Артём") < text.index("Максим")


async def test_week_view_is_a_summary_with_day_buttons(db):
    h = Harness(make_settings(), db)
    await tomorrow_bookings(db, h.settings)
    edit = edited(await h.feed(**h.callback(OWNER_USER, kb.AdmCb(action="week").pack())))
    assert "Артём 2, Максим 1" in edit.text
    day_buttons = [b.callback_data for row in edit.reply_markup.inline_keyboard for b in row]
    assert kb.AdmCb(action="day", arg="1").pack() in day_buttons


async def test_leads_view_hides_other_visitors_text_in_demo(db):
    h = Harness(make_settings(), db)
    await db.create_lead(OTHER.id, "мой номер 8 900 111-22-33", int(time.time()))
    await db.add_demo_clients([(-1, "Алексей К.", "+7 900 111-11-11")])
    await db.create_lead(-1, "Есть подарочные сертификаты?", int(time.time()))

    text = edited(await h.feed(**h.callback(CLIENT, kb.AdmCb(action="leads").pack()))).text
    assert "111-22-33" not in text and "текст скрыт в демо" in text
    assert "подарочные сертификаты" in text and "Алексей" not in text

    text = edited(await h.feed(**h.callback(OWNER_USER, kb.AdmCb(action="leads").pack()))).text
    assert "111-22-33" in text and "Алексей К." in text


async def test_stats_show_barber_load(db):
    h = Harness(make_settings(), db)
    await tomorrow_bookings(db, h.settings)
    text = edited(await h.feed(**h.callback(OWNER_USER, kb.AdmCb(action="stats").pack()))).text
    assert "Загрузка барберов: Артём 2 · Максим 1" in text


async def test_csv_has_barber_column(db):
    h = Harness(make_settings(), db)
    await tomorrow_bookings(db, h.settings)
    calls = await h.feed(**h.callback(OWNER_USER, kb.AdmCb(action="csv").pack()))
    document = next(c for c in calls if isinstance(c, SendDocument)).document
    rows = list(csv.reader(io.StringIO(document.data.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][4] == "барбер" and {r[4] for r in rows[1:]} == {"Артём", "Максим"}


def test_day_view_fits_telegram_limit():
    settings = make_settings()
    barbers = [Barber(i, f"Барбер {i}", "", frozenset(range(7)), True, i) for i in range(1, 6)]
    day = date(2026, 9, 24)
    start = int(datetime.combine(day, dtime(10), tzinfo=settings.schedule.tz).timestamp())
    items = [
        Booking(id=n, user_id=1, barber_id=n % 5 + 1, service_code="cut", start_at=start + n * 600,
                end_at=start + n * 600 + 3600, price=1500, status="confirmed", paid=0, hold_until=None,
                reminded_at=None, client_confirmed=False, cancelled_by=None, created_at=start,
                first_name="Константин Константинопольский", username="very_long_username_here",
                phone="+79001234567", barber_name=f"Барбер {n % 5 + 1}")
        for n in range(80)
    ]
    text = day_view(day, items, barbers, {}, set(), settings, viewer_id=OWNER, is_owner=True)
    assert len(text) <= MAX_TEXT + 100 and "CSV" in text


async def test_masked_names_are_escaped_for_other_visitors(db):
    """Посетитель с «<» в имени не должен ломать панель остальным: Telegram отверг бы такой HTML."""
    h = Harness(make_settings(), db)
    odd = User(id=3, is_bot=False, first_name="<Тёма>")
    await db.upsert_user(odd.id, odd.first_name, None)
    artem, _ = await db.barbers()
    booking = await book(db, odd.id, artem.id, at_day(h.settings, 1, 12))
    await db.create_lead(odd.id, "вопрос про бороду", int(time.time()))
    day = edited(await h.feed(**h.callback(CLIENT, kb.AdmCb(action="day", arg="1").pack()))).text
    leads = edited(await h.feed(**h.callback(CLIENT, kb.AdmCb(action="leads").pack()))).text
    card = edited(await h.feed(**h.callback(CLIENT, kb.AdmCb(action="card", arg=str(booking.id)).pack()))).text
    for text in (day, leads, card):
        assert "&lt;***" in text and "<***" not in text
