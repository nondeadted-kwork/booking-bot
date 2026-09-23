"""Форматирование: дни недели, ближайшее время, карточка записи, описание бота."""
import ast
import pathlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bot import reminders, texts
from bot.config import Settings
from bot.db import Booking

MSK = ZoneInfo("Europe/Moscow")


def sample_booking(**kw) -> Booking:
    start = int(datetime(2026, 9, 24, 10, 0, tzinfo=MSK).timestamp())
    data = dict(id=7, user_id=1, barber_id=1, service_code="cut", start_at=start, end_at=start + 3600, price=1500,
                status="confirmed", paid=0, hold_until=None, reminded_at=None, client_confirmed=False,
                cancelled_by=None, created_at=start - 86400, first_name="Иван", username="ivan", phone="+7900",
                barber_name="Артём")
    data.update(kw)
    return Booking(**data)


def test_days_span():
    assert texts.days_span({0, 1, 2, 3, 4}) == "пн-пт"
    assert texts.days_span({0, 1, 4, 5, 6}) == "пн, вт, пт-вс"
    assert texts.days_span(range(7)) == "ежедневно"
    assert texts.days_span({2}) == "ср"
    assert texts.days_span(set()) == "нет рабочих дней"


def test_nearest_labels():
    today = date(2026, 9, 23)
    assert texts.nearest(datetime(2026, 9, 23, 15, 30), today) == "сегодня 15:30"
    assert texts.nearest(datetime(2026, 9, 24, 10, 0), today) == "завтра 10:00"
    assert texts.nearest(datetime(2026, 9, 25, 11, 0), today) == "пт 11:00"


def test_booking_card_shows_barber_and_uses_hyphen():
    card = texts.booking_card(sample_booking(), MSK, "Москва")
    assert "✂️ Артём" in card and "10:00-11:00" in card
    assert "✂️" not in texts.booking_card(sample_booking(barber_name=None), MSK, "Москва")


def test_payment_line_without_dash():
    assert texts.payment_line(sample_booking()) == "💰 1 500 ₽, оплата на месте"


def test_bot_descriptions_fit_telegram_limits():
    for demo_mode in (True, False):
        settings = Settings(bot_token="1:T", owner_ids=frozenset(), demo_mode=demo_mode, business_name="Б" * 600)
        assert len(texts.bot_description(settings)) <= 512
        assert len(texts.bot_short_description(settings)) <= 120
    demo = Settings(bot_token="1:T", owner_ids=frozenset(), demo_mode=True)
    assert "Демо бота записи" in texts.bot_description(demo)


BOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "bot"


def docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def test_no_long_dashes_in_bot_texts():
    """Тексты бота видят покупатели на скринах и в видео: без длинных тире, как и тексты кворков."""
    found = []
    for path in sorted(BOT_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = docstrings(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                    and ("—" in node.value or "–" in node.value)):
                found.append(f"{path.relative_to(BOT_DIR)}:{node.lineno}: {node.value[:60]!r}")
    assert not found, "\n".join(found)


def test_reminder_names_the_day():
    settings = Settings(bot_token="1:T", owner_ids=frozenset())
    b = sample_booking()  # 24.09 10:00
    same_day = int(datetime(2026, 9, 24, 9, 0, tzinfo=MSK).timestamp())
    day_before = int(datetime(2026, 9, 23, 12, 0, tzinfo=MSK).timestamp())
    assert "Напоминание: сегодня 10:00" in reminders.reminder_text(b, settings, now=same_day)
    assert "Напоминание: завтра 10:00" in reminders.reminder_text(b, settings, now=day_before)

