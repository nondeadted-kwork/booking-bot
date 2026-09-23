"""Ловушки для всего, что не поймали остальные роутеры. Подключается последним."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..db import Database

router = Router(name="fallback")


@router.message(F.contact)
async def phone_without_state(message: Message, db: Database, can_admin: bool) -> None:
    # Контакт пришёл, но бот успел перезапуститься и забыл, на чём остановились.
    await db.set_phone(message.from_user.id, message.contact.phone_number)
    await message.answer("Номер сохранён 👍", reply_markup=kb.main_menu(can_admin))
    await message.answer("Продолжим, выберите услугу:", reply_markup=kb.services())


@router.callback_query()
async def stale_button(cb: CallbackQuery) -> None:
    await cb.answer("Кнопка устарела, откройте меню заново", show_alert=True)


@router.message()
async def fallback(message: Message, can_admin: bool) -> None:
    await message.answer("Я понимаю кнопки меню 🙂 Выберите действие ниже.", reply_markup=kb.main_menu(can_admin))
