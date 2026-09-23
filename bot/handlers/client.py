"""Клиентская часть: старт, кнопки меню, услуги, контакты и «Мои записи».

Кнопки меню собраны в этом роутере, он подключается первым. Поэтому они срабатывают из любого места,
даже посреди ввода телефона или заявки, и сбрасывают начатую форму.
"""
from __future__ import annotations

import time
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import texts
from ..config import SERVICES, Settings
from ..db import Database
from ..notify import edit_cb, notify_owners
from ..reminders import reminder_text
from . import admin

router = Router(name="client")


def start_text(first_name: str, settings: Settings) -> str:
    text = (
        f"Здравствуйте, {escape(first_name)}! 👋\n\n"
        f"Я бот <b>{escape(settings.business_name)}</b>. Запишу вас за полминуты и напомню о визите за час."
    )
    if settings.demo_mode:
        text += (
            "\n\n🧪 <b>Это демо.</b> Записывайтесь смело, салон ненастоящий. "
            "Панель владельца открыта всем: кнопка «👀 Панель владельца»."
        )
        if settings.payments_enabled:
            text += f"\nОплата тестовая, деньги не списываются. Карта: <code>{escape(settings.test_card_hint)}</code>"
    return text


@router.message(CommandStart())
async def start(message: Message, db: Database, settings: Settings, state: FSMContext, can_admin: bool) -> None:
    await state.clear()
    user = message.from_user
    await db.upsert_user(user.id, user.first_name, user.username)  # сбрасывает флаг «заблокировал бота»
    await message.answer(start_text(user.first_name, settings), reply_markup=kb.main_menu(can_admin))


@router.message(Command("book"))
@router.message(F.text == kb.BTN_BOOK)
async def book(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Выберите услугу:", reply_markup=kb.services())


@router.message(F.text == kb.BTN_PRICES)
async def prices(message: Message, state: FSMContext) -> None:
    await state.clear()
    lines = [f"• {escape(s.title)}: <b>{texts.price(s.price)}</b>, {s.minutes} мин" for s in SERVICES]
    await message.answer("💈 <b>Услуги и цены</b>\n\n" + "\n".join(lines) + "\n\nНажмите на услугу, чтобы записаться:",
                         reply_markup=kb.services())


@router.message(Command("help"))
@router.message(F.text == kb.BTN_CONTACTS)
async def contacts(message: Message, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    barbers = await db.barbers()
    team = ""
    if len(barbers) > 1:
        team = "✂️ Барберы: " + ", ".join(f"{escape(b.name)} ({texts.days_span(b.workdays)})" for b in barbers) + "\n"
    await message.answer(
        f"📍 <b>{escape(settings.business_name)}</b>\n"
        f"Адрес: {escape(settings.business_address)}\n"
        f"Телефон: {escape(settings.business_phone)}\n"
        f"Режим работы: {texts.schedule_text(settings.schedule)}\n"
        f"{team}\n"
        f"❓ <b>Частые вопросы</b>\n\n"
        f"<b>Как отменить или перенести?</b>\n«🗂 Мои записи» → «Отменить», затем запишитесь на новое время.\n\n"
        f"<b>Опаздываю, что делать?</b>\nПозвоните по номеру выше, мастер подождёт до 10 минут.\n\n"
        f"<b>Как оплатить?</b>\nКартой онлайн при записи или на месте, как удобнее."
    )


@router.message(Command("admin"))
@router.message(F.text == kb.BTN_ADMIN)
async def admin_entry(message: Message, state: FSMContext, can_admin: bool, is_owner: bool) -> None:
    if not can_admin:
        await message.answer("Эта команда доступна только владельцу.")
        return
    await state.clear()
    await message.answer(admin.header(is_owner), reply_markup=kb.admin_menu())


# --- «Мои записи» ------------------------------------------------------------

async def my_view(user_id: int, db: Database, settings: Settings) -> tuple[str, kb.InlineKeyboardMarkup]:
    items = await db.user_upcoming(user_id, int(time.time()))
    if not items:
        return "У вас нет предстоящих записей.", kb.my_list([])
    tz = settings.schedule.tz
    blocks = [texts.booking_card(b, tz, settings.business_address) for b in items]
    labels = [(b.id, f"{texts.day_short(texts.local(b.start_at, tz).date())} "
                     f"{texts.local(b.start_at, tz):%H:%M}") for b in items]
    return "🗂 <b>Ваши записи</b>\n\n" + "\n\n".join(blocks), kb.my_list(labels)


@router.message(Command("my"))
@router.message(F.text == kb.BTN_MY)
async def my(message: Message, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    text, markup = await my_view(message.from_user.id, db, settings)
    await message.answer(text, reply_markup=markup)


@router.callback_query(kb.MyCb.filter())
async def my_actions(cb: CallbackQuery, callback_data: kb.MyCb, db: Database, settings: Settings, bot: Bot) -> None:
    if callback_data.action == "list":
        text, markup = await my_view(cb.from_user.id, db, settings)
        await edit_cb(cb, text, markup)
        await cb.answer()
        return

    b = await db.get_booking(callback_data.id)
    if b is None or b.user_id != cb.from_user.id:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    if b.status != "confirmed" or b.start_at <= time.time():
        await cb.answer("Эта запись уже неактуальна", show_alert=True)
        text, markup = await my_view(cb.from_user.id, db, settings)
        await edit_cb(cb, text, markup)
        return

    tz = settings.schedule.tz
    if callback_data.action == "come":
        await db.set_client_confirmed(b.id)
        await edit_cb(cb, f"👍 Отлично, ждём вас!\n\n{texts.booking_card(b, tz, settings.business_address)}")
        await notify_owners(bot, settings, f"✅ {escape(b.first_name)} подтвердил(а) визит: "
                                           f"{texts.when(b, tz)} (#{b.id})")
    elif callback_data.action == "cancel":
        await edit_cb(cb, f"Отменить запись?\n\n{texts.booking_card(b, tz, settings.business_address)}",
                      kb.cancel_confirm(b.id))
    elif callback_data.action == "cancel_yes":
        await db.cancel_booking(b.id, by="client")
        refund = "\nДеньги вернём на карту в течение 1-3 дней." if b.paid else ""
        await edit_cb(cb, f"Запись на {texts.when(b, tz)} отменена.{refund}\n\nЗаписаться снова: /book")
        await notify_owners(
            bot, settings,
            f"❌ Клиент отменил запись #{b.id}\n{texts.service_title(b.service_code)}, {texts.when(b, tz)}\n"
            f"👤 {texts.client_line(b, hide=False)}" + ("\n⚠️ Была онлайн-оплата, оформите возврат." if b.paid else ""),
        )
    await cb.answer()


@router.message(Command("remind_test"))
async def remind_test(message: Message, db: Database, settings: Settings, can_admin: bool) -> None:
    """Для демо и видео: показать напоминание сразу, не дожидаясь часа до визита."""
    if not can_admin:
        return
    items = await db.user_upcoming(message.from_user.id, int(time.time()))
    if not items:
        await message.answer("Сначала запишитесь: напоминание придёт по ближайшей записи.")
        return
    await message.answer(reminder_text(items[0], settings), reply_markup=kb.reminder(items[0].id))
