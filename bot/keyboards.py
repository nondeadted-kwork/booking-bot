"""Клавиатуры и callback-данные.

Весь путь записи (услуга → барбер → день → время) живёт в callback_data, а не в памяти бота.
Поэтому кнопки продолжают работать после перезапуска.
"""
from __future__ import annotations

from datetime import date, datetime

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import texts
from .config import SERVICES, Service


class ServiceCb(CallbackData, prefix="svc"):
    code: str


class BarberCb(CallbackData, prefix="brb"):
    code: str
    m: int  # id барбера; 0 (slots.ANY) = любой свободный


class DayCb(CallbackData, prefix="day"):
    code: str
    m: int
    day: str  # YYYYMMDD


class SlotCb(CallbackData, prefix="slot"):
    code: str
    m: int
    ts: int


class ConfirmCb(CallbackData, prefix="ok"):
    code: str
    m: int
    ts: int
    pay: bool


class BackCb(CallbackData, prefix="back"):
    to: str  # services | days
    code: str = ""
    m: int = 0


class MyCb(CallbackData, prefix="my"):
    action: str  # list | cancel | cancel_yes | come
    id: int = 0


class AdmCb(CallbackData, prefix="adm"):
    action: str
    arg: str = ""


class NoopCb(CallbackData, prefix="noop"):
    reason: str = ""


BTN_BOOK = "📅 Записаться"
BTN_MY = "🗂 Мои записи"
BTN_PRICES = "💈 Услуги и цены"
BTN_CONTACTS = "📍 Контакты"
BTN_ADMIN = "👀 Панель владельца"
BTN_SKIP_PHONE = "Пропустить"


def main_menu(can_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_BOOK)],
        [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_PRICES)],
        [KeyboardButton(text=BTN_CONTACTS)] + ([KeyboardButton(text=BTN_ADMIN)] if can_admin else []),
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Выберите действие")


def ask_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить номер", request_contact=True)],
            [KeyboardButton(text=BTN_SKIP_PHONE)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def services() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in SERVICES:
        kb.button(text=f"{s.title} · {texts.price(s.price)}", callback_data=ServiceCb(code=s.code))
    kb.adjust(1)
    return kb.as_markup()


def barber_choice(service: Service, rows: list[tuple[int, str, bool]]) -> InlineKeyboardMarkup:
    """rows: (id барбера или ANY, подпись, есть ли окна на неделе). Без окон кнопка неактивна."""
    kb = InlineKeyboardBuilder()
    for barber_id, label, available in rows:
        callback = BarberCb(code=service.code, m=barber_id) if available else NoopCb(reason="nofree")
        kb.button(text=label, callback_data=callback)
    kb.button(text="« К услугам", callback_data=BackCb(to="services"))
    kb.adjust(1)
    return kb.as_markup()


def days(service: Service, m: int, options: list[tuple[date, int, str]], to_barbers: bool) -> InlineKeyboardMarkup:
    """options: (день, свободных окон, статус open | closed | off). to_barbers: назад к выбору барбера."""
    kb = InlineKeyboardBuilder()
    for d, free, status in options:
        label = texts.day_short(d)
        if status == "closed":
            kb.button(text=f"{label} · выходной", callback_data=NoopCb(reason="closed"))
        elif status == "off":
            kb.button(text=f"{label} · отпуск", callback_data=NoopCb(reason="off"))
        elif free == 0:
            kb.button(text=f"{label} · мест нет", callback_data=NoopCb(reason="full"))
        else:
            kb.button(text=f"{label} · {texts.plural(free, 'окно', 'окна', 'окон')}",
                      callback_data=DayCb(code=service.code, m=m, day=f"{d:%Y%m%d}"))
    if to_barbers:
        kb.button(text="« Другой барбер", callback_data=ServiceCb(code=service.code))
    else:
        kb.button(text="« К услугам", callback_data=BackCb(to="services"))
    kb.adjust(*([2] * ((len(options) + 1) // 2)), 1)
    return kb.as_markup()


def slots(service: Service, m: int, free: list[datetime]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in free:
        kb.button(text=f"{t:%H:%M}", callback_data=SlotCb(code=service.code, m=m, ts=int(t.timestamp())))
    kb.button(text="« Другой день", callback_data=BackCb(to="days", code=service.code, m=m))
    kb.adjust(*([4] * ((len(free) + 3) // 4)), 1)
    return kb.as_markup()


def confirm(service: Service, m: int, ts: int, payments: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if payments:
        kb.button(text=f"💳 Оплатить онлайн {texts.price(service.price)}",
                  callback_data=ConfirmCb(code=service.code, m=m, ts=ts, pay=True))
        kb.button(text="✅ Записаться, оплачу на месте",
                  callback_data=ConfirmCb(code=service.code, m=m, ts=ts, pay=False))
    else:
        kb.button(text="✅ Подтвердить запись", callback_data=ConfirmCb(code=service.code, m=m, ts=ts, pay=False))
    kb.button(text="« Другое время", callback_data=BackCb(to="days", code=service.code, m=m))
    kb.adjust(1)
    return kb.as_markup()


def booking_actions(booking_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отменить запись", callback_data=MyCb(action="cancel", id=booking_id))
    kb.button(text="🗂 Все мои записи", callback_data=MyCb(action="list"))
    kb.adjust(1)
    return kb.as_markup()


def reminder(booking_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Приду", callback_data=MyCb(action="come", id=booking_id))
    kb.button(text="❌ Не смогу", callback_data=MyCb(action="cancel", id=booking_id))
    kb.adjust(2)
    return kb.as_markup()


def cancel_confirm(booking_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, отменить", callback_data=MyCb(action="cancel_yes", id=booking_id))
    kb.button(text="Нет, оставить", callback_data=MyCb(action="list"))
    kb.adjust(2)
    return kb.as_markup()


def my_list(ids: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for booking_id, label in ids:
        kb.button(text=f"❌ Отменить {label}", callback_data=MyCb(action="cancel", id=booking_id))
    kb.button(text="📅 Новая запись", callback_data=BackCb(to="services"))
    kb.adjust(1)
    return kb.as_markup()


# --- панель владельца ---------------------------------------------------------

def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Сегодня", callback_data=AdmCb(action="day", arg="0"))
    kb.button(text="📆 Завтра", callback_data=AdmCb(action="day", arg="1"))
    kb.button(text="🗓 7 дней", callback_data=AdmCb(action="week"))
    kb.button(text="🚫 Выходные", callback_data=AdmCb(action="closed"))
    kb.button(text="📣 Рассылка", callback_data=AdmCb(action="broadcast"))
    kb.button(text="📊 Статистика", callback_data=AdmCb(action="stats"))
    kb.button(text="📤 Выгрузка CSV", callback_data=AdmCb(action="csv"))
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def admin_back() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="« Панель", callback_data=AdmCb(action="menu"))
    return kb.as_markup()


def admin_booking(booking_id: int, can_cancel: bool, username: str | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_cancel:
        kb.button(text="❌ Отменить запись", callback_data=AdmCb(action="cancel", arg=str(booking_id)))
    if username:
        kb.button(text="💬 Написать клиенту", url=f"https://t.me/{username}")
    kb.button(text="« Панель", callback_data=AdmCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def admin_closed_days(options: list[tuple[date, bool]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d, closed in options:
        mark = "🚫" if closed else "✅"
        kb.button(text=f"{mark} {texts.day_short(d)}", callback_data=AdmCb(action="toggle", arg=f"{d:%Y-%m-%d}"))
    kb.button(text="« Панель", callback_data=AdmCb(action="menu"))
    kb.adjust(*([3] * ((len(options) + 2) // 3)), 1)
    return kb.as_markup()


def broadcast_confirm(recipients: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"📣 Отправить ({recipients})", callback_data=AdmCb(action="broadcast_send"))
    kb.button(text="Отмена", callback_data=AdmCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()
