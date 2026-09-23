"""SQLite-хранилище. Одна база-файл, никаких внешних сервисов."""
from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import aiosqlite

from .config import BarberSeed
from .slots import Interval

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,          -- telegram user id; у демо-клиентов отрицательный
    first_name  TEXT    NOT NULL,
    username    TEXT,
    phone       TEXT,
    created_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    is_blocked  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS barbers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    about       TEXT    NOT NULL DEFAULT '',
    workdays    TEXT    NOT NULL,             -- «0,1,2,3,4»: 0 = пн … 6 = вс
    active      INTEGER NOT NULL DEFAULT 1,   -- 0 = скрыт из записи
    sort        INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

CREATE TABLE IF NOT EXISTS barber_days_off (
    barber_id   INTEGER NOT NULL REFERENCES barbers(id),
    day         TEXT    NOT NULL,             -- YYYY-MM-DD, локальная дата
    PRIMARY KEY (barber_id, day)
);

CREATE TABLE IF NOT EXISTS bookings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    barber_id        INTEGER REFERENCES barbers(id),
    service_code     TEXT    NOT NULL,
    start_at         INTEGER NOT NULL,        -- unix UTC
    end_at           INTEGER NOT NULL,
    price            INTEGER NOT NULL,        -- ₽
    status           TEXT    NOT NULL,        -- pending_payment | confirmed | cancelled | expired
    paid             INTEGER NOT NULL DEFAULT 0,  -- копейки, оплачено онлайн
    charge_id        TEXT,
    hold_until       INTEGER,                 -- до какого времени держим слот без оплаты
    reminded_at      INTEGER,
    client_confirmed INTEGER NOT NULL DEFAULT 0,
    cancelled_by     TEXT,                    -- client | owner | conflict
    created_at       INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE INDEX IF NOT EXISTS idx_bookings_start ON bookings(start_at);
CREATE INDEX IF NOT EXISTS idx_bookings_user  ON bookings(user_id, start_at);

CREATE TABLE IF NOT EXISTS closed_days (
    day TEXT PRIMARY KEY                      -- YYYY-MM-DD, локальная дата
);

CREATE TABLE IF NOT EXISTS leads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    text        TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_user ON leads(user_id, created_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Слот занят, если запись подтверждена или ещё ждёт оплату и бронь не истекла.
ACTIVE = "(b.status = 'confirmed' OR (b.status = 'pending_payment' AND b.hold_until > :now))"

BOOKING_COLUMNS = """
    b.id, b.user_id, b.barber_id, b.service_code, b.start_at, b.end_at, b.price, b.status, b.paid,
    b.hold_until, b.reminded_at, b.client_confirmed, b.cancelled_by, b.created_at,
    u.first_name, u.username, u.phone, br.name AS barber_name
"""
BOOKING_FROM = "FROM bookings b JOIN users u ON u.id = b.user_id LEFT JOIN barbers br ON br.id = b.barber_id"


def parse_workdays(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.split(",") if x.strip().isdigit() and int(x) <= 6)


def format_workdays(days: Iterable[int]) -> str:
    return ",".join(str(d) for d in sorted(set(days)))


class SlotTaken(Exception):
    """Кто-то успел занять это время раньше."""


@dataclass
class Booking:
    id: int
    user_id: int
    barber_id: int | None
    service_code: str
    start_at: int
    end_at: int
    price: int
    status: str
    paid: int
    hold_until: int | None
    reminded_at: int | None
    client_confirmed: bool
    cancelled_by: str | None
    created_at: int
    first_name: str
    username: str | None
    phone: str | None
    barber_name: str | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Booking":
        data = dict(row)
        data["client_confirmed"] = bool(data["client_confirmed"])
        return cls(**data)


@dataclass
class User:
    id: int
    first_name: str
    username: str | None
    phone: str | None


@dataclass
class Barber:
    id: int
    name: str
    about: str
    workdays: frozenset[int]
    active: bool
    sort: int

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Barber":
        return cls(id=row["id"], name=row["name"], about=row["about"], workdays=parse_workdays(row["workdays"]),
                   active=bool(row["active"]), sort=row["sort"])


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # isolation_level=None — транзакции открываем сами (BEGIN IMMEDIATE), см. create_booking
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._migrate()

    async def _migrate(self) -> None:
        """Базы старого формата (до барберов) обновляются на месте, данные не теряются."""
        columns = {r["name"] for r in await self._all("PRAGMA table_info(bookings)")}
        if "barber_id" not in columns:
            await self.conn.execute("ALTER TABLE bookings ADD COLUMN barber_id INTEGER REFERENCES barbers(id)")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_barber ON bookings(barber_id, start_at)")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def _all(self, sql: str, params: Any = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _one(self, sql: str, params: Any = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    # --- users ---------------------------------------------------------------

    async def upsert_user(self, user_id: int, first_name: str, username: str | None) -> None:
        await self.conn.execute(
            """INSERT INTO users (id, first_name, username) VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET first_name = excluded.first_name,
                                             username = excluded.username,
                                             is_blocked = 0""",
            (user_id, first_name or "Без имени", username),
        )

    async def get_user(self, user_id: int) -> User | None:
        row = await self._one("SELECT id, first_name, username, phone FROM users WHERE id = ?", (user_id,))
        return User(**dict(row)) if row else None

    async def set_phone(self, user_id: int, phone: str) -> None:
        await self.conn.execute("UPDATE users SET phone = ? WHERE id = ?", (phone, user_id))

    async def mark_blocked(self, user_id: int) -> None:
        await self.conn.execute("UPDATE users SET is_blocked = 1 WHERE id = ?", (user_id,))

    async def active_user_ids(self) -> list[int]:
        return [r["id"] for r in await self._all("SELECT id FROM users WHERE is_blocked = 0")]

    # --- барберы -------------------------------------------------------------

    async def ensure_barbers(self, seeds: Sequence[BarberSeed]) -> None:
        """Первый запуск: создаёт барберов по умолчанию. Записи без барбера (старая база) отдаёт первому."""
        async with self._write_lock:
            row = await self._one("SELECT COUNT(*) AS n FROM barbers")
            if row["n"] == 0:
                for i, s in enumerate(seeds):
                    await self.conn.execute(
                        "INSERT INTO barbers (name, about, workdays, sort) VALUES (?, ?, ?, ?)",
                        (s.name, s.about, format_workdays(s.workdays), i),
                    )
            await self.conn.execute(
                "UPDATE bookings SET barber_id = (SELECT id FROM barbers ORDER BY sort, id LIMIT 1) "
                "WHERE barber_id IS NULL"
            )

    async def barbers(self, include_hidden: bool = False) -> list[Barber]:
        """Барберы в порядке списка. По умолчанию только те, кто принимает записи."""
        where = "" if include_hidden else "WHERE active = 1"
        rows = await self._all(f"SELECT * FROM barbers {where} ORDER BY active DESC, sort, id")
        return [Barber.from_row(r) for r in rows]

    async def get_barber(self, barber_id: int) -> Barber | None:
        row = await self._one("SELECT * FROM barbers WHERE id = ?", (barber_id,))
        return Barber.from_row(row) if row else None

    async def add_barber(self, name: str, about: str, workdays: Iterable[int]) -> Barber:
        async with self._write_lock:
            cur = await self.conn.execute(
                "INSERT INTO barbers (name, about, workdays, sort) "
                "VALUES (?, ?, ?, (SELECT COALESCE(MAX(sort), -1) + 1 FROM barbers))",
                (name, about, format_workdays(workdays)),
            )
            barber_id = cur.lastrowid
        barber = await self.get_barber(barber_id)
        assert barber is not None
        return barber

    async def update_barber(self, barber_id: int, *, name: str | None = None, about: str | None = None,
                            workdays: Iterable[int] | None = None, active: bool | None = None) -> None:
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name
        if about is not None:
            fields["about"] = about
        if workdays is not None:
            fields["workdays"] = format_workdays(workdays)
        if active is not None:
            fields["active"] = int(active)
        if fields:
            sets = ", ".join(f"{key} = :{key}" for key in fields)
            await self.conn.execute(f"UPDATE barbers SET {sets} WHERE id = :id", {**fields, "id": barber_id})

    async def days_off(self) -> dict[int, frozenset[date]]:
        """Отпуска всех барберов: id → даты."""
        result: dict[int, set[date]] = {}
        for r in await self._all("SELECT barber_id, day FROM barber_days_off"):
            result.setdefault(r["barber_id"], set()).add(date.fromisoformat(r["day"]))
        return {barber_id: frozenset(days) for barber_id, days in result.items()}

    async def toggle_day_off(self, barber_id: int, day: str) -> bool:
        """Переключает отпуск барбера на дату YYYY-MM-DD. Возвращает True, если теперь это отпуск."""
        async with self._write_lock:
            if await self._one("SELECT 1 FROM barber_days_off WHERE barber_id = ? AND day = ?", (barber_id, day)):
                await self.conn.execute("DELETE FROM barber_days_off WHERE barber_id = ? AND day = ?",
                                        (barber_id, day))
                return False
            await self.conn.execute("INSERT INTO barber_days_off (barber_id, day) VALUES (?, ?)", (barber_id, day))
            return True

    # --- bookings: чтение ----------------------------------------------------

    async def busy_intervals(self, start: int, end: int, now: int) -> list[tuple[int, int]]:
        rows = await self._all(
            f"SELECT b.start_at, b.end_at FROM bookings b "
            f"WHERE {ACTIVE} AND b.start_at < :end AND b.end_at > :start",
            {"now": now, "start": start, "end": end},
        )
        return [(r["start_at"], r["end_at"]) for r in rows]

    async def get_booking(self, booking_id: int) -> Booking | None:
        row = await self._one(f"SELECT {BOOKING_COLUMNS} {BOOKING_FROM} WHERE b.id = ?", (booking_id,))
        return Booking.from_row(row) if row else None

    async def user_upcoming(self, user_id: int, now: int) -> list[Booking]:
        rows = await self._all(
            f"SELECT {BOOKING_COLUMNS} {BOOKING_FROM} "
            f"WHERE b.user_id = :uid AND b.status = 'confirmed' AND b.end_at > :now ORDER BY b.start_at",
            {"uid": user_id, "now": now},
        )
        return [Booking.from_row(r) for r in rows]

    async def bookings_between(self, start: int, end: int, now: int) -> list[Booking]:
        rows = await self._all(
            f"SELECT {BOOKING_COLUMNS} {BOOKING_FROM} "
            f"WHERE {ACTIVE} AND b.start_at >= :start AND b.start_at < :end ORDER BY b.start_at",
            {"now": now, "start": start, "end": end},
        )
        return [Booking.from_row(r) for r in rows]

    async def all_bookings(self) -> list[Booking]:
        rows = await self._all(f"SELECT {BOOKING_COLUMNS} {BOOKING_FROM} ORDER BY b.start_at")
        return [Booking.from_row(r) for r in rows]

    async def due_reminders(self, now: int, window_sec: int) -> list[Booking]:
        rows = await self._all(
            f"SELECT {BOOKING_COLUMNS} {BOOKING_FROM} "
            f"WHERE b.status = 'confirmed' AND b.reminded_at IS NULL "
            f"  AND b.start_at > :now AND b.start_at <= :now + :window",
            {"now": now, "window": window_sec},
        )
        return [Booking.from_row(r) for r in rows]

    # --- bookings: запись ----------------------------------------------------

    async def create_booking(
        self,
        *,
        user_id: int,
        service_code: str,
        start_at: int,
        end_at: int,
        price: int,
        now: int,
        hold_until: int | None = None,
        reminded_at: int | None = None,
    ) -> Booking:
        """Атомарно проверяет пересечение и создаёт запись.

        asyncio.Lock защищает от гонки внутри процесса, BEGIN IMMEDIATE — от второго процесса,
        если его кто-то случайно запустит рядом.
        """
        status = "pending_payment" if hold_until else "confirmed"
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                clash = await self._one(
                    f"SELECT 1 FROM bookings b WHERE {ACTIVE} AND b.start_at < :end AND b.end_at > :start LIMIT 1",
                    {"now": now, "start": start_at, "end": end_at},
                )
                if clash:
                    raise SlotTaken
                cur = await self.conn.execute(
                    """INSERT INTO bookings (user_id, service_code, start_at, end_at, price, status,
                                             hold_until, reminded_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, service_code, start_at, end_at, price, status, hold_until, reminded_at, now),
                )
                booking_id = cur.lastrowid
                await self.conn.execute("COMMIT")
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
        booking = await self.get_booking(booking_id)
        assert booking is not None
        return booking

    async def cancel_booking(self, booking_id: int, by: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE bookings SET status = 'cancelled', cancelled_by = ? "
            "WHERE id = ? AND status IN ('confirmed', 'pending_payment')",
            (by, booking_id),
        )
        return cur.rowcount > 0

    async def extend_hold(self, booking_id: int, until: int) -> None:
        await self.conn.execute(
            "UPDATE bookings SET hold_until = MAX(COALESCE(hold_until, 0), ?) WHERE id = ?", (until, booking_id)
        )

    async def confirm_payment(self, booking_id: int, charge_id: str, amount: int, now: int) -> str:
        """Возвращает 'ok', 'already', 'conflict' (время успели занять) или 'missing'."""
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                b = await self._one("SELECT id, status, start_at, end_at FROM bookings WHERE id = ?", (booking_id,))
                if b is None:
                    result = "missing"
                elif b["status"] == "confirmed":
                    result = "already"
                else:
                    clash = await self._one(
                        f"SELECT 1 FROM bookings b WHERE {ACTIVE} AND b.id != :id "
                        f"AND b.start_at < :end AND b.end_at > :start LIMIT 1",
                        {"now": now, "id": booking_id, "start": b["start_at"], "end": b["end_at"]},
                    )
                    result = "conflict" if clash else "ok"
                    await self.conn.execute(
                        "UPDATE bookings SET status = ?, cancelled_by = ?, paid = ?, charge_id = ?, hold_until = NULL "
                        "WHERE id = ?",
                        (
                            "cancelled" if clash else "confirmed",
                            "conflict" if clash else None,
                            amount,
                            charge_id,
                            booking_id,
                        ),
                    )
                await self.conn.execute("COMMIT")
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
        return result

    async def expire_holds(self, now: int) -> list[Booking]:
        async with self._write_lock:
            rows = await self._all(
                f"SELECT {BOOKING_COLUMNS} {BOOKING_FROM} "
                f"WHERE b.status = 'pending_payment' AND b.hold_until <= ?",
                (now,),
            )
            expired = [Booking.from_row(r) for r in rows]
            if expired:
                ids = ",".join(str(b.id) for b in expired)
                await self.conn.execute(
                    f"UPDATE bookings SET status = 'expired' WHERE id IN ({ids}) AND status = 'pending_payment'"
                )
        return expired

    async def mark_reminded(self, booking_id: int, now: int) -> None:
        await self.conn.execute("UPDATE bookings SET reminded_at = ? WHERE id = ?", (now, booking_id))

    async def set_client_confirmed(self, booking_id: int) -> None:
        await self.conn.execute("UPDATE bookings SET client_confirmed = 1 WHERE id = ?", (booking_id,))

    # --- выходные ------------------------------------------------------------

    async def closed_days(self) -> set[str]:
        return {r["day"] for r in await self._all("SELECT day FROM closed_days")}

    async def toggle_closed_day(self, day: str) -> bool:
        """Переключает день. Возвращает True, если день теперь закрыт."""
        async with self._write_lock:
            if await self._one("SELECT 1 FROM closed_days WHERE day = ?", (day,)):
                await self.conn.execute("DELETE FROM closed_days WHERE day = ?", (day,))
                return False
            await self.conn.execute("INSERT INTO closed_days (day) VALUES (?)", (day,))
            return True

    # --- статистика ----------------------------------------------------------

    async def stats(self, now: int, since: int) -> dict[str, Any]:
        users = await self._one("SELECT COUNT(*) AS n FROM users")
        upcoming = await self._one(
            "SELECT COUNT(*) AS n FROM bookings WHERE status = 'confirmed' AND start_at > ?", (now,)
        )
        period = await self._one(
            """SELECT
                 SUM(status = 'confirmed')                          AS visits,
                 COALESCE(SUM(CASE WHEN status = 'confirmed' THEN price END), 0) AS revenue,
                 COALESCE(SUM(CASE WHEN status = 'confirmed' THEN paid END), 0)  AS paid_online,
                 SUM(status = 'cancelled')                          AS cancelled
               FROM bookings WHERE created_at >= ?""",
            (since,),
        )
        top = await self._one(
            """SELECT service_code, COUNT(*) AS n FROM bookings
               WHERE status = 'confirmed' AND created_at >= ?
               GROUP BY service_code ORDER BY n DESC LIMIT 1""",
            (since,),
        )
        return {
            "users": users["n"],
            "upcoming": upcoming["n"],
            "visits": period["visits"] or 0,
            "revenue": period["revenue"],
            "paid_online": period["paid_online"] // 100,
            "cancelled": period["cancelled"] or 0,
            "top_service": top["service_code"] if top else None,
        }
