"""Расчёт свободных окон. Чистые функции без Telegram и БД, поэтому легко тестируются."""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import Schedule

Interval = tuple[int, int]  # (start_ts, end_ts), unix-время UTC
ANY = 0                     # «любой свободный барбер» в callback-данных и расчётах


@dataclass(frozen=True)
class Staff:
    """Барбер глазами расчёта окон: в какие дни недели работает и когда в отпуске."""
    id: int
    workdays: frozenset[int]
    off: frozenset[date] = frozenset()


def overlaps(start: int, end: int, busy: Iterable[Interval]) -> bool:
    return any(b_start < end and b_end > start for b_start, b_end in busy)


def calendar_days(now: datetime, schedule: Schedule, workdays: Collection[int] | None = None) -> list[date]:
    """Рабочие дни в ближайшие `days_ahead` календарных дней, начиная с сегодняшнего.

    workdays: дни недели барбера. Без них берутся дни салона.
    """
    today = now.astimezone(schedule.tz).date()
    days_on = schedule.workdays if workdays is None else schedule.workdays & frozenset(workdays)
    window = (today + timedelta(days=i) for i in range(schedule.days_ahead))
    return [d for d in window if d.weekday() in days_on]


def day_range(day: date, schedule: Schedule) -> Interval:
    start = datetime.combine(day, schedule.work_start, tzinfo=schedule.tz)
    end = datetime.combine(day, schedule.work_end, tzinfo=schedule.tz)
    return int(start.timestamp()), int(end.timestamp())


def free_slots(
    day: date,
    minutes: int,
    busy: Sequence[Interval],
    now: datetime,
    schedule: Schedule,
    closed: Collection[date] = frozenset(),
) -> list[datetime]:
    """Все времена начала, в которые услуга длиной `minutes` помещается целиком."""
    if day.weekday() not in schedule.workdays or day in closed:
        return []

    t = datetime.combine(day, schedule.work_start, tzinfo=schedule.tz)
    end = datetime.combine(day, schedule.work_end, tzinfo=schedule.tz)
    earliest = now + timedelta(minutes=schedule.min_lead_min)
    step = timedelta(minutes=schedule.slot_step_min)
    duration = timedelta(minutes=minutes)

    result = []
    while t + duration <= end:
        if t >= earliest:
            start_ts, end_ts = int(t.timestamp()), int((t + duration).timestamp())
            if not overlaps(start_ts, end_ts, busy):
                result.append(t)
        t += step
    return result


def staff_slots(day: date, minutes: int, staff: Staff, busy: Sequence[Interval], now: datetime,
                schedule: Schedule, closed: Collection[date] = frozenset()) -> list[datetime]:
    """Окна одного барбера: его рабочие дни, его отпуск, выходные салона и его занятое время."""
    if day.weekday() not in staff.workdays or day in staff.off:
        return []
    return free_slots(day, minutes, busy, now, schedule, closed)


def free_by_start(day: date, minutes: int, staff: Sequence[Staff], busy: Mapping[int, Sequence[Interval]],
                  now: datetime, schedule: Schedule,
                  closed: Collection[date] = frozenset()) -> dict[datetime, list[int]]:
    """Время начала → id барберов, свободных в это время, в порядке списка.

    Для одного барбера передаётся список из одного, для «любого свободного» все барберы.
    """
    result: dict[datetime, list[int]] = {}
    for s in staff:
        for t in staff_slots(day, minutes, s, busy.get(s.id, ()), now, schedule, closed):
            result.setdefault(t, []).append(s.id)
    return dict(sorted(result.items()))
