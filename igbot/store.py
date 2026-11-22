"""SQLite-backed state.

Everything the bot needs to remember across restarts lives here: which inbound
messages it has already handled, when it last replied to each conversation, and
the reply counters that back the rate caps.

The database is intentionally boring — one table of events plus a key/value
table — so it is easy to inspect with ``sqlite3`` when something looks wrong.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    thread_id   TEXT,
    fingerprint TEXT,
    detail      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_fingerprint
    ON events(fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_events_kind_ts ON events(kind, ts);
CREATE INDEX IF NOT EXISTS ix_events_thread ON events(thread_id, ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def fingerprint_for(thread_id: str, sender: str, text: str) -> str:
    """Stable id for an inbound message.

    Includes the thread and sender so the same words in two different
    conversations are treated as two different messages, and collapses repeated
    copies of the identical message into one.
    """
    norm = " ".join((text or "").split()).lower()
    raw = f"{thread_id or ''}|{sender or ''}|{norm}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


class Store:
    """Thread-safe wrapper around the SQLite state file."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ events

    def has_fingerprint(self, fingerprint: str) -> bool:
        """True if this exact message has already been recorded."""
        if not fingerprint:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE fingerprint = ? LIMIT 1", (fingerprint,)
            ).fetchone()
        return row is not None

    def record(
        self,
        kind: str,
        thread_id: str | None = None,
        fingerprint: str | None = None,
        detail: str | None = None,
        ts: float | None = None,
    ) -> bool:
        """Insert an event. Returns False if the fingerprint was already present.

        The uniqueness check and the insert happen in one transaction so two
        concurrent pollers cannot both claim the same message.
        """
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO events (ts, kind, thread_id, fingerprint, detail) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ts if ts is not None else time.time(), kind, thread_id, fingerprint, detail),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False

    def count(self, kind: str, since: float, thread_id: str | None = None) -> int:
        """Count events of a kind since a timestamp."""
        with self._lock:
            if thread_id is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM events WHERE kind = ? AND ts >= ?", (kind, since)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM events WHERE kind = ? AND ts >= ? AND thread_id = ?",
                    (kind, since, thread_id),
                ).fetchone()
        return int(row["c"]) if row else 0

    def last_ts(self, kind: str, thread_id: str | None = None) -> float | None:
        """Timestamp of the most recent matching event."""
        with self._lock:
            if thread_id is None:
                row = self._conn.execute(
                    "SELECT MAX(ts) AS t FROM events WHERE kind = ?", (kind,)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT MAX(ts) AS t FROM events WHERE kind = ? AND thread_id = ?",
                    (kind, thread_id),
                ).fetchone()
        return row["t"] if row and row["t"] is not None else None

    def recent(self, kind: str, limit: int = 50) -> list[sqlite3.Row]:
        """Most recent events of a kind, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE kind = ? ORDER BY ts DESC LIMIT ?", (kind, limit)
            ).fetchall()
        return list(rows)

    def prune(self, older_than_days: int = 30) -> int:
        """Delete old events. Returns the number of rows removed."""
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount or 0

    # ---------------------------------------------------------------------- kv

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def day_start(now: float | None = None) -> float:
    """Midnight (local time) of the current day, as a unix timestamp."""
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def hour_start(now: float | None = None) -> float:
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, -1))


def iter_recent(rows: Iterable[sqlite3.Row], key: str = "ts") -> Iterable[float]:
    for row in rows:
        yield float(row[key])
