"""SQLite-хранилище: наблюдения цен, отправленные находки, настройки, кеш станций."""

from __future__ import annotations

import json
import sqlite3
import statistics
import threading
from datetime import date, datetime, timedelta
from typing import Any

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,           -- flight | train
    origin        TEXT NOT NULL,
    dest          TEXT NOT NULL,
    depart_date   TEXT,
    return_date   TEXT,
    price         REAL NOT NULL,
    klass         TEXT,                    -- для поездов: Плац/Купе/СВ/Сид
    observed_at   TEXT NOT NULL,
    meta          TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_route  ON observations(kind, origin, dest, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_seen   ON observations(observed_at);

CREATE TABLE IF NOT EXISTS deals (
    fingerprint  TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    origin       TEXT,
    dest         TEXT,
    price        REAL,
    discount     REAL,
    sent_at      TEXT NOT NULL,
    payload      TEXT
);
CREATE INDEX IF NOT EXISTS idx_deals_sent ON deals(sent_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS stations (
    name   TEXT PRIMARY KEY,
    code   TEXT NOT NULL,
    title  TEXT
);

CREATE TABLE IF NOT EXISTS scan_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    kind       TEXT,
    checked    INTEGER,
    found      INTEGER,
    error      TEXT
);
"""


class Storage:
    def __init__(self, path=DB_PATH) -> None:
        self.path = str(path)
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    # --- наблюдения --------------------------------------------------------
    def record(
        self,
        kind: str,
        origin: str,
        dest: str,
        price: float,
        depart_date: date | None = None,
        return_date: date | None = None,
        klass: str | None = None,
        meta: dict | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO observations (kind, origin, dest, depart_date, return_date, price,"
                " klass, observed_at, meta) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    kind, origin, dest,
                    depart_date.isoformat() if depart_date else None,
                    return_date.isoformat() if return_date else None,
                    float(price), klass, datetime.now().isoformat(timespec="seconds"),
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )

    def record_many(self, rows: list[tuple]) -> None:
        """rows: (kind, origin, dest, depart, ret, price, klass, meta_dict)"""
        now = datetime.now().isoformat(timespec="seconds")
        payload = [
            (
                r[0], r[1], r[2],
                r[3].isoformat() if isinstance(r[3], date) else r[3],
                r[4].isoformat() if isinstance(r[4], date) else r[4],
                float(r[5]), r[6], now,
                json.dumps(r[7] or {}, ensure_ascii=False),
            )
            for r in rows
        ]
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO observations (kind, origin, dest, depart_date, return_date, price,"
                " klass, observed_at, meta) VALUES (?,?,?,?,?,?,?,?,?)",
                payload,
            )

    def price_history(
        self, kind: str, origin: str, dest: str, days: int = 90, klass: str | None = None
    ) -> list[float]:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        sql = ("SELECT price FROM observations WHERE kind=? AND origin=? AND dest=?"
               " AND observed_at>=?")
        args: list[Any] = [kind, origin, dest, since]
        if klass:
            sql += " AND klass=?"
            args.append(klass)
        with self._conn() as conn:
            return [row["price"] for row in conn.execute(sql, args)]

    def observation_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]

    def prune(self, keep_days: int = 180) -> int:
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM observations WHERE observed_at < ?", (cutoff,))
            return cur.rowcount

    # --- находки -----------------------------------------------------------
    def already_sent(self, fingerprint: str, within_hours: int = 72) -> bool:
        cutoff = (datetime.now() - timedelta(hours=within_hours)).isoformat(timespec="seconds")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM deals WHERE fingerprint=? AND sent_at>=?", (fingerprint, cutoff)
            ).fetchone()
        return row is not None

    def mark_sent(self, fingerprint: str, kind: str, origin: str, dest: str,
                  price: float, discount: float, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO deals (fingerprint, kind, origin, dest, price, discount,"
                " sent_at, payload) VALUES (?,?,?,?,?,?,?,?)",
                (fingerprint, kind, origin, dest, float(price), float(discount),
                 datetime.now().isoformat(timespec="seconds"),
                 json.dumps(payload, ensure_ascii=False, default=str)),
            )

    def sent_today(self) -> int:
        start = datetime.now().replace(hour=0, minute=0, second=0).isoformat(timespec="seconds")
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM deals WHERE sent_at>=?", (start,)
            ).fetchone()["c"]

    def clear_today(self) -> int:
        """Сбрасывает счётчик отправленного за сегодня — чтобы дневной лимит
        не блокировал находки после того, как его выел мусор."""
        start = datetime.now().replace(hour=0, minute=0, second=0).isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM deals WHERE sent_at>=?", (start,))
            return cur.rowcount

    def recent_deals(self, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM deals ORDER BY sent_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- настройки ---------------------------------------------------------
    def setting(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def set_setting(self, key: str, value) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # --- станции РЖД -------------------------------------------------------
    def station_code(self, name: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT code FROM stations WHERE name=?", (name.strip().lower(),)
            ).fetchone()
        return row["code"] if row else None

    def save_station(self, name: str, code: str, title: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO stations (name, code, title) VALUES (?,?,?)",
                (name.strip().lower(), str(code), title),
            )

    # --- журнал сканов -----------------------------------------------------
    def log_scan(self, kind: str, started: datetime, checked: int, found: int,
                 error: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO scan_log (started_at, finished_at, kind, checked, found, error)"
                " VALUES (?,?,?,?,?,?)",
                (started.isoformat(timespec="seconds"),
                 datetime.now().isoformat(timespec="seconds"), kind, checked, found, error),
            )

    def last_scans(self, limit: int = 5) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scan_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def percentile(values: list[float], q: float) -> float | None:
    """q в долях: 0.25 = нижний квартиль."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac
