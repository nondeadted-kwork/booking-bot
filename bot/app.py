"""Сборка диспетчера: middleware, порядок роутеров, обработчик ошибок, первый запуск и профиль бота."""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent, TelegramObject
from aiogram.types import User as TgUser

from . import texts
from .config import DEFAULT_BARBER_NAME, DEMO_BARBERS, BarberSeed, Settings
from .db import Database
from .handlers import admin, barbers, booking, client, fallback, leads
from .notify import notify_owners

log = logging.getLogger(__name__)

# Порядок важен: кнопки меню (client) раньше любых форм, ловушки (fallback) последними.
ROUTERS = (client.router, admin.router, barbers.router, booking.router, leads.router, fallback.router)

COMMANDS = [
    BotCommand(command="book", description="Записаться"),
    BotCommand(command="lead", description="Оставить заявку"),
    BotCommand(command="my", description="Мои записи"),
    BotCommand(command="help", description="Контакты и вопросы"),
    BotCommand(command="start", description="Перезапустить бота"),
]


class UserMiddleware(BaseMiddleware):
    """Регистрирует пользователя в БД и прокидывает в хендлеры флаги is_owner / can_admin."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._seen: set[int] = set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: TgUser | None = data.get("event_from_user")
        is_owner = bool(user and user.id in self.settings.owner_ids)
        if user and not user.is_bot and user.id not in self._seen:
            await self.db.upsert_user(user.id, user.first_name, user.username)
            self._seen.add(user.id)
        data["is_owner"] = is_owner
        data["can_admin"] = is_owner or self.settings.demo_mode
        return await handler(event, data)


_last_alert: dict[str, float] = {}


async def on_error(event: ErrorEvent, bot: Bot, settings: Settings) -> bool:
    """Любое необработанное исключение: пишем в лог, извиняемся перед клиентом, сообщаем владельцу."""
    exc = event.exception
    update = event.update
    log.error("Update %s failed: %r", update.update_id, exc, exc_info=exc)
    try:
        if update.callback_query:
            await update.callback_query.answer("⚠️ Что-то пошло не так. Попробуйте ещё раз.", show_alert=True)
        elif update.message:
            await update.message.answer("⚠️ Что-то пошло не так. Мы уже разбираемся, попробуйте через минуту.")
    except Exception:  # noqa: BLE001 (если Telegram недоступен, просто молчим)
        pass

    # Одна и та же ошибка не чаще раза в 5 минут, чтобы не заспамить владельца.
    key = type(exc).__name__
    if time.monotonic() - _last_alert.get(key, -1e9) > 300:
        _last_alert[key] = time.monotonic()
        await notify_owners(
            bot, settings,
            f"🔥 <b>Ошибка в боте</b>\n<code>{escape(repr(exc))[:600]}</code>\n"
            f"update_id={update.update_id}. Полный traceback в логах: <code>docker compose logs bot</code>",
        )
    return True


def build_dispatcher(db: Database, settings: Settings) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["settings"] = settings
    dp.update.outer_middleware(UserMiddleware(db, settings))
    dp.errors.register(on_error)
    dp.include_routers(*ROUTERS)
    return dp


async def bootstrap(db: Database, settings: Settings) -> None:
    """Первый запуск: барберы по умолчанию. В демо три барбера, без демо один мастер на графике салона."""
    if settings.demo_mode:
        seeds = DEMO_BARBERS
    else:
        seeds = (BarberSeed(DEFAULT_BARBER_NAME, "", settings.schedule.workdays),)
    await db.ensure_barbers(seeds)


async def set_profile(bot: Bot, settings: Settings) -> None:
    """Описание в пустом чате до «Старт», короткое описание и меню команд. Сбой Telegram не мешает старту."""
    try:
        await bot.set_my_description(description=texts.bot_description(settings))
        await bot.set_my_short_description(short_description=texts.bot_short_description(settings))
        await bot.set_my_commands(COMMANDS)
    except TelegramAPIError as e:
        log.warning("Не удалось обновить описание бота: %s", e)
