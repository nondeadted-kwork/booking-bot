"""Окна с учётом барберов: отпуск и выходные в днях, ближайшее время, порядок для «любого»."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from bot import availability
from bot.config import SERVICES_BY_CODE, BarberSeed, Schedule, Settings
from bot.db import Database
from bot.slots import ANY

MSK = ZoneInfo("Europe/Moscow")
WEDNESDAY = date(2026, 9, 23)
NOW = datetime.combine(WEDNESDAY, time(8), tzinfo=MSK)
SETTINGS = Settings(bot_token="1:T", owner_ids=frozenset(),
                    schedule=Schedule(tz=MSK, work_start=time(10), work_end=time(14)))
CUT = SERVICES_BY_CODE["cut"]


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "av.db"))
    await database.connect()
    await database.ensure_barbers((BarberSeed("Артём", "", frozenset({0, 1, 2, 3, 4})),
                                   BarberSeed("Максим", "", frozenset({2, 3, 4, 5, 6}))))
    await database.upsert_user(1, "Иван", None)
    yield database
    await database.close()


def at(hour: int, d: date = WEDNESDAY) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=MSK)


async def book(db: Database, barber_id: int, hour: int) -> None:
    start = int(at(hour).timestamp())
    await db.create_booking(user_id=1, barber_id=barber_id, service_code="cut", start_at=start, end_at=start + 3600,
                            price=1500, now=int(NOW.timestamp()))


async def test_day_options_show_vacation_and_closed_days(db):
    await db.toggle_day_off(1, "2026-09-24")   # чт: Артём в отпуске
    await db.toggle_closed_day("2026-09-25")   # пт: салон закрыт
    options = {d: (n, s) for d, n, s in await availability.day_options(db, SETTINGS, CUT, 1, NOW)}
    assert options[WEDNESDAY] == (7, "open")
    assert options[date(2026, 9, 24)] == (0, "off")
    assert options[date(2026, 9, 25)] == (0, "closed")
    assert date(2026, 9, 26) not in options    # сб не рабочий день Артёма
    any_days = {d: s for d, _, s in await availability.day_options(db, SETTINGS, CUT, ANY, NOW)}
    assert any_days[date(2026, 9, 24)] == "open"  # Максим работает


async def test_nearest_time_per_barber_and_for_any(db):
    for hour in (10, 11, 12, 13):
        await book(db, 1, hour)                  # Артём занят всю среду
    barbers, nearest = await availability.nearest(db, SETTINGS, CUT, NOW)
    assert [b.name for b in barbers] == ["Артём", "Максим"]
    assert nearest[1] == at(10, date(2026, 9, 24))
    assert nearest[2] == at(10)
    assert nearest[ANY] == at(10)


async def test_any_prefers_the_less_loaded_barber(db):
    await book(db, 1, 10)
    free = await availability.free_times(db, SETTINGS, CUT, ANY, WEDNESDAY, NOW)
    assert free[at(12)] == [2, 1]  # у Артёма уже есть запись в этот день
    assert free[at(10)] == [2]


async def test_hidden_barber_has_no_times(db):
    await db.update_barber(1, active=False)
    assert await availability.free_times(db, SETTINGS, CUT, 1, WEDNESDAY, NOW) == {}
