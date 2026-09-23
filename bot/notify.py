"""Уведомления владельцу и общие хелперы отправки."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import texts
from .config import Settings
from .db import Booking, Lead
from .keyboards import AdmCb

log = logging.getLogger(__name__)

# В демо посетитель видит, что в этот момент пришло владельцу: обе стороны помещаются в одно видео.
MIRROR_HEADER = "👀 <i>В этот момент владелец получил:</i>\n\n"


async def notify_owners(bot: Bot, settings: Settings, text: str, markup: InlineKeyboardMarkup | None = None,
                        mirror_to: int | None = None) -> None:
    """Шлёт владельцам. В демо копию получает и посетитель, чьё действие вызвало уведомление."""
    for owner_id in settings.owner_ids:
        try:
            await bot.send_message(owner_id, text, reply_markup=markup)
        except TelegramAPIError as e:
            # Частая причина: владелец не нажал /start у своего бота.
            log.warning("Не удалось уведомить владельца %s: %s", owner_id, e)
    if settings.demo_mode and mirror_to is not None and mirror_to > 0 and mirror_to not in settings.owner_ids:
        try:
            await bot.send_message(mirror_to, MIRROR_HEADER + text, reply_markup=markup)
        except TelegramAPIError as e:
            log.warning("Не удалось отправить копию уведомления %s: %s", mirror_to, e)


async def notify_new_booking(bot: Bot, settings: Settings, b: Booking) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="Открыть карточку", callback_data=AdmCb(action="card", arg=str(b.id)))
    barber = f"✂️ {escape(b.barber_name)}\n" if b.barber_name else ""
    text = (
        f"🆕 <b>Новая запись #{b.id}</b>\n"
        f"💈 {escape(texts.service_title(b.service_code))}\n"
        f"{barber}"
        f"🗓 {texts.when(b, settings.schedule.tz)}\n"
        f"👤 {texts.client_line(b, hide=False)}\n"
        f"{texts.payment_line(b)}"
    )
    await notify_owners(bot, settings, text, kb.as_markup(), mirror_to=b.user_id)


async def notify_new_lead(bot: Bot, settings: Settings, lead: Lead) -> None:
    markup = None
    if lead.username:
        kb = InlineKeyboardBuilder()
        kb.button(text="💬 Написать", url=f"https://t.me/{lead.username}")
        markup = kb.as_markup()
    text = (
        f"📩 <b>Новая заявка #{lead.id}</b>\n"
        f"👤 {texts.lead_client_line(lead, hide=False)}\n"
        f"💬 {escape(lead.text)}"
    )
    await notify_owners(bot, settings, text, markup, mirror_to=lead.user_id)


async def edit_or_send(
    bot: Bot, chat_id: int, message: Message | None, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Редактирует сообщение с кнопками, а если нельзя (старое/удалено) — шлёт новое."""
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return
    await bot.send_message(chat_id, text, reply_markup=markup)


async def edit_cb(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    assert cb.bot is not None
    await edit_or_send(cb.bot, cb.from_user.id, cb.message if isinstance(cb.message, Message) else None, text, markup)
