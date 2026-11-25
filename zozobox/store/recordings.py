"""Voice-message slots (SPEC 2.2, 7.3).

Two single-message slots:
- `zozo_message`: Zozo's live reply to grandma (reviewed, then published by the cloud).
- `grandma_message`: grandma's latest message, played without review.

`put()` makes a new recording live and marks the previous live one
superseded. `withhold()` is the parent 'delete': the recording stops being
live/published but stays in the archive. Nothing is hard-deleted except by
`purge()`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable

from .content import check_rel_path
from .db import Database, StoreError, parse_ts, ts_str


class Slot(str, Enum):
    ZOZO = "zozo_message"
    GRANDMA = "grandma_message"


class RecStatus(str, Enum):
    LIVE = "live"
    SUPERSEDED = "superseded"
    WITHHELD = "withheld"


@dataclass(frozen=True)
class Recording:
    id: str
    slot: Slot
    media: str
    created_at: datetime
    duration_s: float | None
    status: RecStatus
    meta: dict[str, Any] = field(default_factory=dict)
    superseded_at: datetime | None = None
    superseded_by: str | None = None
    withheld_at: datetime | None = None
    heard_at: datetime | None = None
    published_at: datetime | None = None

    @property
    def live(self) -> bool:
        return self.status is RecStatus.LIVE

    @classmethod
    def from_row(cls, r) -> "Recording":
        return cls(
            id=r["id"],
            slot=Slot(r["slot"]),
            media=r["media"],
            created_at=parse_ts(r["created_at"]),
            duration_s=r["duration_s"],
            status=RecStatus(r["status"]),
            meta=json.loads(r["meta"]),
            superseded_at=parse_ts(r["superseded_at"]),
            superseded_by=r["superseded_by"],
            withheld_at=parse_ts(r["withheld_at"]),
            heard_at=parse_ts(r["heard_at"]),
            published_at=parse_ts(r["published_at"]),
        )


class RecordingSlots:
    def __init__(
        self,
        db: Database,
        clock: Callable[[], datetime],
        on_purge: Callable[[Recording], None] | None = None,
    ):
        self._db = db
        self._clock = clock
        self._on_purge = on_purge

    def put(
        self,
        slot: Slot | str,
        media: str,
        *,
        duration_s: float | None = None,
        created_at: datetime | None = None,
        meta: dict[str, Any] | None = None,
        rec_id: str | None = None,
    ) -> Recording:
        """Make a new recording live in `slot`; the old live one becomes superseded.
        A recording older than the current live one (e.g. a late sync) is
        archived as superseded instead of replacing it."""
        slot = Slot(slot)
        media = check_rel_path(media)
        created = created_at or self._clock()
        rec_id = rec_id or uuid.uuid4().hex
        now = ts_str(self._clock())
        with self._db.tx() as c:
            if c.execute("SELECT 1 FROM recordings WHERE id=?", (rec_id,)).fetchone():
                raise StoreError(f"recording {rec_id!r} already exists")
            cur = c.execute(
                "SELECT id, created_at FROM recordings WHERE slot=? AND status='live'", (slot.value,)
            ).fetchone()
            newer = cur is None or ts_str(created) >= cur["created_at"]
            c.execute(
                "INSERT INTO recordings(id, slot, media, created_at, duration_s, meta, status, "
                "superseded_at, superseded_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rec_id,
                    slot.value,
                    media,
                    ts_str(created),
                    duration_s,
                    json.dumps(meta or {}),
                    RecStatus.LIVE.value if newer else RecStatus.SUPERSEDED.value,
                    None if newer else now,
                    None if newer else cur["id"],
                ),
            )
            if newer and cur is not None:
                c.execute(
                    "UPDATE recordings SET status='superseded', superseded_at=?, superseded_by=? "
                    "WHERE id=?",
                    (now, rec_id, cur["id"]),
                )
        return self.get(rec_id)

    def get(self, rec_id: str) -> Recording:
        r = self._db.query_one("SELECT * FROM recordings WHERE id=?", (rec_id,))
        if r is None:
            raise StoreError(f"no recording {rec_id!r}")
        return Recording.from_row(r)

    def find(self, rec_id: str) -> Recording | None:
        r = self._db.query_one("SELECT * FROM recordings WHERE id=?", (rec_id,))
        return Recording.from_row(r) if r else None

    def live(self, slot: Slot | str) -> Recording | None:
        r = self._db.query_one(
            "SELECT * FROM recordings WHERE slot=? AND status='live' ORDER BY created_at DESC LIMIT 1",
            (Slot(slot).value,),
        )
        return Recording.from_row(r) if r else None

    def history(self, slot: Slot | str | None = None, *, limit: int = 100) -> list[Recording]:
        """Archive view, newest first, every status included."""
        if slot is None:
            rows = self._db.query(
                "SELECT * FROM recordings ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        else:
            rows = self._db.query(
                "SELECT * FROM recordings WHERE slot=? ORDER BY created_at DESC LIMIT ?",
                (Slot(slot).value, limit),
            )
        return [Recording.from_row(r) for r in rows]

    def _update(self, rec_id: str, sql: str, params=()) -> Recording:
        with self._db.tx() as c:
            if c.execute(f"UPDATE recordings SET {sql} WHERE id=?", (*params, rec_id)).rowcount == 0:
                raise StoreError(f"no recording {rec_id!r}")
        return self.get(rec_id)

    def withhold(self, rec_id: str) -> Recording:
        """Parent delete: no longer live or published; kept in the archive."""
        return self._update(
            rec_id,
            "status='withheld', withheld_at=COALESCE(withheld_at, ?)",
            (ts_str(self._clock()),),
        )

    def mark_heard(self, rec_id: str, at: datetime | None = None) -> Recording:
        return self._update(
            rec_id, "heard_at=COALESCE(heard_at, ?)", (ts_str(at or self._clock()),)
        )

    def mark_published(self, rec_id: str, at: datetime | None = None) -> Recording:
        return self._update(
            rec_id, "published_at=COALESCE(published_at, ?)", (ts_str(at or self._clock()),)
        )

    def has_unheard(self, slot: Slot | str = Slot.GRANDMA) -> bool:
        """LED 'something new' for grandma's message (SPEC 4)."""
        rec = self.live(slot)
        return rec is not None and rec.heard_at is None

    def review_ends_at(self, rec: Recording, review_delay_s: float) -> datetime:
        """Nominal end of the review window (recording time + delay). The cloud
        is authoritative and also pauses the window during quiet hours."""
        return rec.created_at + timedelta(seconds=review_delay_s)

    def purge(self, rec_id: str, *, confirm: bool = False) -> Recording:
        if not confirm:
            raise StoreError("purge needs confirm=True")
        rec = self.get(rec_id)
        with self._db.tx() as c:
            c.execute("DELETE FROM recordings WHERE id=?", (rec_id,))
        if self._on_purge:
            self._on_purge(rec)
        return rec
