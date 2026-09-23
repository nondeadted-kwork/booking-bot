"""Заявки: вопрос или пожелание без записи на время. Текст → телефон → номер заявки, владельцу уведомление."""
from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from .. import keyboards as kb
from .. import texts
from ..config import Settings
from ..db import Database
from ..notify import notify_new_lead

router = Router(name="leads")

LEADS_PER_HOUR = 3
TEXT_MIN, TEXT_MAX = 3, 500
LIMIT_REACHED = "Вы уже оставили 3 заявки за час. Мы ответим на них, подождите немного 🙂"


class LeadForm(StatesGroup):
    text = State()
    phone = State()


async def over_limit(db: Database, user_id: int) -> bool:
    return await db.count_leads_since(user_id, int(time.time()) - 3600) >= LEADS_PER_HOUR


async def ask_text(message: Message, state: FSMContext, db: Database) -> None:
    """Начало заявки. Вызывается из меню (client.py), поэтому сбрасывает любую начатую форму."""
    await state.clear()
    if await over_limit(db, message.from_user.id):
        await message.answer(LIMIT_REACHED)
        return
    await state.set_state(LeadForm.text)
    await message.answer(
        "📝 Напишите, что нужно: вопрос, пожелание или удобное время.\n\n"
        "Например: <i>«Стрижка и борода в субботу после 18:00, можно?»</i>"
    )


@router.message(LeadForm.text, F.text)
async def got_text(message: Message, state: FSMContext, db: Database, settings: Settings, bot: Bot,
                   can_admin: bool) -> None:
    text = message.text.strip()
    if len(text) < TEXT_MIN:
        await message.answer("Напишите чуть подробнее: что нужно и когда удобно.")
        return
    if len(text) > TEXT_MAX:
        await message.answer(f"Получилось {len(text)} символов, а можно до {TEXT_MAX}. Сократите, пожалуйста.")
        return
    user = await db.get_user(message.from_user.id)
    if user is not None and user.phone is None:
        # Телефон спрашиваем один раз, как при записи; «Пропустить» тоже запоминается.
        await state.update_data(text=text)
        await state.set_state(LeadForm.phone)
        await message.answer("Оставьте номер, чтобы мы могли перезвонить. Нажмите кнопку ниже 👇",
                             reply_markup=kb.ask_phone())
        return
    await state.clear()
    await submit(message, text, db, settings, bot, can_admin)


@router.message(LeadForm.text)
async def text_expected(message: Message) -> None:
    await message.answer("Пришлите, пожалуйста, текстом: что нужно и когда удобно.")


@router.message(LeadForm.phone, F.contact)
async def got_phone(message: Message, state: FSMContext, db: Database, settings: Settings, bot: Bot,
                    can_admin: bool) -> None:
    await db.set_phone(message.from_user.id, message.contact.phone_number)
    await after_phone(message, state, db, settings, bot, can_admin)


@router.message(LeadForm.phone, F.text == kb.BTN_SKIP_PHONE)
async def skip_phone(message: Message, state: FSMContext, db: Database, settings: Settings, bot: Bot,
                     can_admin: bool) -> None:
    await db.set_phone(message.from_user.id, "")
    await after_phone(message, state, db, settings, bot, can_admin)


@router.message(LeadForm.phone)
async def phone_expected(message: Message) -> None:
    await message.answer("Нажмите «📱 Отправить номер» или «Пропустить» 👇", reply_markup=kb.ask_phone())


async def after_phone(message: Message, state: FSMContext, db: Database, settings: Settings, bot: Bot,
                      can_admin: bool) -> None:
    text = (await state.get_data()).get("text", "")
    await state.clear()
    await submit(message, text, db, settings, bot, can_admin)


async def submit(message: Message, text: str, db: Database, settings: Settings, bot: Bot, can_admin: bool) -> None:
    if not text:
        await message.answer("Текст заявки потерялся: бот перезапускался. Нажмите «📝 Оставить заявку» ещё раз.",
                             reply_markup=kb.main_menu(can_admin))
        return
    if await over_limit(db, message.from_user.id):  # форму могли открыть в двух окнах сразу
        await message.answer(LIMIT_REACHED, reply_markup=kb.main_menu(can_admin))
        return
    lead = await db.create_lead(message.from_user.id, text, int(time.time()))
    await message.answer(
        f"✅ <b>Заявка №{lead.id} принята!</b>\n\n"
        f"Ответим здесь или перезвоним в рабочее время: {texts.schedule_text(settings.schedule)}.",
        reply_markup=kb.main_menu(can_admin),
    )
    await notify_new_lead(bot, settings, lead)
