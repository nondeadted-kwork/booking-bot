"""Сквозные сценарии без реального Telegram: запись с выбором барбера, оплата, панель, сбои."""
from __future__ import annotations

import sqlite3

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    EditMessageText,
    SendDocument,
    SendInvoice,
    SetMyCommands,
    SetMyDescription,
    SetMyShortDescription,
)
from aiogram.types import Contact, PreCheckoutQuery, SuccessfulPayment, User

from bot import app, reminders
from bot import keyboards as kb
from bot.config import SERVICES_BY_CODE
from tests.harness import (
    CLIENT,
    ONE_BARBER,
    OTHER,
    OWNER,
    OWNER_USER,
    Harness,
    at_day,
    book,
    buttons,
    first,
    make_settings,
    open_db,
    sent_to,
    texts_of,
)


@pytest.fixture
async def db(tmp_path):
    database = await open_db(str(tmp_path / "flow.db"))
    yield database
    await database.close()


async def walk_to_confirm(h: Harness, user: User, barber: str = "first") -> str:
    """/start → Записаться → услуга → барбер → день → время. Возвращает callback кнопки подтверждения.

    barber: first (первый в списке), any («Любой свободный»), skip (мастер один, шага выбора нет).
    """
    calls = await h.feed(**h.message(user, "/start"))
    assert "Здравствуйте" in calls[0].text

    calls = await h.feed(**h.message(user, kb.BTN_BOOK))
    service_cb = first(calls[0], "svc")

    calls = await h.feed(**h.callback(user, service_cb))
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    if barber == "skip":
        assert not any(d.startswith("brb:") for d in buttons(edit))
    else:
        assert "Выберите барбера" in edit.text
        options = [d for d in buttons(edit) if d.startswith("brb:")]
        calls = await h.feed(**h.callback(user, options[-1] if barber == "any" else options[0]))
        edit = next(c for c in calls if isinstance(c, EditMessageText))
    day_cb = first(edit, "day")

    calls = await h.feed(**h.callback(user, day_cb))
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    slot_cb = first(edit, "slot")

    calls = await h.feed(**h.callback(user, slot_cb))
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert "Проверьте запись" in edit.text
    return first(edit, "ok")


async def test_full_booking_flow_with_phone_and_reminder(db):
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)

    # Телефона ещё нет: бот его спрашивает
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    assert "номер телефона" in texts_of(calls)

    contact = Contact(phone_number="+79001234567", first_name="Иван", user_id=CLIENT.id)
    calls = await h.feed(**h.message(CLIENT, contact=contact))
    assert "Вы записаны" in texts_of(calls) and "✂️ Артём" in texts_of(calls)
    [to_owner] = sent_to(calls, OWNER)
    assert "Новая запись" in to_owner.text and "+79001234567" in to_owner.text and "Артём" in to_owner.text

    [booking] = await db.user_upcoming(CLIENT.id, 0)
    assert booking.status == "confirmed"

    # Второй клиент пытается взять то же время у того же барбера по старой кнопке
    await db.upsert_user(OTHER.id, OTHER.first_name, None)
    await db.set_phone(OTHER.id, "")
    calls = await h.feed(**h.callback(OTHER, confirm_cb))
    assert "только что заняли" in texts_of(calls)

    # Напоминание уходит за час и только один раз
    await reminders.tick(h.bot, db, h.settings, now=booking.start_at - 30 * 60)
    sent = h.session.take()
    assert len(sent) == 1 and "Напоминание" in sent[0].text and sent[0].chat_id == CLIENT.id
    await reminders.tick(h.bot, db, h.settings, now=booking.start_at - 29 * 60)
    assert h.session.take() == []


async def test_any_barber_gets_a_concrete_barber(db):
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT, barber="any")
    assert kb.ConfirmCb.unpack(confirm_cb).m == 0
    await db.set_phone(CLIENT.id, "")
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    [booking] = await db.user_upcoming(CLIENT.id, 0)
    assert booking.barber_name in ("Артём", "Максим")
    assert f"✂️ {booking.barber_name}" in texts_of(calls)


async def test_any_barber_falls_back_to_second_when_first_is_taken(db):
    """Оба свободны, первого занимают, пока клиент думает: запись достаётся второму."""
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT, barber="any")
    data = kb.ConfirmCb.unpack(confirm_cb)
    service = SERVICES_BY_CODE[data.code]
    artem, maxim = await db.barbers()
    await db.upsert_user(OTHER.id, OTHER.first_name, None)
    await db.create_booking(user_id=OTHER.id, barber_id=artem.id, service_code=service.code, start_at=data.ts,
                            end_at=data.ts + service.minutes * 60, price=service.price, now=data.ts - 7200)
    await db.set_phone(CLIENT.id, "")
    await h.feed(**h.callback(CLIENT, confirm_cb))
    [booking] = await db.user_upcoming(CLIENT.id, 0)
    assert booking.barber_id == maxim.id


async def test_single_barber_skips_choice(tmp_path):
    db = await open_db(str(tmp_path / "one.db"), ONE_BARBER)
    try:
        h = Harness(make_settings(), db)
        confirm_cb = await walk_to_confirm(h, CLIENT, barber="skip")
        await db.set_phone(CLIENT.id, "")
        calls = await h.feed(**h.callback(CLIENT, confirm_cb))
        assert "Вы записаны" in texts_of(calls)
    finally:
        await db.close()


async def test_hidden_barber_button_is_refused(db):
    h = Harness(make_settings(), db)
    await h.feed(**h.message(CLIENT, "/start"))
    calls = await h.feed(**h.message(CLIENT, kb.BTN_BOOK))
    calls = await h.feed(**h.callback(CLIENT, first(calls[0], "svc")))
    artem_cb = first(next(c for c in calls if isinstance(c, EditMessageText)), "brb")
    artem, _ = await db.barbers()
    await db.update_barber(artem.id, active=False)
    calls = await h.feed(**h.callback(CLIENT, artem_cb))
    alert = next(c for c in calls if isinstance(c, AnswerCallbackQuery))
    assert "больше не принимает" in alert.text


async def test_menu_button_escapes_phone_form(db):
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    assert "номер телефона" in texts_of(calls)
    calls = await h.feed(**h.message(CLIENT, kb.BTN_MY))
    assert "нет предстоящих записей" in texts_of(calls)
    calls = await h.feed(**h.message(CLIENT, "привет"))
    assert "понимаю кнопки меню" in texts_of(calls)


async def test_demo_admin_panel_masks_other_clients(db):
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)
    await db.set_phone(CLIENT.id, "")
    await h.feed(**h.callback(CLIENT, confirm_cb))
    [booking] = await db.user_upcoming(CLIENT.id, 0)

    calls = await h.feed(**h.message(OTHER, kb.BTN_ADMIN))
    assert "Панель владельца" in calls[0].text and "Демо-режим" in calls[0].text

    calls = await h.feed(**h.message(OTHER, f"/b{booking.id}"))
    assert "И***" in calls[0].text and "Иван" not in calls[0].text
    assert not any(d.startswith("adm:cancel") for d in buttons(calls[0]))

    calls = await h.feed(**h.callback(OTHER, kb.AdmCb(action="toggle", arg="2030-01-01").pack()))
    alert = next(c for c in calls if isinstance(c, AnswerCallbackQuery))
    assert "демо" in alert.text.lower()

    calls = await h.feed(**h.callback(OTHER, kb.AdmCb(action="csv").pack()))
    assert any(isinstance(c, SendDocument) for c in calls)


async def test_online_payment_flow(db):
    h = Harness(make_settings(payment_token="381764678:TEST:1"), db)
    await db.upsert_user(CLIENT.id, CLIENT.first_name, CLIENT.username)
    await db.set_phone(CLIENT.id, "+79001234567")
    confirm_cb = await walk_to_confirm(h, CLIENT)
    pay_cb = confirm_cb.rsplit(":", 1)[0] + ":1"

    calls = await h.feed(**h.callback(CLIENT, pay_cb))
    invoice = next(c for c in calls if isinstance(c, SendInvoice))
    assert invoice.currency == "RUB" and invoice.prices[0].amount > 0 and "барбер Артём" in invoice.description

    query = PreCheckoutQuery(id="pc", from_user=CLIENT, currency="RUB",
                             total_amount=invoice.prices[0].amount, invoice_payload=invoice.payload)
    calls = await h.feed(pre_checkout_query=query)
    answer = next(c for c in calls if isinstance(c, AnswerPreCheckoutQuery))
    assert answer.ok is True

    payment = SuccessfulPayment(currency="RUB", total_amount=invoice.prices[0].amount,
                                invoice_payload=invoice.payload, telegram_payment_charge_id="tg",
                                provider_payment_charge_id="prov")
    calls = await h.feed(**h.message(CLIENT, successful_payment=payment))
    assert "Оплата прошла" in texts_of(calls)
    [booking] = await db.user_upcoming(CLIENT.id, 0)
    assert booking.paid == invoice.prices[0].amount


async def test_expired_hold_rejects_payment(db):
    h = Harness(make_settings(payment_token="381764678:TEST:1"), db)
    await db.upsert_user(CLIENT.id, CLIENT.first_name, None)
    await db.set_phone(CLIENT.id, "")
    confirm_cb = await walk_to_confirm(h, CLIENT)
    calls = await h.feed(**h.callback(CLIENT, confirm_cb.rsplit(":", 1)[0] + ":1"))
    invoice = next(c for c in calls if isinstance(c, SendInvoice))

    await db.conn.execute("UPDATE bookings SET hold_until = 1")  # бронь истекла
    query = PreCheckoutQuery(id="pc", from_user=CLIENT, currency="RUB",
                             total_amount=invoice.prices[0].amount, invoice_payload=invoice.payload)
    calls = await h.feed(pre_checkout_query=query)
    answer = next(c for c in calls if isinstance(c, AnswerPreCheckoutQuery))
    assert answer.ok is False and "истекла" in answer.error_message


async def test_garbage_callback_does_not_crash(db):
    h = Harness(make_settings(), db)
    calls = await h.feed(**h.callback(CLIENT, "slot:cut:not-a-number"))
    assert any(isinstance(c, AnswerCallbackQuery) and "устарела" in (c.text or "") for c in calls)


async def test_database_failure_is_reported_to_client_and_owner(db, monkeypatch):
    """«Уронили» базу посреди записи: клиент видит извинение, владелец алерт, бот живёт дальше."""
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)
    await db.set_phone(CLIENT.id, "")

    async def broken(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "create_booking", broken)
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    to_owner = sent_to(calls, OWNER)
    assert to_owner and "database is locked" in to_owner[0].text
    assert any(isinstance(c, AnswerCallbackQuery) and "пошло не так" in (c.text or "") for c in calls)

    monkeypatch.undo()  # база «ожила»: следующая попытка проходит
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    assert "Вы записаны" in texts_of(calls)


async def test_bot_profile_is_set_on_start(db):
    h = Harness(make_settings(), db)
    await app.set_profile(h.bot, h.settings)
    calls = h.session.take()
    description = next(c for c in calls if isinstance(c, SetMyDescription))
    assert "Демо бота записи" in description.description
    assert any(isinstance(c, SetMyShortDescription) for c in calls)
    assert any(isinstance(c, SetMyCommands) for c in calls)


async def test_bot_profile_failure_does_not_stop_start(db):
    h = Harness(make_settings(), db)

    async def refuse(bot, method, timeout=None):
        raise TelegramBadRequest(method=method, message="Bad Request: description is too long")

    h.session.make_request = refuse
    await app.set_profile(h.bot, h.settings)  # не падает, только пишет предупреждение в лог


async def test_demo_visitor_sees_what_owner_got(db):
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)
    await db.set_phone(CLIENT.id, "")
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    [to_owner] = sent_to(calls, OWNER)
    [copy] = [c for c in sent_to(calls, CLIENT.id) if "владелец получил" in c.text]
    assert to_owner.text in copy.text


async def test_no_copy_without_demo_and_no_copy_to_owner(db):
    h = Harness(make_settings(demo_mode=False), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)
    await db.set_phone(CLIENT.id, "")
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    assert not any("владелец получил" in c.text for c in sent_to(calls, CLIENT.id))

    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, OWNER_USER)
    await db.set_phone(OWNER, "")
    calls = await h.feed(**h.callback(OWNER_USER, confirm_cb))
    assert len(sent_to(calls, OWNER)) == 1  # одно уведомление, без копии самому себе


async def test_demo_reminder_button_does_not_cancel_the_real_reminder(db):
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)
    await db.set_phone(CLIENT.id, "")
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    card = next(c for c in calls if isinstance(c, EditMessageText) and "Вы записаны" in c.text)
    remind_cb = next(d for d in buttons(card) if d.startswith("my:remind"))
    [booking] = await db.user_upcoming(CLIENT.id, 0)

    calls = await h.feed(**h.callback(CLIENT, remind_cb))
    [reminder] = sent_to(calls, CLIENT.id)
    assert "Напоминание" in reminder.text
    assert (await db.get_booking(booking.id)).reminded_at == booking.reminded_at

    calls = await h.feed(**h.callback(CLIENT, next(d for d in buttons(reminder) if d.startswith("my:come"))))
    assert any("владелец получил" in c.text and "подтвердил" in c.text for c in sent_to(calls, CLIENT.id))


async def test_no_reminder_button_without_demo(db):
    h = Harness(make_settings(demo_mode=False), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)
    await db.set_phone(CLIENT.id, "")
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    card = next(c for c in calls if isinstance(c, EditMessageText) and "Вы записаны" in c.text)
    assert not any(d.startswith("my:remind") for d in buttons(card))


async def test_my_bookings_has_reminder_button_for_the_nearest_in_demo(db):
    h = Harness(make_settings(), db)
    await h.feed(**h.message(CLIENT, "/start"))
    await book(db, CLIENT.id, 1, at_day(h.settings, 3, 12))
    nearest = await book(db, CLIENT.id, 1, at_day(h.settings, 2, 12))

    [listing] = sent_to(await h.feed(**h.message(CLIENT, kb.BTN_MY)), CLIENT.id)
    remind = [d for d in buttons(listing) if d.startswith("my:remind")]
    assert remind == [kb.MyCb(action="remind", id=nearest.id).pack()]

    [reminder] = sent_to(await h.feed(**h.callback(CLIENT, remind[0])), CLIENT.id)
    assert "Напоминание" in reminder.text
    assert any(d.startswith("my:come") for d in buttons(reminder))


async def test_my_bookings_has_no_reminder_button_without_demo(db):
    h = Harness(make_settings(demo_mode=False), db)
    await h.feed(**h.message(CLIENT, "/start"))
    await book(db, CLIENT.id, 1, at_day(h.settings, 2, 12))
    [listing] = sent_to(await h.feed(**h.message(CLIENT, kb.BTN_MY)), CLIENT.id)
    assert not any(d.startswith("my:remind") for d in buttons(listing))


async def test_demo_start_text_has_a_plan(db):
    h = Harness(make_settings(), db)
    calls = await h.feed(**h.message(CLIENT, "/start"))
    assert "Что попробовать" in calls[0].text and "Показать напоминание" in calls[0].text



async def test_double_tap_on_confirm_keeps_the_success_card(db):
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)
    await db.set_phone(CLIENT.id, "")
    await h.feed(**h.callback(CLIENT, confirm_cb))
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    assert "Вы записаны" in texts_of(calls) and "заняли" not in texts_of(calls)
    assert len(await db.user_upcoming(CLIENT.id, 0)) == 1
    assert sent_to(calls, OWNER) == []  # вторая «Новая запись» владельцу не уходит


async def test_double_tap_with_any_barber_books_once(db):
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT, barber="any")
    await db.set_phone(CLIENT.id, "")
    await h.feed(**h.callback(CLIENT, confirm_cb))
    await h.feed(**h.callback(CLIENT, confirm_cb))
    assert len(await db.user_upcoming(CLIENT.id, 0)) == 1


async def test_come_and_cancel_notifications_name_the_barber(db):
    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)
    await db.set_phone(CLIENT.id, "")
    await h.feed(**h.callback(CLIENT, confirm_cb))
    [booking] = await db.user_upcoming(CLIENT.id, 0)
    calls = await h.feed(**h.callback(CLIENT, kb.MyCb(action="come", id=booking.id).pack()))
    [to_owner] = sent_to(calls, OWNER)
    assert "Артём" in to_owner.text
    calls = await h.feed(**h.callback(CLIENT, kb.MyCb(action="cancel_yes", id=booking.id).pack()))
    [to_owner] = sent_to(calls, OWNER)
    assert "Артём" in to_owner.text

