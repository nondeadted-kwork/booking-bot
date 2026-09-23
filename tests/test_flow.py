"""Сквозной сценарий без реального Telegram: апдейты подаются в диспетчер,
а все запросы к Bot API перехватывает FakeSession."""
from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    EditMessageText,
    SendDocument,
    SendInvoice,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Contact,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
    User,
)

from bot import keyboards as kb
from bot import reminders
from bot.app import build_dispatcher
from bot.config import BarberSeed, Settings
from bot.db import Database
from bot.handlers import admin, client

OWNER = 1000
CLIENT = User(id=1, is_bot=False, first_name="Иван", username="ivan")
OTHER = User(id=2, is_bot=False, first_name="Пётр")


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._ids = itertools.count(100)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendInvoice, SendDocument, EditMessageText)):
            return Message(message_id=next(self._ids), date=datetime.now(),
                           chat=Chat(id=method.chat_id or 0, type="private"),
                           text=getattr(method, "text", None))
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs) -> AsyncGenerator[bytes, None]:
        yield b""

    def take(self) -> list[TelegramMethod]:
        calls, self.calls = self.calls, []
        return calls


class Harness:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.session = FakeSession()
        self.bot = Bot("123456:TEST", session=self.session)
        self.db = db
        self.settings = settings
        for router in (admin.router, client.router):  # роутеры — модульные синглтоны, отвязываем от прошлого теста
            router._parent_router = None
        self.dp = build_dispatcher(db, settings)
        self._ids = itertools.count(1)

    async def feed(self, **kwargs) -> list[TelegramMethod]:
        await self.dp.feed_update(self.bot, Update(update_id=next(self._ids), **kwargs))
        return self.session.take()

    def message(self, user: User, text: str | None = None, **extra) -> dict:
        return {"message": Message(message_id=next(self._ids), date=datetime.now(),
                                   chat=Chat(id=user.id, type="private"), from_user=user, text=text, **extra)}

    def callback(self, user: User, data: str) -> dict:
        msg = Message(message_id=next(self._ids), date=datetime.now(), chat=Chat(id=user.id, type="private"),
                      text="…")
        return {"callback_query": CallbackQuery(id=str(next(self._ids)), from_user=user, chat_instance="ci",
                                                message=msg, data=data)}


def buttons(call: TelegramMethod) -> list[str]:
    markup = call.reply_markup
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def first(call: TelegramMethod, prefix: str) -> str:
    return next(d for d in buttons(call) if d.startswith(prefix + ":"))


def texts_of(calls) -> str:
    return "\n".join(getattr(c, "text", "") or "" for c in calls)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "flow.db"))
    await database.connect()
    await database.ensure_barbers((BarberSeed("Мастер", "", frozenset(range(7))),))
    yield database
    await database.close()


def make_settings(**kw) -> Settings:
    return Settings(bot_token="123456:TEST", owner_ids=frozenset({OWNER}), demo_mode=True, **kw)


async def walk_to_confirm(h: Harness, user: User) -> str:
    """/start → Записаться → услуга → день → время. Возвращает callback кнопки подтверждения."""
    calls = await h.feed(**h.message(user, "/start"))
    assert "Здравствуйте" in calls[0].text

    calls = await h.feed(**h.message(user, kb.BTN_BOOK))
    service_cb = first(calls[0], "svc")

    calls = await h.feed(**h.callback(user, service_cb))
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

    # Телефона ещё нет — бот его спрашивает
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    assert "номер телефона" in texts_of(calls)

    contact = Contact(phone_number="+79001234567", first_name="Иван", user_id=CLIENT.id)
    calls = await h.feed(**h.message(CLIENT, contact=contact))
    all_text = texts_of(calls)
    assert "Вы записаны" in all_text
    owner_msgs = [c for c in calls if isinstance(c, SendMessage) and c.chat_id == OWNER]
    assert owner_msgs and "Новая запись" in owner_msgs[0].text and "+79001234567" in owner_msgs[0].text

    [booking] = await db.user_upcoming(CLIENT.id, 0)
    assert booking.status == "confirmed"

    # Второй клиент пытается взять то же время по старой кнопке
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
    pay_cb = confirm_cb.replace(":0", ":1") if confirm_cb.endswith(":0") else confirm_cb

    calls = await h.feed(**h.callback(CLIENT, pay_cb))
    invoice = next(c for c in calls if isinstance(c, SendInvoice))
    assert invoice.currency == "RUB" and invoice.prices[0].amount > 0

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
    """«Уронили» базу посреди записи: клиент видит извинение, владелец — алерт, бот живёт дальше."""
    import sqlite3

    h = Harness(make_settings(), db)
    confirm_cb = await walk_to_confirm(h, CLIENT)
    await db.set_phone(CLIENT.id, "")

    async def broken(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "create_booking", broken)
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    to_owner = [c for c in calls if isinstance(c, SendMessage) and c.chat_id == OWNER]
    assert to_owner and "database is locked" in to_owner[0].text
    assert any(isinstance(c, AnswerCallbackQuery) and "пошло не так" in (c.text or "") for c in calls)

    monkeypatch.undo()  # база «ожила» — следующая попытка проходит
    calls = await h.feed(**h.callback(CLIENT, confirm_cb))
    assert "Вы записаны" in texts_of(calls)
