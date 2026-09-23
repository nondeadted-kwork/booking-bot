"""Панель владельца: расписание по барберам, заявки, выходные, рассылка, статистика и выгрузка.

В DEMO_MODE панель видят все, но чужие имена и телефоны скрыты, а действия,
затрагивающие других людей (отмена чужих записей, выходные, рассылка всем), выключены.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import keyboards as kb
from .. import texts
from ..config import Settings
from ..db import Barber, Booking, Database, Lead
from ..notify import edit_cb

log = logging.getLogger(__name__)
router = Router(name="admin")

STATUS = {"confirmed": "подтверждена", "cancelled": "отменена", "expired": "не оплачена",
          "pending_payment": "ждёт оплату"}
DEMO_LOCKED = "🔒 В демо это выключено, у владельца работает."
LEGEND = "\n\n<i>💳 оплачено онлайн · ✅ клиент подтвердил · ⏳ ждёт оплату. Нажмите /bN, чтобы открыть запись.</i>"
MAX_TEXT = 3800  # лимит сообщения Telegram 4096, остальное под легенду


class BroadcastForm(StatesGroup):
    text = State()


def header(is_owner: bool) -> str:
    text = ("👑 <b>Панель владельца</b>\n\n"
            "Расписание, барберы, заявки, рассылка и статистика прямо в Telegram, без CRM.")
    if not is_owner:
        text += ("\n\n🧪 <i>Демо-режим: вы видите панель глазами владельца. Имена других клиентов скрыты, "
                 "действия, которые затронут других людей, выключены.</i>")
    return text


def midnight(d: date, settings: Settings) -> int:
    return int(datetime.combine(d, dtime(0), tzinfo=settings.schedule.tz).timestamp())


def hide_for(viewer_id: int, is_owner: bool, user_id: int) -> bool:
    return not is_owner and user_id != viewer_id


def fit(lines: list[str]) -> str:
    """Склеивает строки, пока влезает в одно сообщение. Режет по целым строкам, чтобы не порвать HTML."""
    text = ""
    for line in lines:
        if len(text) + len(line) + 1 > MAX_TEXT:
            return text + "\n…не влезло в сообщение, полный список в выгрузке CSV"
        text = f"{text}\n{line}" if text else line
    return text


def day_view(d: date, items: list[Booking], barbers: list[Barber], off: dict[int, frozenset[date]],
             closed: set[str], settings: Settings, viewer_id: int, is_owner: bool) -> str:
    """День по барберам: у каждого число записей, сумма и сами записи."""
    title = f"<b>{texts.day_long(d).capitalize()}</b>"
    if f"{d:%Y-%m-%d}" in closed:
        return f"{title}\n🚫 Салон не работает"
    tz = settings.schedule.tz
    lines = [f"{title} · {len(items)} зап. · {texts.price(sum(b.price for b in items))}"]
    for br in barbers:
        mine = [b for b in items if b.barber_id == br.id]
        works = br.active and d.weekday() in br.workdays and d.weekday() in settings.schedule.workdays
        if not mine and not works:
            continue
        head = f"\n✂️ <b>{escape(br.name)}</b>"
        if not mine:
            lines.append(f"{head} · {'🌴 отпуск' if d in off.get(br.id, frozenset()) else 'свободен'}")
            continue
        lines.append(f"{head} · {len(mine)} зап. · {texts.price(sum(b.price for b in mine))}")
        lines += [texts.admin_line(b, tz, hide_for(viewer_id, is_owner, b.user_id)) for b in mine]
    return fit(lines)


def week_view(days: list[date], items: list[Booking], barbers: list[Barber], closed: set[str],
              settings: Settings) -> str:
    """Неделя одной сводкой: строка на день, сколько записей у каждого барбера."""
    tz = settings.schedule.tz
    total = sum(b.price for b in items)
    lines = [f"🗓 <b>Ближайшие 7 дней</b> · {len(items)} зап. · {texts.price(total)}", ""]
    for d in days:
        mine = [b for b in items if texts.local(b.start_at, tz).date() == d]
        label = f"<b>{texts.day_short(d)}</b>"
        if f"{d:%Y-%m-%d}" in closed:
            lines.append(f"{label} · выходной")
        elif not mine:
            lines.append(f"{label} · записей нет")
        else:
            per = {br.id: sum(1 for b in mine if b.barber_id == br.id) for br in barbers}
            who = ", ".join(f"{escape(br.name)} {per[br.id]}" for br in barbers if per[br.id])
            lines.append(f"{label} · {len(mine)} зап. · {texts.price(sum(b.price for b in mine))} · {who}")
    return "\n".join(lines) + "\n\nНажмите на день, чтобы открыть его."


def leads_view(leads: list[Lead], viewer_id: int, is_owner: bool, settings: Settings) -> str:
    if not leads:
        return "📩 <b>Заявки</b>\n\nЗаявок пока нет."
    tz = settings.schedule.tz
    blocks = []
    for lead in leads:
        hide = hide_for(viewer_id, is_owner, lead.user_id)
        if hide and lead.user_id > 0:  # настоящий посетитель демо: в тексте может быть личное
            body = "<i>текст скрыт в демо</i>"
        else:
            body = escape(lead.text if len(lead.text) <= 100 else lead.text[:99] + "…")
        blocks.append(f"<b>#{lead.id}</b> · {texts.local(lead.created_at, tz):%d.%m %H:%M} · "
                      f"{texts.lead_client_line(lead, hide)}\n{body}")
    return "📩 <b>Заявки</b> · последние 10\n\n" + "\n\n".join(blocks)


@router.message(F.text.regexp(r"^/b(\d+)(@\w+)?$").as_("match"))
async def booking_by_command(message: Message, match, db: Database, settings: Settings,
                             can_admin: bool, is_owner: bool) -> None:
    if not can_admin:
        return
    text, markup = await card(int(match.group(1)), message.from_user.id, is_owner, db, settings)
    await message.answer(text, reply_markup=markup)


async def card(booking_id: int, viewer_id: int, is_owner: bool, db: Database, settings: Settings):
    b = await db.get_booking(booking_id)
    if b is None:
        return "Запись не найдена.", kb.admin_back()
    hide = hide_for(viewer_id, is_owner, b.user_id)
    tz = settings.schedule.tz
    status = STATUS.get(b.status, b.status)
    if b.client_confirmed:
        status += ", клиент подтвердил визит ✅"
    barber = f"✂️ {escape(b.barber_name)}\n" if b.barber_name else ""
    text = (
        f"📌 <b>Запись #{b.id}</b>\n\n"
        f"💈 {escape(texts.service_title(b.service_code))}\n"
        f"{barber}"
        f"🗓 {texts.when(b, tz)}\n"
        f"👤 {texts.client_line(b, hide)}\n"
        f"{texts.payment_line(b)}\n"
        f"Статус: {status}\n"
        f"Создана: {texts.local(b.created_at, tz):%d.%m %H:%M}"
    )
    can_cancel = b.status in ("confirmed", "pending_payment") and b.start_at > time.time() and not hide
    return text, kb.admin_booking(b.id, can_cancel, None if hide else b.username)


@router.message(BroadcastForm.text)
async def broadcast_text(message: Message, state: FSMContext, db: Database, is_owner: bool) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Рассылка отменена.")
        return
    if not message.text:
        await message.answer("Пока поддерживается только текст. Пришлите сообщение текстом.")
        return
    await state.update_data(text=message.html_text)
    recipients = len(await db.active_user_ids()) if is_owner else 1
    note = "" if is_owner else "\n\n🧪 В демо рассылка придёт только вам."
    await message.answer(f"Так увидят клиенты:\n\n{message.html_text}{note}",
                         reply_markup=kb.broadcast_confirm(recipients))


@router.callback_query(kb.AdmCb.filter())
async def admin_actions(cb: CallbackQuery, callback_data: kb.AdmCb, db: Database, settings: Settings, bot: Bot,
                        state: FSMContext, can_admin: bool, is_owner: bool) -> None:
    if not can_admin:
        await cb.answer("Нет доступа", show_alert=True)
        return
    action, arg = callback_data.action, callback_data.arg
    tz = settings.schedule.tz
    now = datetime.now(tz)
    now_ts = int(now.timestamp())
    viewer = cb.from_user.id

    if action == "menu":
        await state.clear()
        await edit_cb(cb, header(is_owner), kb.admin_menu())

    elif action == "day":
        offset = int(arg) if arg.lstrip("-").isdigit() else 0
        d = now.date() + timedelta(days=offset)
        start = midnight(d, settings)
        items = await db.bookings_between(start, start + 86400, now_ts)
        barbers = await db.barbers(include_hidden=True)
        text = day_view(d, items, barbers, await db.days_off(), await db.closed_days(), settings, viewer, is_owner)
        await edit_cb(cb, "📋 " + text + LEGEND, kb.admin_day_back())

    elif action == "week":
        days = [now.date() + timedelta(days=i) for i in range(7)]
        start = midnight(days[0], settings)
        items = await db.bookings_between(start, start + 7 * 86400, now_ts)
        barbers = await db.barbers(include_hidden=True)
        await edit_cb(cb, week_view(days, items, barbers, await db.closed_days(), settings),
                      kb.admin_week([(i, texts.day_short(d)) for i, d in enumerate(days)]))

    elif action == "leads":
        await edit_cb(cb, leads_view(await db.recent_leads(10), viewer, is_owner, settings), kb.admin_back())

    elif action == "card":
        text, markup = await card(int(arg), viewer, is_owner, db, settings)
        await edit_cb(cb, text, markup)

    elif action == "cancel":
        b = await db.get_booking(int(arg))
        if b is None or hide_for(viewer, is_owner, b.user_id):
            await cb.answer(DEMO_LOCKED if b else "Запись не найдена", show_alert=True)
            return
        if await db.cancel_booking(b.id, by="owner"):
            refund = "\nОнлайн-оплату вернём в течение 1-3 дней." if b.paid else ""
            try:
                await bot.send_message(
                    b.user_id,
                    f"😔 Ваша запись на {texts.when(b, tz)} отменена администратором. Извините за неудобства!"
                    f"{refund}\n\nВыбрать другое время: /book",
                )
            except TelegramAPIError as e:
                log.warning("Can't notify client %s about cancellation: %s", b.user_id, e)
        text, markup = await card(b.id, viewer, is_owner, db, settings)
        await edit_cb(cb, text + ("\n\n⚠️ Была онлайн-оплата, оформите возврат в кабинете провайдера."
                                  if b.paid else ""), markup)

    elif action == "closed":
        await edit_cb(cb, *await closed_view(db, settings))

    elif action == "toggle":
        if not is_owner:
            await cb.answer(DEMO_LOCKED, show_alert=True)
            return
        d = date.fromisoformat(arg)
        if arg not in await db.closed_days():
            start = midnight(d, settings)
            if await db.bookings_between(start, start + 86400, now_ts):
                await cb.answer("На этот день есть записи. Сначала отмените их: «📋 Сегодня» или «🗓 7 дней».",
                                show_alert=True)
                return
        await db.toggle_closed_day(arg)
        await edit_cb(cb, *await closed_view(db, settings))

    elif action == "broadcast":
        await state.set_state(BroadcastForm.text)
        await edit_cb(cb, "📣 Пришлите текст рассылки одним сообщением, можно с эмодзи и форматированием.\n\n"
                          "Например: «Свободные окна на завтра: 12:00 и 16:30. Записаться: /book»",
                      kb.admin_back())

    elif action == "broadcast_send":
        text = (await state.get_data()).get("text")
        await state.clear()
        if not text:
            await cb.answer("Текст потерялся (бот перезапускался), начните заново", show_alert=True)
            return
        recipients = await db.active_user_ids() if is_owner else [viewer]
        await edit_cb(cb, f"📣 Отправляю {len(recipients)}…")
        sent, blocked, failed = await broadcast(bot, db, recipients, text)
        await cb.message.answer(
            f"📣 <b>Рассылка завершена</b>\nДоставлено: {sent}\nЗаблокировали бота: {blocked}\nОшибок: {failed}",
            reply_markup=kb.admin_back(),
        )

    elif action == "stats":
        since = now_ts - 30 * 86400
        s = await db.stats(now_ts, since)
        total = s["visits"] + s["cancelled"]
        cancel_rate = f" ({round(100 * s['cancelled'] / total)}%)" if total else ""
        top = texts.service_title(s["top_service"]) if s["top_service"] else "нет данных"
        load = " · ".join(f"{escape(name)} {n}" for name, n in await db.barber_load(since)) or "нет данных"
        await edit_cb(
            cb,
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Клиентов в базе: {s['users']}\n"
            f"📅 Предстоящих записей: {s['upcoming']}\n\n"
            f"<b>За 30 дней</b>\n"
            f"✂️ Записей: {s['visits']}\n"
            f"💰 Сумма записей: {texts.price(s['revenue'])}\n"
            f"💳 Оплачено онлайн: {texts.price(s['paid_online'])}\n"
            f"❌ Отмен: {s['cancelled']}{cancel_rate}\n"
            f"🏆 Популярная услуга: {escape(top)}\n"
            f"💈 Загрузка барберов: {load}",
            kb.admin_back(),
        )

    elif action == "csv":
        rows = await db.all_bookings()
        data = export_csv(rows, settings, viewer, is_owner)
        await cb.message.answer_document(
            BufferedInputFile(data, filename=f"bookings_{now:%Y-%m-%d}.csv"),
            caption=f"📤 {len(rows)} записей. Открывается в Excel и Google Таблицах.",
        )

    await cb.answer()


async def closed_view(db: Database, settings: Settings):
    closed = await db.closed_days()
    today = datetime.now(settings.schedule.tz).date()
    days = [today + timedelta(days=i) for i in range(14)]  # две недели вперёд
    options = [(d, f"{d:%Y-%m-%d}" in closed) for d in days if d.weekday() in settings.schedule.workdays]
    return ("🚫 <b>Выходные дни салона</b>\n\nНажмите на день, чтобы закрыть или открыть запись.\n"
            "✅ принимаем записи, 🚫 выходной."), kb.admin_closed_days(options)


def export_csv(rows: list[Booking], settings: Settings, viewer_id: int, is_owner: bool) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")  # «;»: так русский Excel сразу разбивает по колонкам
    w.writerow(["id", "дата", "время", "услуга", "барбер", "цена", "статус", "оплачено онлайн", "клиент",
                "username", "телефон", "создана"])
    tz = settings.schedule.tz
    for b in rows:
        hide = hide_for(viewer_id, is_owner, b.user_id)
        start = texts.local(b.start_at, tz)
        w.writerow([
            b.id, f"{start:%d.%m.%Y}", f"{start:%H:%M}", texts.service_title(b.service_code), b.barber_name or "",
            b.price, STATUS.get(b.status, b.status), b.paid // 100,
            texts.mask(b.first_name) if hide else b.first_name,
            "" if hide else (b.username or ""), "" if hide else (b.phone or ""),
            f"{texts.local(b.created_at, tz):%d.%m.%Y %H:%M}",
        ])
    return buf.getvalue().encode("utf-8-sig")  # BOM, чтобы Excel не показал кракозябры


async def broadcast(bot: Bot, db: Database, user_ids: list[int], text: str) -> tuple[int, int, int]:
    sent = blocked = failed = 0
    for uid in user_ids:
        for _ in range(3):
            try:
                await bot.send_message(uid, text)
                sent += 1
                break
            except TelegramRetryAfter as e:  # Telegram попросил притормозить: ждём и повторяем
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await db.mark_blocked(uid)
                blocked += 1
                break
            except TelegramBadRequest as e:
                log.warning("Broadcast to %s failed: %s", uid, e)
                failed += 1
                break
        else:
            failed += 1
        await asyncio.sleep(0.05)  # примерно 20 сообщений в секунду, лимит Telegram 30
    return sent, blocked, failed
