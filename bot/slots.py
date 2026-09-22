"""Расчёт свободных окон. Чистые функции без Telegram и БД — поэтому легко тестируются."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

from .config import Schedule

Interval = tuple[int, int]  # (start_ts, end_ts), unix-время UTC


def overlaps(start: int, end: int, busy: Iterable[Interval]) -> bool:
    return any(b_start < end and b_end > start for b_start, b_end in busy)


def calendar_days(now: datetime, schedule: Schedule) -> list[date]:
    """Ближайшие рабочие дни (по графику), начиная с сегодняшнего."""
    today = now.astimezone(schedule.tz).date()
    days = (today + timedelta(days=i) for i in range(schedule.days_ahead + 7))
    return [d for d in days if d.weekday() in schedule.workdays][: schedule.days_ahead]


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
    closed: set[date] | frozenset[date] = frozenset(),
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
