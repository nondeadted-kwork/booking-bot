"""Клиентская часть: запись, оплата, «Мои записи», напоминания."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from aiogram.types import User as TgUser

from .. import keyboards as kb
from .. import texts
from ..config import SERVICES, SERVICES_BY_CODE, Service, Settings
from ..db import Database, SlotTaken
from ..notify import edit_cb, edit_or_send, notify_new_booking, notify_owners
from ..reminders import reminder_text
from ..slots import calendar_days, day_range, free_slots

log = logging.getLogger(__name__)
router = Router(name="client")


class PhoneForm(StatesGroup):
    waiting = State()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def schedule_text(settings: Settings) -> str:
    sch = settings.schedule
    days = sorted(sch.workdays)
    if days == list(range(days[0], days[-1] + 1)):
        span = f"{texts.WEEKDAYS[days[0]]}–{texts.WEEKDAYS[days[-1]]}"
    else:
        span = ", ".join(texts.WEEKDAYS[d] for d in days)
    return f"{span} {sch.work_start:%H:%M}–{sch.work_end:%H:%M}"


# --- расчёт свободного времени -----------------------------------------------

async def closed_dates(db: Database) -> set[date]:
    return {date.fromisoformat(d) for d in await db.closed_days()}


async def free_for_day(service: Service, day: date, db: Database, settings: Settings, now: datetime) -> list[datetime]:
    start, end = day_range(day, settings.schedule)
    busy = await db.busy_intervals(start, end, int(now.timestamp()))
    return free_slots(day, service.minutes, busy, now, settings.schedule, await closed_dates(db))


async def day_options(
    service: Service, db: Database, settings: Settings, now: datetime
) -> list[tuple[date, int, bool]]:
    days = calendar_days(now, settings.schedule)
    if not days:
        return []
    start, _ = day_range(days[0], settings.schedule)
    _, end = day_range(days[-1], settings.schedule)
    busy = await db.busy_intervals(start, end, int(now.timestamp()))
    closed = await closed_dates(db)
    return [
        (d, len(free_slots(d, service.minutes, busy, now, settings.schedule, closed)), d in closed)
        for d in days
    ]


def service_header(service: Service) -> str:
    return f"💈 <b>{escape(service.title)}</b> · {service.minutes} мин · {texts.price(service.price)}"


async def show_days(cb: CallbackQuery, service: Service, db: Database, settings: Settings) -> None:
    options = await day_options(service, db, settings, utcnow())
    await edit_cb(cb, f"{service_header(service)}\n\nВыберите день:",
                  kb.days(service, options))


async def show_slots(cb: CallbackQuery, service: Service, day: date, db: Database, settings: Settings,
                     prefix: str = "") -> bool:
    free = await free_for_day(service, day, db, settings, utcnow())
    if not free:
        return False
    await edit_cb(cb, f"{prefix}{service_header(service)}\n🗓 <b>{texts.day_long(day)}</b>\n\nВыберите время:",
                  kb.slots(service, free))
    return True


# --- старт и меню ------------------------------------------------------------

@router.message(CommandStart())
async def start(message: Message, db: Database, settings: Settings, state: FSMContext, can_admin: bool) -> None:
    await state.clear()
    user = message.from_user
    await db.upsert_user(user.id, user.first_name, user.username)  # сбрасывает флаг «заблокировал бота»
    text = (
        f"Здравствуйте, {escape(user.first_name)}! 👋\n\n"
        f"Я бот <b>{escape(settings.business_name)}</b>. Запишу вас за полминуты: "
        f"услуга → день → время. За час до визита пришлю напоминание."
    )
    if settings.demo_mode:
        text += (
            "\n\n🧪 <b>Это демо.</b> Записывайтесь смело — это не настоящий барбершоп. "
            "Панель владельца открыта всем: кнопка «👀 Панель владельца»."
        )
        if settings.payments_enabled:
            text += f"\nОплата тестовая, деньги не списываются. Карта: <code>{escape(settings.test_card_hint)}</code>"
    await message.answer(text, reply_markup=kb.main_menu(can_admin))


@router.message(Command("book"))
@router.message(F.text == kb.BTN_BOOK)
async def book(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Выберите услугу:", reply_markup=kb.services())


@router.message(F.text == kb.BTN_PRICES)
async def prices(message: Message, state: FSMContext) -> None:
    await state.clear()
    lines = [f"• {escape(s.title)} — <b>{texts.price(s.price)}</b>, {s.minutes} мин" for s in SERVICES]
    await message.answer("💈 <b>Услуги и цены</b>\n\n" + "\n".join(lines) + "\n\nНажмите на услугу, чтобы записаться:",
                         reply_markup=kb.services())


@router.message(Command("help"))
@router.message(F.text == kb.BTN_CONTACTS)
async def contacts(message: Message, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"📍 <b>{escape(settings.business_name)}</b>\n"
        f"Адрес: {escape(settings.business_address)}\n"
        f"Телефон: {escape(settings.business_phone)}\n"
        f"Режим работы: {schedule_text(settings)}\n\n"
        f"❓ <b>Частые вопросы</b>\n\n"
        f"<b>Как отменить или перенести?</b>\n«🗂 Мои записи» → «Отменить», затем запишитесь на новое время.\n\n"
        f"<b>Опаздываю — что делать?</b>\nПозвоните по номеру выше, мастер подождёт до 10 минут.\n\n"
        f"<b>Как оплатить?</b>\nКартой онлайн при записи или на месте — как удобнее."
    )


# --- путь записи: услуга → день → время → подтверждение ----------------------

@router.callback_query(kb.ServiceCb.filter())
async def pick_service(cb: CallbackQuery, callback_data: kb.ServiceCb, db: Database, settings: Settings) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    if service is None:
        await cb.answer("Эта услуга больше недоступна", show_alert=True)
        await edit_cb(cb, "Выберите услугу:", kb.services())
        return
    await show_days(cb, service, db, settings)
    await cb.answer()


@router.callback_query(kb.DayCb.filter())
async def pick_day(cb: CallbackQuery, callback_data: kb.DayCb, db: Database, settings: Settings) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    try:
        day = datetime.strptime(callback_data.day, "%Y%m%d").date()
    except ValueError:
        day = None
    if service is None or day is None:
        await cb.answer("Кнопка устарела — начните заново", show_alert=True)
        return
    if not await show_slots(cb, service, day, db, settings):
        await cb.answer("На этот день свободного времени уже нет", show_alert=True)
        await show_days(cb, service, db, settings)
        return
    await cb.answer()


@router.callback_query(kb.SlotCb.filter())
async def pick_slot(cb: CallbackQuery, callback_data: kb.SlotCb, db: Database, settings: Settings) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    if service is None:
        await cb.answer("Кнопка устарела — начните заново", show_alert=True)
        return
    tz = settings.schedule.tz
    start = datetime.fromtimestamp(callback_data.ts, tz)
    free = await free_for_day(service, start.date(), db, settings, utcnow())
    if start not in free:
        await cb.answer("Это время только что заняли — выберите другое", show_alert=True)
        if not await show_slots(cb, service, start.date(), db, settings):
            await show_days(cb, service, db, settings)
        return
    end = datetime.fromtimestamp(callback_data.ts + service.minutes * 60, tz)
    await edit_cb(
        cb,
        f"<b>Проверьте запись:</b>\n\n"
        f"💈 {escape(service.title)} ({service.minutes} мин)\n"
        f"🗓 {texts.day_long(start.date())}, {start:%H:%M}–{end:%H:%M}\n"
        f"💰 {texts.price(service.price)}\n"
        f"📍 {escape(settings.business_address)}",
        kb.confirm(service, callback_data.ts, settings.payments_enabled),
    )
    await cb.answer()


@router.callback_query(kb.BackCb.filter())
async def back(cb: CallbackQuery, callback_data: kb.BackCb, db: Database, settings: Settings) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    if callback_data.to == "days" and service is not None:
        await show_days(cb, service, db, settings)
    else:
        await edit_cb(cb, "Выберите услугу:", kb.services())
    await cb.answer()


@router.callback_query(kb.NoopCb.filter())
async def noop(cb: CallbackQuery, callback_data: kb.NoopCb) -> None:
    reasons = {"closed": "В этот день мы не работаем", "full": "На этот день всё занято — выберите другой"}
    await cb.answer(reasons.get(callback_data.reason, "Недоступно"), show_alert=True)


@router.callback_query(kb.ConfirmCb.filter())
async def confirm(cb: CallbackQuery, callback_data: kb.ConfirmCb, db: Database, settings: Settings,
                  state: FSMContext, bot: Bot, can_admin: bool) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    if service is None:
        await cb.answer("Кнопка устарела — начните заново", show_alert=True)
        return
    user = await db.get_user(cb.from_user.id)
    if user is not None and user.phone is None:
        # Спросим телефон один раз; «Пропустить» сохраняет пустую строку, чтобы больше не спрашивать.
        await state.set_state(PhoneForm.waiting)
        await state.update_data(code=service.code, ts=callback_data.ts, pay=callback_data.pay)
        await cb.answer()
        await cb.message.answer(
            "Оставьте номер телефона — мастер позвонит, если что-то изменится. Нажмите кнопку ниже 👇",
            reply_markup=kb.ask_phone(),
        )
        return
    await cb.answer()
    await finalize(bot, cb.from_user, service, callback_data.ts, callback_data.pay, db, settings,
                   edit=cb.message if isinstance(cb.message, Message) else None)


@router.message(PhoneForm.waiting, F.contact)
async def got_phone(message: Message, state: FSMContext, db: Database, settings: Settings, bot: Bot,
                    can_admin: bool) -> None:
    await db.set_phone(message.from_user.id, message.contact.phone_number)
    await continue_after_phone(message, state, db, settings, bot, can_admin)


@router.message(PhoneForm.waiting, F.text == kb.BTN_SKIP_PHONE)
async def skip_phone(message: Message, state: FSMContext, db: Database, settings: Settings, bot: Bot,
                     can_admin: bool) -> None:
    await db.set_phone(message.from_user.id, "")
    await continue_after_phone(message, state, db, settings, bot, can_admin)


async def continue_after_phone(message: Message, state: FSMContext, db: Database, settings: Settings, bot: Bot,
                               can_admin: bool) -> None:
    data = await state.get_data()
    await state.clear()
    await message.answer("Спасибо! 👍", reply_markup=kb.main_menu(can_admin))
    service = SERVICES_BY_CODE.get(data.get("code", ""))
    if service is None:
        await message.answer("Выберите услугу:", reply_markup=kb.services())
        return
    await finalize(bot, message.from_user, service, data["ts"], data["pay"], db, settings)


@router.message(F.contact)
async def phone_without_state(message: Message, db: Database, can_admin: bool) -> None:
    # Контакт пришёл, но бот успел перезапуститься и забыл, на чём остановились.
    await db.set_phone(message.from_user.id, message.contact.phone_number)
    await message.answer("Номер сохранён 👍", reply_markup=kb.main_menu(can_admin))
    await message.answer("Продолжим — выберите услугу:", reply_markup=kb.services())


async def finalize(bot: Bot, user: TgUser, service: Service, ts: int, pay: bool, db: Database, settings: Settings,
                   edit: Message | None = None) -> None:
    """Создаёт запись. Если выбрана онлайн-оплата — держит слот и выставляет счёт."""
    now = utcnow()
    now_ts = int(now.timestamp())
    tz = settings.schedule.tz
    start = datetime.fromtimestamp(ts, tz)

    async def slot_gone() -> None:
        free = await free_for_day(service, start.date(), db, settings, now)
        text = "😔 Это время только что заняли."
        if free:
            await edit_or_send(bot, user.id, edit, f"{text} Выберите другое на {texts.day_long(start.date())}:",
                               kb.slots(service, free))
        else:
            await edit_or_send(bot, user.id, edit, f"{text} Выберите другой день:", kb.services())

    if start not in await free_for_day(service, start.date(), db, settings, now):
        await slot_gone()
        return

    online = pay and settings.payments_enabled
    # Если до визита меньше часа, отдельное напоминание не нужно — человек только что записался.
    already_reminded = now_ts if ts - now_ts <= settings.remind_before_min * 60 else None
    barber = (await db.barbers())[0]  # временно: выбор барбера появится вместе с booking.py
    try:
        booking = await db.create_booking(
            user_id=user.id,
            barber_id=barber.id,
            service_code=service.code,
            start_at=ts,
            end_at=ts + service.minutes * 60,
            price=service.price,
            now=now_ts,
            hold_until=now_ts + settings.payment_hold_min * 60 if online else None,
            reminded_at=already_reminded,
        )
    except SlotTaken:
        await slot_gone()
        return

    if online:
        await edit_or_send(
            bot, user.id, edit,
            f"⏳ Держу для вас <b>{texts.day_long(start.date())}, {start:%H:%M}</b> "
            f"{settings.payment_hold_min} минут. Оплатите счёт ниже 👇",
        )
        try:
            await bot.send_invoice(
                chat_id=user.id,
                title=service.title[:32],
                description=f"{texts.day_long(start.date())}, {start:%H:%M} · {settings.business_name}"[:255],
                payload=f"b:{booking.id}",
                currency="RUB",
                prices=[LabeledPrice(label=service.title, amount=service.price * 100)],
                provider_token=settings.payment_token,
            )
        except TelegramBadRequest as e:
            # Например, неверный PAYMENT_TOKEN. Не оставляем клиента с «висящей» бронью.
            log.error("send_invoice failed: %s", e)
            await db.cancel_booking(booking.id, by="system")
            await bot.send_message(
                user.id,
                "Онлайн-оплата сейчас недоступна 😔 Можно записаться с оплатой на месте:",
                reply_markup=kb.confirm(service, ts, payments=False),
            )
            await notify_owners(bot, settings, f"🔥 Не удалось выставить счёт: <code>{escape(str(e))}</code>\n"
                                               f"Проверьте PAYMENT_TOKEN.")
        return

    await edit_or_send(
        bot, user.id, edit,
        f"✅ <b>Вы записаны!</b>\n\n{texts.booking_card(booking, tz, settings.business_address)}\n\n"
        f"Напомню за час до визита. Если планы изменятся — отмените запись кнопкой ниже.",
        kb.booking_actions(booking.id),
    )
    await notify_new_booking(bot, settings, booking)


# --- оплата (Telegram Payments) ----------------------------------------------

def parse_payload(payload: str) -> int | None:
    prefix, _, raw_id = payload.partition(":")
    return int(raw_id) if prefix == "b" and raw_id.isdigit() else None


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, db: Database) -> None:
    """Последний шанс отказать до списания денег: бронь ещё наша и сумма совпадает?"""
    now = int(time.time())
    booking_id = parse_payload(query.invoice_payload)
    b = await db.get_booking(booking_id) if booking_id else None
    if b is None or b.user_id != query.from_user.id or b.status != "pending_payment" or (b.hold_until or 0) <= now:
        await query.answer(ok=False, error_message="Бронь истекла. Выберите время заново — это займёт 10 секунд.")
        return
    if query.total_amount != b.price * 100:
        await query.answer(ok=False, error_message="Цена изменилась. Пожалуйста, оформите запись заново.")
        return
    await db.extend_hold(b.id, now + 10 * 60)  # чтобы бронь не истекла, пока банк проводит платёж
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def paid(message: Message, db: Database, settings: Settings, bot: Bot) -> None:
    sp = message.successful_payment
    booking_id = parse_payload(sp.invoice_payload)
    charge = sp.provider_payment_charge_id or sp.telegram_payment_charge_id
    result = await db.confirm_payment(booking_id or 0, charge, sp.total_amount, int(time.time()))
    log.info("Payment for booking #%s: %s (charge %s)", booking_id, result, charge)
    b = await db.get_booking(booking_id) if booking_id else None

    if b is not None and result in ("ok", "already"):
        await message.answer(
            f"✅ <b>Оплата прошла, вы записаны!</b>\n\n"
            f"{texts.booking_card(b, settings.schedule.tz, settings.business_address)}\n\n"
            f"Напомню за час до визита.",
            reply_markup=kb.booking_actions(b.id),
        )
        if result == "ok":
            await notify_new_booking(bot, settings, b)
        return

    # Деньги списались, а слот за это время успели занять (или бронь не нашлась). Редко, но бывает.
    await message.answer(
        "Оплата прошла, но это время, к сожалению, успели занять, пока шёл платёж. "
        "Администратор свяжется с вами и вернёт деньги или предложит другое время. Извините!"
    )
    await notify_owners(
        bot, settings,
        f"⚠️ <b>Нужен возврат.</b> Оплата {texts.price(sp.total_amount // 100)} по брони #{booking_id} "
        f"прошла, но слот занят ({result}).\nКлиент: {escape(message.from_user.full_name)} "
        f"(id {message.from_user.id})\nПлатёж: <code>{escape(charge)}</code>",
    )


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
        refund = "\nДеньги вернём на карту в течение 1–3 дней." if b.paid else ""
        await edit_cb(cb, f"Запись на {texts.when(b, tz)} отменена.{refund}\n\nЗаписаться снова: /book")
        await notify_owners(
            bot, settings,
            f"❌ Клиент отменил запись #{b.id}\n{texts.service_title(b.service_code)} — {texts.when(b, tz)}\n"
            f"👤 {texts.client_line(b, hide=False)}" + ("\n⚠️ Была онлайн-оплата — оформите возврат." if b.paid else ""),
        )
    await cb.answer()


@router.message(Command("remind_test"))
async def remind_test(message: Message, db: Database, settings: Settings, can_admin: bool) -> None:
    """Для демо и видео: показать напоминание сразу, не дожидаясь часа до визита."""
    if not can_admin:
        return
    items = await db.user_upcoming(message.from_user.id, int(time.time()))
    if not items:
        await message.answer("Сначала запишитесь — напоминание придёт по ближайшей записи.")
        return
    await message.answer(reminder_text(items[0], settings), reply_markup=kb.reminder(items[0].id))


# --- всё остальное ------------------------------------------------------------

@router.message(PhoneForm.waiting)
async def phone_other(message: Message) -> None:
    await message.answer("Нажмите «📱 Отправить номер» или «Пропустить» 👇", reply_markup=kb.ask_phone())


@router.callback_query()
async def stale_button(cb: CallbackQuery) -> None:
    await cb.answer("Кнопка устарела — откройте меню заново", show_alert=True)


@router.message()
async def fallback(message: Message, can_admin: bool) -> None:
    await message.answer("Я понимаю кнопки меню 🙂 Выберите действие ниже.", reply_markup=kb.main_menu(can_admin))
