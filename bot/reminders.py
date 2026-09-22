"""Фоновый цикл: напоминания за час и снятие неоплаченных броней.

Состояние хранится в БД (reminded_at, hold_until), а не в памяти —
после перезапуска бота ни одно напоминание не теряется и не дублируется.
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from . import keyboards as kb
from . import texts
from .config import Settings
from .db import Booking, Database

log = logging.getLogger(__name__)


def reminder_text(b: Booking, settings: Settings) -> str:
    start = texts.local(b.start_at, settings.schedule.tz)
    return (
        f"⏰ <b>Напоминание: сегодня в {start:%H:%M}</b>\n\n"
        f"{texts.booking_card(b, settings.schedule.tz, settings.business_address)}\n\n"
        f"Всё в силе?"
    )


async def tick(bot: Bot, db: Database, settings: Settings, now: int) -> None:
    for b in await db.expire_holds(now):
        log.info("Hold expired for booking #%s", b.id)
        try:
            await bot.send_message(
                b.user_id,
                f"⌛ Бронь на {texts.when(b, settings.schedule.tz)} снята: оплата не поступила "
                f"за {settings.payment_hold_min} минут. Время снова свободно — записаться: /book",
            )
        except TelegramAPIError as e:
            log.warning("Can't notify user %s about expired hold: %s", b.user_id, e)

    for b in await db.due_reminders(now, settings.remind_before_min * 60):
        try:
            await bot.send_message(b.user_id, reminder_text(b, settings), reply_markup=kb.reminder(b.id))
            log.info("Reminder sent for booking #%s", b.id)
        except (TelegramNetworkError, TelegramRetryAfter, TelegramServerError) as e:
            log.warning("Reminder #%s postponed, Telegram unavailable: %s", b.id, e)
            continue  # не помечаем — попробуем на следующем тике
        except TelegramForbiddenError:
            log.info("User %s blocked the bot, reminder #%s skipped", b.user_id, b.id)
            await db.mark_blocked(b.user_id)
        except TelegramAPIError as e:
            log.warning("Reminder #%s failed: %s", b.id, e)
        await db.mark_reminded(b.id, now)


async def run(bot: Bot, db: Database, settings: Settings) -> None:
    log.info("Background loop started: tick=%ss, remind %s min before",
             settings.tick_seconds, settings.remind_before_min)
    while True:
        try:
            await tick(bot, db, settings, int(time.time()))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Упал тик (например, база заблокирована) — логируем и пробуем снова, цикл не умирает.
            log.exception("Background tick failed, retry in %ss", settings.tick_seconds)
        await asyncio.sleep(settings.tick_seconds)
