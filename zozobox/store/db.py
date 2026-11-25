"""SQLite plumbing for the store: one connection, one lock, WAL, migrations.

The box writes from several threads (engine, nightly job, sync client,
dashboard), so the single connection is opened with check_same_thread=False
and every access goes through `Database.lock` (an RLock, so repo methods can
nest). Writes use `tx()`, which wraps BEGIN IMMEDIATE ... COMMIT.

Schema versions live in PRAGMA user_version. `MIGRATIONS[i]` moves the schema
from version i to i+1. Only ever append to it: never edit a released step.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

UTC = timezone.utc
_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


class StoreError(Exception):
    """Raised for store-level problems (unknown ids, newer schema, bad media paths)."""


def to_utc(dt: datetime) -> datetime:
    """Aware UTC datetime. Naive datetimes are taken to be UTC already."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ts_str(dt: datetime) -> str:
    """Fixed-width UTC text form: sorts lexically in time order."""
    return to_utc(dt).strftime(_TS_FMT)


def parse_ts(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.strptime(s, _TS_FMT).replace(tzinfo=UTC)


MIGRATIONS: list[list[str]] = [
    # v0 -> v1: initial schema.
    [
        """CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE content (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            for_date TEXT,
            media TEXT NOT NULL DEFAULT '{}',
            meta TEXT NOT NULL DEFAULT '{}',
            safety TEXT NOT NULL DEFAULT 'pending',
            safety_note TEXT,
            approval TEXT NOT NULL DEFAULT 'pending',
            approval_at TEXT,
            first_served_at TEXT,
            served_day TEXT,
            completed_at TEXT,
            withheld_at TEXT,
            superseded_at TEXT,
            superseded_by TEXT
        )""",
        "CREATE INDEX content_kind ON content(kind, created_at)",
        """CREATE TABLE daily (
            day TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )""",
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            day TEXT NOT NULL,
            type TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            media TEXT
        )""",
        "CREATE INDEX events_type_ts ON events(type, ts)",
        "CREATE INDEX events_day ON events(day)",
        "CREATE INDEX events_ts ON events(ts)",
        """CREATE TABLE outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            remote_key TEXT NOT NULL,
            local_path TEXT,
            record TEXT,
            content_type TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            claimed_at TEXT,
            done_at TEXT,
            last_error TEXT
        )""",
        "CREATE INDEX outbox_due ON outbox(status, next_attempt_at)",
        "CREATE INDEX outbox_key ON outbox(remote_key, status)",
        """CREATE TABLE recordings (
            id TEXT PRIMARY KEY,
            slot TEXT NOT NULL,
            media TEXT NOT NULL,
            created_at TEXT NOT NULL,
            duration_s REAL,
            meta TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'live',
            superseded_at TEXT,
            superseded_by TEXT,
            withheld_at TEXT,
            heard_at TEXT,
            published_at TEXT
        )""",
        "CREATE INDEX recordings_slot ON recordings(slot, created_at)",
    ],
]

SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    def __init__(self, path: Path, migrations: list[list[str]] | None = None):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._migrations = MIGRATIONS if migrations is None else migrations
        # Depth of tx() blocks this object has open. Only the thread holding
        # `lock` touches it, so a plain int is enough.
        self._tx_depth = 0
        self.conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=5.0
        )
        try:
            self.conn.row_factory = sqlite3.Row
            with self.lock:
                mode = self._set_wal()
                if str(mode).lower() != "wal":
                    log.warning("store: journal_mode is %s, not WAL", mode)
                self.conn.execute("PRAGMA synchronous=NORMAL")
                self.conn.execute("PRAGMA foreign_keys=ON")
                self.conn.execute("PRAGMA busy_timeout=5000")
                self.migrate()
        except BaseException:
            self.close()
            raise

    def _set_wal(self, timeout_s: float = 5.0) -> str:
        """Switch to WAL. On a brand-new file another process doing the same at
        the same moment makes SQLite return BUSY at once (the busy handler is
        not used for a journal-mode change), so retry for a while."""
        deadline = time.monotonic() + timeout_s
        delay = 0.005
        while True:
            try:
                return self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e).lower():
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)

    @property
    def version(self) -> int:
        with self.lock:
            return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        """Bring the schema up to date. Safe against another process opening
        the same file at the same time: each step re-reads user_version after
        taking the write lock (BEGIN IMMEDIATE), so a step another process
        already applied is never run twice."""
        target = len(self._migrations)
        with self.lock:
            current = self.version
            if current > target:
                raise StoreError(
                    f"database {self.path} is schema v{current}, this code knows v{target}"
                )
            while current < target:
                with self.tx() as c:
                    current = c.execute("PRAGMA user_version").fetchone()[0]
                    if current > target:
                        raise StoreError(
                            f"database {self.path} is schema v{current}, this code knows v{target}"
                        )
                    if current == target:
                        break  # another process finished the job
                    for stmt in self._migrations[current]:
                        c.execute(stmt)
                    # PRAGMA can't take parameters; the value is our own int.
                    c.execute(f"PRAGMA user_version={current + 1}")
                log.info("store: migrated schema v%d -> v%d", current, current + 1)
                current += 1

    def _rollback_quietly(self) -> None:
        """ROLLBACK if a transaction is still open. SQLite may already have
        rolled back by itself (SQLITE_FULL, IOERR, NOMEM); never let a failing
        ROLLBACK hide the error that caused it."""
        try:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            log.exception("store: rollback failed")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. Nested calls join the outer transaction.

        If COMMIT fails the transaction is rolled back and the error raised,
        so the connection never stays stuck inside a dead transaction."""
        with self.lock:
            if self._tx_depth > 0:
                self._tx_depth += 1
                try:
                    yield self.conn
                finally:
                    self._tx_depth -= 1
                return
            if self.conn.in_transaction:
                # Not ours: left over from a failure we could not clean up.
                log.warning("store: rolling back a stray open transaction")
                self._rollback_quietly()
            self.conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                try:
                    yield self.conn
                except BaseException:
                    self._rollback_quietly()
                    raise
                try:
                    self.conn.execute("COMMIT")
                except BaseException:
                    self._rollback_quietly()
                    raise
            finally:
                self._tx_depth = 0

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.close()
            except sqlite3.Error:  # pragma: no cover
                log.exception("store: close failed")
