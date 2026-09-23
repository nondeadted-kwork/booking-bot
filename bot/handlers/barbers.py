"""Барберы в панели владельца: список, карточка, рабочие дни, отпуск, скрытие и добавление.

Барберы общие для всех посетителей демо, поэтому посетитель видит все экраны, но ничего не сохраняет.
Добавление он проходит до карточки «Так его увидят клиенты», замок срабатывает на «Сохранить».
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import keyboards as kb
from .. import texts
from ..config import Settings
from ..db import Barber, Booking, Database
from ..notify import edit_cb
from .admin import DEMO_LOCKED, midnight

router = Router(name="barbers")

NAME_MIN, NAME_MAX = 2, 30
ABOUT_MAX = 80
OFF_DAYS_AHEAD = 14
LOST = "Данные потерялись: бот перезапускался. Начните заново с «➕ Добавить барбера»."


class BarberForm(StatesGroup):
    name = State()         # новый барбер: имя
    about = State()        # новый барбер: описание
    days = State()         # новый барбер: рабочие дни кнопками
    edit_name = State()
    edit_about = State()


def clean(text: str) -> str:
    """Убирает лишние пробелы и переносы: имя и описание показываются в одну строку."""
    return " ".join(text.split())


def conflict_text(head: str, clash: list[Booking], tz: ZoneInfo) -> str:
    """Текст всплывающего окна о мешающих записях. Telegram показывает в нём не больше 200 символов."""
    shown = ", ".join(f"{texts.local(b.start_at, tz):%d.%m %H:%M}" for b in clash[:3])
    more = f" и ещё {len(clash) - 3}" if len(clash) > 3 else ""
    return f"{head}: {shown}{more}. Сначала отмените их."[:200]


# --- экраны -------------------------------------------------------------------

def list_screen(barbers: list[Barber]) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"✂️ <b>{escape(b.name)}</b> · {texts.days_span(b.workdays)}" if b.active
             else f"🙈 <b>{escape(b.name)}</b> · скрыт из записи" for b in barbers]
    text = ("💈 <b>Барберы</b>\n\n" + "\n".join(lines)
            + "\n\nНажмите на барбера, чтобы поменять график, отпуск или описание.")
    return text, kb.barber_list([(b.id, f"{'✂️' if b.active else '🙈'} {b.name}") for b in barbers])


async def card_screen(b: Barber, db: Database, settings: Settings, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    today = datetime.now(settings.schedule.tz).date()
    off = sorted(d for d in (await db.days_off()).get(b.id, frozenset()) if d >= today)
    upcoming = len(await db.barber_upcoming(b.id, int(time.time())))
    off_text = ", ".join(f"{d:%d.%m}" for d in off) if off else "нет"
    text = (
        f"✂️ <b>{escape(b.name)}</b>\n"
        f"{escape(b.about) if b.about else '<i>без описания</i>'}\n\n"
        f"📅 Рабочие дни: {texts.days_span(b.workdays)}\n"
        f"🌴 Отпуск: {off_text}\n"
        f"📋 Будущих записей: {upcoming}"
    )
    if not b.active:
        text += "\n\n🙈 Скрыт из записи: клиенты его не видят."
    return text + note, kb.barber_card(b.id, b.active)


def days_screen(b: Barber) -> tuple[str, InlineKeyboardMarkup]:
    text = (f"📅 <b>Рабочие дни: {escape(b.name)}</b>\n\n"
            "Нажмите на день недели, чтобы включить или выключить его. "
            "Отпуск на конкретные даты ставится кнопкой «🌴 Отпуск».")
    return text, kb.weekday_toggles(b.workdays, "day", b.id, kb.BarbCb(action="card", id=b.id))


async def off_screen(b: Barber, db: Database, settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    today = datetime.now(settings.schedule.tz).date()
    off = (await db.days_off()).get(b.id, frozenset())
    days = [today + timedelta(days=i) for i in range(OFF_DAYS_AHEAD)]
    options = [(d, d in off) for d in days if d.weekday() in b.workdays and d.weekday() in settings.schedule.workdays]
    text = (f"🌴 <b>Отпуск: {escape(b.name)}</b>\n\n"
            "✅ работает, 🌴 отпуск. Нажмите на дату, чтобы переключить.\n"
            "Дату, на которую уже есть записи, в отпуск поставить нельзя.")
    return text, kb.days_off_toggles(b.id, options)


def new_days_screen(name: str, days: list[int]) -> tuple[str, InlineKeyboardMarkup]:
    text = f"📅 В какие дни работает <b>{escape(name)}</b>? Нажмите на день, чтобы включить или выключить его."
    return text, kb.weekday_toggles(frozenset(days), "nday", 0, kb.BarbCb(action="ndone"))


def preview_screen(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    about = f": {escape(data['about'])}" if data.get("about") else ""
    text = ("👀 <b>Так его увидят клиенты при записи:</b>\n\n"
            f"• <b>{escape(data['name'])}</b>{about}\n"
            f"📅 {texts.days_span(data['days'])}\n\n"
            "Сохранить?")
    return text, kb.barber_save()


# --- изменения ------------------------------------------------------------------

async def toggle_workday(b: Barber, arg: str, db: Database, settings: Settings) -> str | None:
    """Включает или выключает день недели. Возвращает текст запрета или None."""
    if not arg.isdigit() or int(arg) > 6:
        return None
    wd = int(arg)
    if wd not in b.workdays:
        await db.update_barber(b.id, workdays=b.workdays | {wd})
        return None
    rest = b.workdays - {wd}
    if not rest:
        return "Оставьте хотя бы один рабочий день или скройте барбера из записи."
    tz = settings.schedule.tz
    clash = [x for x in await db.barber_upcoming(b.id, int(time.time()))
             if texts.local(x.start_at, tz).weekday() == wd]
    if clash:
        return conflict_text(f"На {texts.WEEKDAYS[wd]} у барбера есть записи", clash, tz)
    await db.update_barber(b.id, workdays=rest)
    return None


async def toggle_vacation(b: Barber, arg: str, db: Database, settings: Settings) -> str | None:
    """Ставит или снимает отпуск на дату. Возвращает текст запрета или None."""
    try:
        day = date.fromisoformat(arg)
    except ValueError:
        return None
    if day not in (await db.days_off()).get(b.id, frozenset()):
        start = midnight(day, settings)
        clash = await db.bookings_between(start, start + 86400, int(time.time()), barber_id=b.id)
        if clash:
            return conflict_text(f"На {day:%d.%m} у барбера есть записи", clash, settings.schedule.tz)
    await db.toggle_day_off(b.id, day.isoformat())
    return None


# --- кнопки -------------------------------------------------------------------

@router.callback_query(kb.BarbCb.filter())
async def barber_actions(cb: CallbackQuery, callback_data: kb.BarbCb, db: Database, settings: Settings,
                         state: FSMContext, can_admin: bool, is_owner: bool) -> None:
    if not can_admin:
        await cb.answer("Нет доступа", show_alert=True)
        return
    action = callback_data.action

    if action == "list":
        await state.clear()
        await edit_cb(cb, *list_screen(await db.barbers(include_hidden=True)))
        await cb.answer()
        return
    if action in ("new", "skip", "nday", "ndone", "save"):
        await new_barber(cb, action, callback_data.arg, db, settings, state, is_owner)
        return

    b = await db.get_barber(callback_data.id)
    if b is None:
        await cb.answer("Барбер не найден", show_alert=True)
        return

    if action == "card":
        await state.clear()
        await edit_cb(cb, *await card_screen(b, db, settings))
    elif action == "days":
        await edit_cb(cb, *days_screen(b))
    elif action == "off":
        await edit_cb(cb, *await off_screen(b, db, settings))
    elif not is_owner:
        # Всё остальное меняет барбера, а он общий для всех посетителей демо.
        await cb.answer(DEMO_LOCKED, show_alert=True)
        return
    elif action == "day":
        refusal = await toggle_workday(b, callback_data.arg, db, settings)
        if refusal:
            await cb.answer(refusal, show_alert=True)
            return
        await edit_cb(cb, *days_screen(await db.get_barber(b.id)))
    elif action == "offday":
        refusal = await toggle_vacation(b, callback_data.arg, db, settings)
        if refusal:
            await cb.answer(refusal, show_alert=True)
            return
        await edit_cb(cb, *await off_screen(b, db, settings))
    elif action == "hide":
        if len(await db.barbers()) <= 1:
            await cb.answer("Это последний барбер в записи. Сначала добавьте другого, иначе клиенты "
                            "не смогут записаться.", show_alert=True)
            return
        await db.update_barber(b.id, active=False)
        upcoming = len(await db.barber_upcoming(b.id, int(time.time())))
        note = f"\n\nБудущие записи ({upcoming}) остаются, новых к нему не будет." if upcoming else ""
        await edit_cb(cb, *await card_screen(await db.get_barber(b.id), db, settings, note))
    elif action == "show":
        await db.update_barber(b.id, active=True)
        await edit_cb(cb, *await card_screen(await db.get_barber(b.id), db, settings))
    elif action in ("name", "about"):
        await state.set_state(BarberForm.edit_name if action == "name" else BarberForm.edit_about)
        await state.update_data(barber_id=b.id)
        prompt = (f"Пришлите новое имя, от {NAME_MIN} до {NAME_MAX} символов." if action == "name" else
                  f"Пришлите новое описание, до {ABOUT_MAX} символов. Например: «Фейды и бритьё, 5 лет в профессии».")
        await edit_cb(cb, f"✂️ <b>{escape(b.name)}</b>\n\n{prompt}", kb.barber_back(b.id))
    await cb.answer()


async def new_barber(cb: CallbackQuery, action: str, arg: str, db: Database, settings: Settings,
                     state: FSMContext, is_owner: bool) -> None:
    """Шаги добавления барбера кнопками: пропуск описания, рабочие дни, карточка, сохранение."""
    if action == "new":
        await state.clear()
        await state.set_state(BarberForm.name)
        await edit_cb(cb, f"➕ <b>Новый барбер</b>\n\nКак его зовут? Пришлите имя, от {NAME_MIN} до {NAME_MAX} символов.",
                      kb.barber_cancel())
        await cb.answer()
        return
    data = await state.get_data()
    if "name" not in data:
        await state.clear()
        await cb.answer(LOST, show_alert=True)
        return
    if action == "skip":
        days = data.get("days") or sorted(settings.schedule.workdays)
        await state.update_data(about="", days=days)
        await state.set_state(BarberForm.days)
        await edit_cb(cb, *new_days_screen(data["name"], days))
    elif action == "nday":
        days = set(data.get("days", []))
        if arg.isdigit() and int(arg) <= 6:
            days ^= {int(arg)}
        await state.update_data(days=sorted(days))
        await edit_cb(cb, *new_days_screen(data["name"], sorted(days)))
    elif action == "ndone":
        if not data.get("days"):
            await cb.answer("Выберите хотя бы один рабочий день.", show_alert=True)
            return
        await edit_cb(cb, *preview_screen(data))
    elif action == "save":
        if not data.get("days"):
            await cb.answer("Выберите хотя бы один рабочий день.", show_alert=True)
            return
        await state.clear()
        if not is_owner:
            await cb.answer(DEMO_LOCKED, show_alert=True)
            await edit_cb(cb, "🧪 В демо барбер не сохраняется: все посетители видят один и тот же салон. "
                              "У владельца новый барбер сразу появляется в записи.", kb.barber_list_back())
            return
        b = await db.add_barber(data["name"], data.get("about", ""), data["days"])
        await edit_cb(cb, *await card_screen(b, db, settings, "\n\n✅ Сохранено. Барбер уже доступен для записи."))
    await cb.answer()


# --- текстовые шаги -------------------------------------------------------------

@router.message(BarberForm.name, F.text)
async def new_name(message: Message, state: FSMContext) -> None:
    name = clean(message.text)
    if not NAME_MIN <= len(name) <= NAME_MAX:
        await message.answer(f"Имя должно быть от {NAME_MIN} до {NAME_MAX} символов. Попробуйте ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(BarberForm.about)
    await message.answer(f"Пара слов о барбере для клиентов, до {ABOUT_MAX} символов.\n"
                         f"Например: «Фейды и бритьё, 5 лет в профессии».", reply_markup=kb.barber_skip_about())


@router.message(BarberForm.about, F.text)
async def new_about(message: Message, state: FSMContext, settings: Settings) -> None:
    about = clean(message.text)
    if len(about) > ABOUT_MAX:
        await message.answer(f"Получилось {len(about)} символов, а можно до {ABOUT_MAX}. Сократите, пожалуйста.")
        return
    data = await state.get_data()
    days = data.get("days") or sorted(settings.schedule.workdays)
    await state.update_data(about=about, days=days)
    await state.set_state(BarberForm.days)
    text, markup = new_days_screen(data["name"], days)
    await message.answer(text, reply_markup=markup)


@router.message(BarberForm.edit_name, F.text)
async def edit_name(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    name = clean(message.text)
    if not NAME_MIN <= len(name) <= NAME_MAX:
        await message.answer(f"Имя должно быть от {NAME_MIN} до {NAME_MAX} символов. Попробуйте ещё раз.")
        return
    await save_edit(message, state, db, settings, name=name)


@router.message(BarberForm.edit_about, F.text)
async def edit_about(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    about = clean(message.text)
    if len(about) > ABOUT_MAX:
        await message.answer(f"Получилось {len(about)} символов, а можно до {ABOUT_MAX}. Сократите, пожалуйста.")
        return
    await save_edit(message, state, db, settings, about=about)


async def save_edit(message: Message, state: FSMContext, db: Database, settings: Settings, **fields) -> None:
    barber_id = (await state.get_data()).get("barber_id")
    await state.clear()
    b = await db.get_barber(barber_id) if barber_id is not None else None
    if b is None:
        await message.answer("Барбер не найден. Откройте «👀 Панель владельца» → «💈 Барберы».")
        return
    await db.update_barber(b.id, **fields)
    text, markup = await card_screen(await db.get_barber(b.id), db, settings, "\n\n✅ Сохранено.")
    await message.answer(text, reply_markup=markup)


@router.message(BarberForm.days)
async def days_expected(message: Message) -> None:
    await message.answer("Отметьте дни кнопками выше и нажмите «Готово».")


@router.message(BarberForm.name)
@router.message(BarberForm.about)
@router.message(BarberForm.edit_name)
@router.message(BarberForm.edit_about)
async def text_expected(message: Message) -> None:
    await message.answer("Пришлите, пожалуйста, текстом.")
