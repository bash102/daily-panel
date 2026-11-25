"""Upload queue to the archive (SPEC 7.3, 7.4).

Each entry is either a local file or a JSON record, plus the R2 key it goes
to. The sync client claims a batch, uploads, then marks each done or failed.
Failures back off exponentially (base * 2^(attempts-1), capped) and are
retried forever: the cloud is the primary copy, so nothing is dropped.

Claims are leases: an entry claimed by a process that died (power cut) is
returned to the queue when the sync client reopens the Store with
recover_outbox=True, or when the lease expires.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .db import Database, StoreError, parse_ts, ts_str

PENDING = "pending"
INFLIGHT = "inflight"
DONE = "done"


@dataclass(frozen=True)
class OutboxItem:
    id: int
    remote_key: str
    local_path: Path | None
    record: Any
    content_type: str | None
    created_at: datetime
    status: str
    attempts: int
    next_attempt_at: datetime
    claimed_at: datetime | None
    done_at: datetime | None
    last_error: str | None

    @property
    def is_file(self) -> bool:
        return self.local_path is not None

    @classmethod
    def from_row(cls, r) -> "OutboxItem":
        return cls(
            id=r["id"],
            remote_key=r["remote_key"],
            local_path=Path(r["local_path"]) if r["local_path"] is not None else None,
            record=json.loads(r["record"]) if r["record"] is not None else None,
            content_type=r["content_type"],
            created_at=parse_ts(r["created_at"]),
            status=r["status"],
            attempts=r["attempts"],
            next_attempt_at=parse_ts(r["next_attempt_at"]),
            claimed_at=parse_ts(r["claimed_at"]),
            done_at=parse_ts(r["done_at"]),
            last_error=r["last_error"],
        )


@dataclass(frozen=True)
class OutboxStats:
    pending: int
    inflight: int
    done: int
    failing: int  # pending entries that have failed at least once
    oldest_pending_at: datetime | None
    last_error: str | None


class Outbox:
    def __init__(
        self,
        db: Database,
        clock: Callable[[], datetime],
        *,
        backoff_base_s: float = 30.0,
        backoff_max_s: float = 3600.0,
        lease_s: float = 600.0,
    ):
        self._db = db
        self._clock = clock
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.lease_s = lease_s

    def enqueue(
        self,
        remote_key: str,
        *,
        path: str | Path | None = None,
        record: Any = None,
        content_type: str | None = None,
    ) -> OutboxItem:
        """Queue a file (path) or a JSON record for `remote_key`. A still-pending
        (unclaimed) entry for the same key is replaced, so only the latest
        version uploads."""
        if (path is None) == (record is None):
            raise StoreError("enqueue needs exactly one of path= or record=")
        if not remote_key or remote_key.startswith("/"):
            raise StoreError(f"bad remote key {remote_key!r}")
        local = str(Path(path).resolve()) if path is not None else None
        rec = json.dumps(record) if record is not None else None
        now = ts_str(self._clock())
        with self._db.tx() as c:
            r = c.execute(
                "SELECT id FROM outbox WHERE remote_key=? AND status=? ORDER BY id DESC LIMIT 1",
                (remote_key, PENDING),
            ).fetchone()
            if r is not None:
                c.execute(
                    "UPDATE outbox SET local_path=?, record=?, content_type=?, attempts=0, "
                    "next_attempt_at=?, last_error=NULL WHERE id=?",
                    (local, rec, content_type, now, r["id"]),
                )
                item_id = r["id"]
            else:
                item_id = c.execute(
                    "INSERT INTO outbox(remote_key, local_path, record, content_type, created_at, "
                    "status, next_attempt_at) VALUES (?,?,?,?,?,?,?)",
                    (remote_key, local, rec, content_type, now, PENDING, now),
                ).lastrowid
        return self.get(item_id)

    def get(self, item_id: int) -> OutboxItem:
        r = self._db.query_one("SELECT * FROM outbox WHERE id=?", (item_id,))
        if r is None:
            raise StoreError(f"no outbox item {item_id}")
        return OutboxItem.from_row(r)

    def claim(self, limit: int = 10) -> list[OutboxItem]:
        """Lease up to `limit` due entries, oldest first."""
        now = self._clock()
        stale = ts_str(now - timedelta(seconds=self.lease_s))
        with self._db.tx() as c:
            rows = c.execute(
                "SELECT id FROM outbox WHERE (status=? AND next_attempt_at<=?) "
                "OR (status=? AND claimed_at<=?) ORDER BY next_attempt_at, id LIMIT ?",
                (PENDING, ts_str(now), INFLIGHT, stale, limit),
            ).fetchall()
            ids = [r["id"] for r in rows]
            for i in ids:
                c.execute(
                    "UPDATE outbox SET status=?, claimed_at=? WHERE id=?", (INFLIGHT, ts_str(now), i)
                )
        return [self.get(i) for i in ids]

    def mark_done(self, item_id: int) -> OutboxItem:
        with self._db.tx() as c:
            cur = c.execute(
                "UPDATE outbox SET status=?, done_at=?, claimed_at=NULL, last_error=NULL WHERE id=?",
                (DONE, ts_str(self._clock()), item_id),
            )
            if cur.rowcount == 0:
                raise StoreError(f"no outbox item {item_id}")
        return self.get(item_id)

    def backoff_s(self, attempts: int) -> float:
        return min(self.backoff_base_s * (2 ** max(attempts - 1, 0)), self.backoff_max_s)

    def mark_failed(self, item_id: int, error: str | BaseException = "") -> OutboxItem:
        now = self._clock()
        with self._db.tx() as c:
            r = c.execute("SELECT attempts FROM outbox WHERE id=?", (item_id,)).fetchone()
            if r is None:
                raise StoreError(f"no outbox item {item_id}")
            attempts = r["attempts"] + 1
            nxt = now + timedelta(seconds=self.backoff_s(attempts))
            c.execute(
                "UPDATE outbox SET status=?, attempts=?, next_attempt_at=?, claimed_at=NULL, "
                "last_error=? WHERE id=?",
                (PENDING, attempts, ts_str(nxt), str(error)[:500], item_id),
            )
        return self.get(item_id)

    def recover(self, *, only_expired: bool = False) -> int:
        """Return in-flight claims to the queue. Call it once at startup, from
        the sync-client process only (Store(recover_outbox=True)): any other
        process would steal that client's live leases and cause double
        uploads. only_expired=True touches just claims older than lease_s."""
        with self._db.tx() as c:
            if only_expired:
                stale = ts_str(self._clock() - timedelta(seconds=self.lease_s))
                return c.execute(
                    "UPDATE outbox SET status=?, claimed_at=NULL WHERE status=? AND claimed_at<=?",
                    (PENDING, INFLIGHT, stale),
                ).rowcount
            return c.execute(
                "UPDATE outbox SET status=?, claimed_at=NULL WHERE status=?", (PENDING, INFLIGHT)
            ).rowcount

    def pending(self, limit: int = 100) -> list[OutboxItem]:
        rows = self._db.query(
            "SELECT * FROM outbox WHERE status<>? ORDER BY next_attempt_at, id LIMIT ?", (DONE, limit)
        )
        return [OutboxItem.from_row(r) for r in rows]

    def stats(self) -> OutboxStats:
        counts = {
            r["status"]: r["n"]
            for r in self._db.query("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")
        }
        failing = self._db.query_one(
            "SELECT COUNT(*) FROM outbox WHERE status<>? AND attempts>0", (DONE,)
        )[0]
        oldest = self._db.query_one(
            "SELECT MIN(created_at) FROM outbox WHERE status<>?", (DONE,)
        )[0]
        err = self._db.query_one(
            "SELECT last_error FROM outbox WHERE status<>? AND last_error IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (DONE,),
        )
        return OutboxStats(
            pending=counts.get(PENDING, 0),
            inflight=counts.get(INFLIGHT, 0),
            done=counts.get(DONE, 0),
            failing=failing,
            oldest_pending_at=parse_ts(oldest),
            last_error=err[0] if err else None,
        )

    def prune_done(self, older_than: timedelta = timedelta(days=30)) -> int:
        """Drop bookkeeping rows for uploads confirmed long ago (the files stay)."""
        cutoff = ts_str(self._clock() - older_than)
        with self._db.tx() as c:
            return c.execute(
                "DELETE FROM outbox WHERE status=? AND done_at<?", (DONE, cutoff)
            ).rowcount
