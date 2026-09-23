"""Свободное время с учётом барберов. Читает базу и считает окна чистыми функциями из slots.py."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .config import Service, Settings
from .db import Barber, Database
from .slots import ANY, Interval, Staff, calendar_days, day_range, free_by_start


@dataclass(frozen=True)
class Snapshot:
    """Всё, что нужно для расчёта окон, прочитанное из базы за один раз."""
    barbers: list[Barber]            # активные, в порядке списка
    staff: dict[int, Staff]
    busy: dict[int, list[Interval]]
    closed: frozenset[date]

    def pick(self, m: int) -> list[Staff]:
        """Кого считать: одного выбранного барбера или всех для «любого»."""
        if m == ANY:
            return [self.staff[b.id] for b in self.barbers]
        return [self.staff[m]] if m in self.staff else []


async def snapshot(db: Database, settings: Settings, now: datetime, days: list[date]) -> Snapshot:
    barbers = await db.barbers()
    off = await db.days_off()
    closed = frozenset(date.fromisoformat(d) for d in await db.closed_days())
    busy: dict[int, list[Interval]] = {}
    if days:
        start, _ = day_range(min(days), settings.schedule)
        _, end = day_range(max(days), settings.schedule)
        busy = await db.busy_by_barber(start, end, int(now.timestamp()))
    staff = {b.id: Staff(b.id, b.workdays, off.get(b.id, frozenset())) for b in barbers}
    return Snapshot(barbers, staff, busy, closed)


async def free_times(db: Database, settings: Settings, service: Service, m: int, day: date,
                     now: datetime) -> dict[datetime, list[int]]:
    """Окна на день: время начала → свободные в это время барберы, менее загруженные первыми."""
    snap = await snapshot(db, settings, now, [day])
    staff = snap.pick(m)
    free = free_by_start(day, service.minutes, staff, snap.busy, now, settings.schedule, snap.closed)
    load = {s.id: len(snap.busy.get(s.id, [])) for s in staff}
    order = {s.id: i for i, s in enumerate(staff)}
    return {t: sorted(ids, key=lambda i: (load[i], order[i])) for t, ids in free.items()}


async def day_options(db: Database, settings: Settings, service: Service, m: int,
                      now: datetime) -> list[tuple[date, int, str]]:
    """Дни для клавиатуры: (день, сколько окон, статус open | closed | off)."""
    snap = await snapshot(db, settings, now, calendar_days(now, settings.schedule))
    staff = snap.pick(m)
    one = staff[0] if m != ANY and staff else None
    options = []
    for d in calendar_days(now, settings.schedule, one.workdays if one else None):
        if d in snap.closed:
            options.append((d, 0, "closed"))
        elif one is not None and d in one.off:
            options.append((d, 0, "off"))
        else:
            free = free_by_start(d, service.minutes, staff, snap.busy, now, settings.schedule, snap.closed)
            options.append((d, len(free), "open"))
    return options


async def nearest(db: Database, settings: Settings, service: Service,
                  now: datetime) -> tuple[list[Barber], dict[int, datetime | None]]:
    """Активные барберы и ближайшее свободное время каждого. Ключ ANY: ближайшее среди всех."""
    days = calendar_days(now, settings.schedule)
    snap = await snapshot(db, settings, now, days)
    result: dict[int, datetime | None] = {}
    for b in snap.barbers:
        result[b.id] = None
        for d in days:
            free = free_by_start(d, service.minutes, snap.pick(b.id), snap.busy, now, settings.schedule, snap.closed)
            if free:
                result[b.id] = next(iter(free))
                break
    found = [t for t in result.values() if t is not None]
    result[ANY] = min(found) if found else None
    return snap.barbers, result
