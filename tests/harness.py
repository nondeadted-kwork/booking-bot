"""Обвязка сквозных тестов: апдейты подаются в диспетчер, запросы к Bot API перехватывает FakeSession."""
from __future__ import annotations

import itertools
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from datetime import time as dtime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendDocument, SendInvoice, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import app
from bot.config import BarberSeed, Settings
from bot.db import Booking, Database

OWNER = 1000
OWNER_USER = User(id=OWNER, is_bot=False, first_name="Фёдор")
CLIENT = User(id=1, is_bot=False, first_name="Иван", username="ivan")
OTHER = User(id=2, is_bot=False, first_name="Пётр")
EVERY_DAY = frozenset(range(7))
ONE_BARBER = (BarberSeed("Мастер", "", EVERY_DAY),)
TWO_BARBERS = (BarberSeed("Артём", "Фейды", EVERY_DAY), BarberSeed("Максим", "Борода", EVERY_DAY))


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
        for router in app.ROUTERS:  # роутеры модульные синглтоны: отвязываем от прошлого теста
            router._parent_router = None
        self.dp = app.build_dispatcher(db, settings)
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


def sent_to(calls, chat_id: int) -> list[SendMessage]:
    return [c for c in calls if isinstance(c, SendMessage) and c.chat_id == chat_id]


def make_settings(**kw) -> Settings:
    kw.setdefault("demo_mode", True)
    return Settings(bot_token="123456:TEST", owner_ids=frozenset({OWNER}), **kw)


async def open_db(path: str, barbers=TWO_BARBERS) -> Database:
    database = Database(path)
    await database.connect()
    await database.ensure_barbers(barbers)
    return database


def at_day(settings: Settings, days: int, hour: int, minute: int = 0) -> int:
    """Unix-время через `days` дней от сегодня, в hour:minute по часам салона."""
    tz = settings.schedule.tz
    day = datetime.now(tz).date() + timedelta(days=days)
    return int(datetime.combine(day, dtime(hour, minute), tzinfo=tz).timestamp())


async def book(db: Database, user_id: int, barber_id: int, start: int, minutes: int = 60,
               price: int = 1500) -> Booking:
    return await db.create_booking(user_id=user_id, barber_id=barber_id, service_code="cut", start_at=start,
                                   end_at=start + minutes * 60, price=price, now=int(time.time()))
