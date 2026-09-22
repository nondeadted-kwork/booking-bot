"""Настройки бота: секреты из .env, бизнес-параметры — здесь же, в коде."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Service:
    code: str      # короткий id, уходит в callback_data (≤ 10 символов)
    title: str
    minutes: int   # кратно шагу сетки (SLOT_STEP_MIN)
    price: int     # ₽


# Услуги под конкретного клиента правятся здесь.
SERVICES: tuple[Service, ...] = (
    Service("cut", "Мужская стрижка", 60, 1500),
    Service("beard", "Моделирование бороды", 30, 900),
    Service("combo", "Стрижка + борода", 90, 2200),
    Service("kids", "Детская стрижка", 30, 1000),
)
SERVICES_BY_CODE = {s.code: s for s in SERVICES}


@dataclass(frozen=True)
class Schedule:
    tz: ZoneInfo = ZoneInfo("Europe/Moscow")
    work_start: time = time(10, 0)
    work_end: time = time(20, 0)
    workdays: frozenset[int] = frozenset({0, 1, 2, 3, 4, 5})  # пн–сб
    slot_step_min: int = 30
    days_ahead: int = 7          # сколько дней вперёд показывать
    min_lead_min: int = 60       # нельзя записаться ближе, чем за час


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_ids: frozenset[int]
    demo_mode: bool = False
    payment_token: str | None = None
    test_card_hint: str = "1111 1111 1111 1026, срок 12/30, CVC 000"
    db_path: str = "data/bot.db"
    business_name: str = "Барбершоп «Лезвие»"
    business_address: str = "Москва, ул. Примерная, 1"
    business_phone: str = "+7 900 000-00-00"
    remind_before_min: int = 60
    payment_hold_min: int = 15
    tick_seconds: int = 30
    schedule: Schedule = field(default_factory=Schedule)

    @property
    def payments_enabled(self) -> bool:
        return bool(self.payment_token)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            sys.exit("BOT_TOKEN не задан. Скопируйте .env.example в .env и вставьте токен от @BotFather.")
        try:
            owners = frozenset(int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x)
        except ValueError:
            sys.exit("OWNER_IDS должен быть списком числовых id через запятую, например: 123456789,987654321")
        if not owners:
            print("⚠️  OWNER_IDS пуст — уведомления о записях никому не придут.", file=sys.stderr)

        tz_name = os.getenv("TZ_NAME", "Europe/Moscow")
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            sys.exit(f"TZ_NAME={tz_name!r} — неизвестный часовой пояс. Пример: Europe/Moscow, Asia/Yekaterinburg")
        return cls(
            bot_token=token,
            owner_ids=owners,
            demo_mode=os.getenv("DEMO_MODE", "0").lower() in {"1", "true", "yes"},
            payment_token=os.getenv("PAYMENT_TOKEN", "").strip() or None,
            test_card_hint=os.getenv("TEST_CARD_HINT", cls.test_card_hint),
            db_path=os.getenv("DB_PATH", "data/bot.db"),
            business_name=os.getenv("BUSINESS_NAME", cls.business_name),
            business_address=os.getenv("BUSINESS_ADDRESS", cls.business_address),
            business_phone=os.getenv("BUSINESS_PHONE", cls.business_phone),
            remind_before_min=int(os.getenv("REMIND_BEFORE_MIN", "60")),
            schedule=Schedule(tz=tz),
        )
