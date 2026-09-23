from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bot.config import Schedule
from bot.slots import Staff, calendar_days, free_by_start, free_slots, staff_slots

MSK = ZoneInfo("Europe/Moscow")
SCH = Schedule(tz=MSK, work_start=time(10), work_end=time(14), slot_step_min=30, min_lead_min=60,
               workdays=frozenset({0, 1, 2, 3, 4, 5}))  # пн-сб
WEDNESDAY = date(2026, 9, 23)
SUNDAY = date(2026, 9, 27)


def at(h: int, m: int = 0, d: date = WEDNESDAY) -> datetime:
    return datetime.combine(d, time(h, m), tzinfo=MSK)


def ts(h: int, m: int = 0) -> int:
    return int(at(h, m).timestamp())


def hhmm(slots):
    return [f"{s:%H:%M}" for s in slots]


def test_full_day_without_bookings():
    slots = free_slots(WEDNESDAY, 60, [], now=at(0), schedule=SCH)
    # 10:00…13:00 — последняя часовая услуга должна закончиться к 14:00
    assert hhmm(slots) == ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00"]


def test_busy_interval_blocks_overlapping_starts():
    busy = [(ts(11), ts(12))]
    slots = free_slots(WEDNESDAY, 60, busy, now=at(0), schedule=SCH)
    # 10:30 заняло бы 10:30–11:30 и пересеклось бы с 11:00–12:00
    assert hhmm(slots) == ["10:00", "12:00", "12:30", "13:00"]


def test_short_service_fits_into_gap():
    busy = [(ts(10), ts(10, 30)), (ts(11), ts(14))]
    assert hhmm(free_slots(WEDNESDAY, 30, busy, now=at(0), schedule=SCH)) == ["10:30"]
    assert free_slots(WEDNESDAY, 60, busy, now=at(0), schedule=SCH) == []


def test_min_lead_time_hides_near_slots():
    slots = free_slots(WEDNESDAY, 30, [], now=at(11, 10), schedule=SCH)
    assert hhmm(slots)[0] == "12:30"  # 11:10 + 60 минут → ближайшее окно 12:30


def test_day_off_and_closed_day():
    assert free_slots(SUNDAY, 30, [], now=at(0), schedule=SCH) == []
    assert free_slots(WEDNESDAY, 30, [], now=at(0), schedule=SCH, closed={WEDNESDAY}) == []


def test_past_day_has_no_slots():
    assert free_slots(WEDNESDAY - timedelta(days=1), 30, [], now=at(9), schedule=SCH) == []


def test_calendar_days_is_a_window_of_calendar_days():
    friday = date(2026, 9, 25)
    days = calendar_days(at(9, d=friday), SCH)
    assert days[0] == friday and SUNDAY not in days
    assert len(days) == 6  # 7 календарных дней минус воскресенье
    barber_days = calendar_days(at(9, d=friday), SCH, workdays={0, 1, 2, 3, 4})
    assert [d.weekday() for d in barber_days] == [4, 0, 1, 2, 3]


def test_staff_slots_respect_barber_days_and_vacation():
    artem = Staff(1, frozenset({0, 1, 2, 3, 4}), off=frozenset({WEDNESDAY}))
    assert staff_slots(WEDNESDAY, 60, artem, [], now=at(0), schedule=SCH) == []  # отпуск
    assert staff_slots(date(2026, 9, 26), 60, artem, [], now=at(0), schedule=SCH) == []  # суббота не его день
    maxim = Staff(2, frozenset({2, 5}))
    assert hhmm(staff_slots(WEDNESDAY, 60, maxim, [], now=at(0), schedule=SCH))[0] == "10:00"


def test_free_by_start_merges_barbers_in_list_order():
    a, b = Staff(1, frozenset(range(7))), Staff(2, frozenset(range(7)))
    busy = {1: [(ts(10), ts(12))], 2: [(ts(12), ts(14))]}
    free = free_by_start(WEDNESDAY, 60, [a, b], busy, now=at(0), schedule=SCH)
    assert hhmm(free) == ["10:00", "10:30", "11:00", "12:00", "12:30", "13:00"]
    assert free[at(10)] == [2] and free[at(12)] == [1]
    both = free_by_start(WEDNESDAY, 30, [a, b], {}, now=at(0), schedule=SCH)
    assert both[at(10)] == [1, 2]
