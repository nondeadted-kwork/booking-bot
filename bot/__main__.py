"""Точка входа: python -m bot"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand
from aiogram.utils.token import TokenValidationError

from . import reminders
from .app import build_dispatcher
from .config import Settings
from .db import Database

log = logging.getLogger("bot")


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    db = Database(settings.db_path)
    try:
        await db.connect()
    except sqlite3.DatabaseError as e:
        await db.close()
        sys.exit(f"База {settings.db_path} не открывается ({e}). "
                 f"Восстановите из бэкапа: cp backups/bot-ГГГГ-ММ-ДД.db {settings.db_path}")

    bot: Bot | None = None
    try:
        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        me = await bot.get_me()
    except (TokenValidationError, TelegramUnauthorizedError):
        await db.close()
        if bot is not None:
            await bot.session.close()
        sys.exit("BOT_TOKEN неверный. Скопируйте токен заново из @BotFather → /mybots → API Token.")

    dp = build_dispatcher(db, settings)
    await bot.set_my_commands([
        BotCommand(command="book", description="Записаться"),
        BotCommand(command="my", description="Мои записи"),
        BotCommand(command="help", description="Контакты и вопросы"),
        BotCommand(command="start", description="Перезапустить бота"),
    ])
    log.info("Started @%s | demo=%s | payments=%s | db=%s", me.username, settings.demo_mode,
             settings.payments_enabled, settings.db_path)

    background = asyncio.create_task(reminders.run(bot, db, settings))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        background.cancel()
        await db.close()
        await bot.session.close()
        log.info("Stopped")


if __name__ == "__main__":
    asyncio.run(main())
