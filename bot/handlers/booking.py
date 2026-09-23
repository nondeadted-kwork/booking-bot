"""Путь записи: услуга → барбер → день → время → подтверждение. Телефон и онлайн-оплата."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from aiogram.types import User as TgUser

from .. import availability
from .. import keyboards as kb
from .. import texts
from ..config import SERVICES_BY_CODE, Service, Settings
from ..db import Booking, Database, SlotTaken
from ..notify import edit_cb, edit_or_send, notify_new_booking, notify_owners
from ..slots import ANY

log = logging.getLogger(__name__)
router = Router(name="booking")


class PhoneForm(StatesGroup):
    waiting = State()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def booked_text(b: Booking, settings: Settings) -> str:
    return (f"✅ <b>Вы записаны!</b>\n\n{texts.booking_card(b, settings.schedule.tz, settings.business_address)}\n\n"
            f"Напомню за час до визита. Если планы изменятся, отмените запись кнопкой ниже.")


def service_header(service: Service) -> str:
    return f"💈 <b>{escape(service.title)}</b> · {service.minutes} мин · {texts.price(service.price)}"


async def is_bookable(db: Database, m: int) -> bool:
    """Барбер из кнопки ещё принимает записи? «Любой свободный» подходит всегда."""
    return m == ANY or any(b.id == m for b in await db.barbers())


async def barber_note(db: Database, m: int) -> str:
    """Строка с барбером под заголовком. Если мастер один, её нет."""
    barbers = await db.barbers()
    if len(barbers) <= 1:
        return ""
    if m == ANY:
        return "\n✂️ Любой свободный барбер"
    name = next((b.name for b in barbers if b.id == m), None)
    return f"\n✂️ {escape(name)}" if name else ""


# --- экраны -------------------------------------------------------------------

async def show_barbers(cb: CallbackQuery, service: Service, db: Database, settings: Settings) -> None:
    now = utcnow()
    barbers, nearest = await availability.nearest(db, settings, service, now)
    if not barbers:
        await edit_cb(cb, "Сейчас запись недоступна. Оставьте заявку, и мы перезвоним.")
        return
    if len(barbers) == 1:  # мастер один: шаг выбора не нужен
        await show_days(cb, service, barbers[0].id, db, settings)
        return
    tz = settings.schedule.tz
    today = now.astimezone(tz).date()

    def label(name: str, t: datetime | None) -> str:
        return f"{name} · {texts.nearest(t.astimezone(tz), today)}" if t else f"{name} · нет окон на неделе"

    rows = [(b.id, label(b.name, nearest[b.id]), nearest[b.id] is not None) for b in barbers]
    rows.append((ANY, label("🎲 Любой свободный", nearest[ANY]), nearest[ANY] is not None))
    about = "\n".join(f"• <b>{escape(b.name)}</b>" + (f": {escape(b.about)}" if b.about else "") for b in barbers)
    await edit_cb(cb, f"{service_header(service)}\n\nВыберите барбера:\n{about}", kb.barber_choice(service, rows))


async def show_days(cb: CallbackQuery, service: Service, m: int, db: Database, settings: Settings) -> None:
    options = await availability.day_options(db, settings, service, m, utcnow())
    many = len(await db.barbers()) > 1
    await edit_cb(cb, f"{service_header(service)}{await barber_note(db, m)}\n\nВыберите день:",
                  kb.days(service, m, options, to_barbers=many))


async def show_slots(cb: CallbackQuery, service: Service, m: int, day: date, db: Database,
                     settings: Settings) -> bool:
    free = await availability.free_times(db, settings, service, m, day, utcnow())
    if not free:
        return False
    await edit_cb(cb, f"{service_header(service)}{await barber_note(db, m)}\n🗓 <b>{texts.day_long(day)}</b>\n\n"
                      f"Выберите время:", kb.slots(service, m, list(free)))
    return True


# --- путь записи --------------------------------------------------------------

@router.callback_query(kb.ServiceCb.filter())
async def pick_service(cb: CallbackQuery, callback_data: kb.ServiceCb, db: Database, settings: Settings) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    if service is None:
        await cb.answer("Эта услуга больше недоступна", show_alert=True)
        await edit_cb(cb, "Выберите услугу:", kb.services())
        return
    await show_barbers(cb, service, db, settings)
    await cb.answer()


@router.callback_query(kb.BarberCb.filter())
async def pick_barber(cb: CallbackQuery, callback_data: kb.BarberCb, db: Database, settings: Settings) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    if service is None:
        await cb.answer("Кнопка устарела, начните заново", show_alert=True)
        return
    if not await is_bookable(db, callback_data.m):
        await cb.answer("Этот барбер больше не принимает записи, выберите другого", show_alert=True)
        await show_barbers(cb, service, db, settings)
        return
    await show_days(cb, service, callback_data.m, db, settings)
    await cb.answer()


@router.callback_query(kb.DayCb.filter())
async def pick_day(cb: CallbackQuery, callback_data: kb.DayCb, db: Database, settings: Settings) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    try:
        day = datetime.strptime(callback_data.day, "%Y%m%d").date()
    except ValueError:
        day = None
    if service is None or day is None:
        await cb.answer("Кнопка устарела, начните заново", show_alert=True)
        return
    if not await is_bookable(db, callback_data.m):
        await cb.answer("Этот барбер больше не принимает записи, выберите другого", show_alert=True)
        await show_barbers(cb, service, db, settings)
        return
    if not await show_slots(cb, service, callback_data.m, day, db, settings):
        await cb.answer("На этот день свободного времени уже нет", show_alert=True)
        await show_days(cb, service, callback_data.m, db, settings)
        return
    await cb.answer()


@router.callback_query(kb.SlotCb.filter())
async def pick_slot(cb: CallbackQuery, callback_data: kb.SlotCb, db: Database, settings: Settings) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    if service is None or not await is_bookable(db, callback_data.m):
        await cb.answer("Кнопка устарела, начните заново", show_alert=True)
        return
    m = callback_data.m
    tz = settings.schedule.tz
    start = datetime.fromtimestamp(callback_data.ts, tz)
    if start not in await availability.free_times(db, settings, service, m, start.date(), utcnow()):
        await cb.answer("Это время только что заняли, выберите другое", show_alert=True)
        if not await show_slots(cb, service, m, start.date(), db, settings):
            await show_days(cb, service, m, db, settings)
        return
    end = datetime.fromtimestamp(callback_data.ts + service.minutes * 60, tz)
    note = await barber_note(db, m)
    if m == ANY and note:
        note = "\n✂️ Барбер: назначим свободного"
    await edit_cb(
        cb,
        f"<b>Проверьте запись:</b>\n\n"
        f"💈 {escape(service.title)} ({service.minutes} мин){note}\n"
        f"🗓 {texts.day_long(start.date())}, {start:%H:%M}-{end:%H:%M}\n"
        f"💰 {texts.price(service.price)}\n"
        f"📍 {escape(settings.business_address)}",
        kb.confirm(service, m, callback_data.ts, settings.payments_enabled),
    )
    await cb.answer()


@router.callback_query(kb.BackCb.filter())
async def back(cb: CallbackQuery, callback_data: kb.BackCb, db: Database, settings: Settings) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    if callback_data.to == "days" and service is not None and await is_bookable(db, callback_data.m):
        await show_days(cb, service, callback_data.m, db, settings)
    else:
        await edit_cb(cb, "Выберите услугу:", kb.services())
    await cb.answer()


@router.callback_query(kb.NoopCb.filter())
async def noop(cb: CallbackQuery, callback_data: kb.NoopCb) -> None:
    reasons = {
        "closed": "В этот день салон не работает",
        "full": "На этот день всё занято, выберите другой",
        "off": "У барбера в этот день отпуск",
        "nofree": "У этого барбера нет окон на неделе, выберите другого",
    }
    await cb.answer(reasons.get(callback_data.reason, "Недоступно"), show_alert=True)


@router.callback_query(kb.ConfirmCb.filter())
async def confirm(cb: CallbackQuery, callback_data: kb.ConfirmCb, db: Database, settings: Settings,
                  state: FSMContext, bot: Bot) -> None:
    service = SERVICES_BY_CODE.get(callback_data.code)
    if service is None:
        await cb.answer("Кнопка устарела, начните заново", show_alert=True)
        return
    user = await db.get_user(cb.from_user.id)
    if user is not None and user.phone is None:
        # Спросим телефон один раз; «Пропустить» сохраняет пустую строку, чтобы больше не спрашивать.
        await state.set_state(PhoneForm.waiting)
        await state.update_data(code=service.code, m=callback_data.m, ts=callback_data.ts, pay=callback_data.pay)
        await cb.answer()
        await bot.send_message(cb.from_user.id,
                               "Оставьте номер телефона, мы позвоним, если что-то изменится. Нажмите кнопку ниже 👇",
                               reply_markup=kb.ask_phone())
        return
    await cb.answer()
    await finalize(bot, cb.from_user, service, callback_data.m, callback_data.ts, callback_data.pay, db, settings,
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


@router.message(PhoneForm.waiting)
async def phone_other(message: Message) -> None:
    await message.answer("Нажмите «📱 Отправить номер» или «Пропустить» 👇", reply_markup=kb.ask_phone())


async def continue_after_phone(message: Message, state: FSMContext, db: Database, settings: Settings, bot: Bot,
                               can_admin: bool) -> None:
    data = await state.get_data()
    await state.clear()
    await message.answer("Спасибо! 👍", reply_markup=kb.main_menu(can_admin))
    service = SERVICES_BY_CODE.get(data.get("code", ""))
    if service is None or "ts" not in data:
        await message.answer("Выберите услугу:", reply_markup=kb.services())
        return
    await finalize(bot, message.from_user, service, data.get("m", ANY), data["ts"], data.get("pay", False), db,
                   settings)


async def finalize(bot: Bot, user: TgUser, service: Service, m: int, ts: int, pay: bool, db: Database,
                   settings: Settings, edit: Message | None = None) -> None:
    """Создаёт запись. Для «любого» пробует свободных барберов по очереди, менее загруженных первыми.

    Если выбрана онлайн-оплата, держит слот и выставляет счёт.
    """
    now = utcnow()
    now_ts = int(now.timestamp())
    tz = settings.schedule.tz
    start = datetime.fromtimestamp(ts, tz)

    async def slot_gone() -> None:
        free = await availability.free_times(db, settings, service, m, start.date(), utcnow())
        text = "😔 Это время только что заняли."
        if free:
            await edit_or_send(bot, user.id, edit, f"{text} Выберите другое на {texts.day_long(start.date())}:",
                               kb.slots(service, m, list(free)))
        else:
            await edit_or_send(bot, user.id, edit, f"{text} Выберите другой день:", kb.services())

    own = [b for b in await db.bookings_between(ts, ts + 1, now_ts) if b.user_id == user.id]
    if own:  # повторное нажатие «Подтвердить»: запись уже есть, вторую не создаём
        if own[0].status == "pending_payment":
            await edit_or_send(bot, user.id, edit, "⏳ Это время уже держится за вами. Оплатите счёт выше 👆")
        else:
            await edit_or_send(bot, user.id, edit, booked_text(own[0], settings),
                               kb.booking_actions(own[0].id, demo=settings.demo_mode))
        return

    candidates = (await availability.free_times(db, settings, service, m, start.date(), now)).get(start, [])
    online = pay and settings.payments_enabled
    # Если до визита меньше часа, отдельное напоминание не нужно: человек только что записался.
    already_reminded = now_ts if ts - now_ts <= settings.remind_before_min * 60 else None
    booking = None
    for barber_id in candidates:
        try:
            booking = await db.create_booking(
                user_id=user.id,
                barber_id=barber_id,
                service_code=service.code,
                start_at=ts,
                end_at=ts + service.minutes * 60,
                price=service.price,
                now=now_ts,
                hold_until=now_ts + settings.payment_hold_min * 60 if online else None,
                reminded_at=already_reminded,
            )
            break
        except SlotTaken:
            continue  # этого барбера заняли секунду назад, пробуем следующего
    if booking is None:
        await slot_gone()
        return

    if online:
        await edit_or_send(
            bot, user.id, edit,
            f"⏳ Держу для вас <b>{texts.day_long(start.date())}, {start:%H:%M}</b> "
            f"{settings.payment_hold_min} минут. Оплатите счёт ниже 👇",
        )
        parts = [f"{texts.day_long(start.date())}, {start:%H:%M}"]
        if booking.barber_name and len(await db.barbers()) > 1:
            parts.append(f"барбер {booking.barber_name}")
        parts.append(settings.business_name)
        try:
            await bot.send_invoice(
                chat_id=user.id,
                title=service.title[:32],
                description=" · ".join(parts)[:255],
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
                reply_markup=kb.confirm(service, m, ts, payments=False),
            )
            await notify_owners(bot, settings, f"🔥 Не удалось выставить счёт: <code>{escape(str(e))}</code>\n"
                                               f"Проверьте PAYMENT_TOKEN.")
        return

    await edit_or_send(bot, user.id, edit, booked_text(booking, settings),
                       kb.booking_actions(booking.id, demo=settings.demo_mode))
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
        await query.answer(ok=False, error_message="Бронь истекла. Выберите время заново, это займёт 10 секунд.")
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
            reply_markup=kb.booking_actions(b.id, demo=settings.demo_mode),
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
