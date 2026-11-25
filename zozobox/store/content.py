"""Generated content cache (SPEC 7.1).

Each item is one generated thing (a story, a letter card, today's weather, a
coloring page, a fixed phrase) with its media files (paths relative to the
media dir), metadata, safety state and approval state.

Serving rules ("only regenerate what was served"):
- servable = safety passed AND approval in (approved, auto), not withheld,
  not superseded.
- `current_for_today(kind, day)`: an item first served on `day` stays that
  day's item. Otherwise the oldest servable unserved item is offered, so an
  unserved item carries over to later days. Weather only ever offers items
  made for that day; among several servable ones the newest wins.
- Weather is a forecast for a calendar date. Without an explicit for_date it
  is for `weather_day(created)`: the local calendar date, so weather fetched
  by the nightly job after midnight (before day_start) is for the coming
  morning, not for the box day that is ending. `needs_generation('weather')`
  without `today` checks the same day.
- A newer weather item for a date supersedes older unserved weather for that
  date only once it is servable itself (safety passed and approved/auto). An
  unapproved or failed refresh never displaces weather Zozo can already get.
- `needs_generation(kind, day)`: weather -> no usable item for `day` yet
  (servable, or still pending safety/approval); other kinds -> no unserved
  item that is servable or still pending safety/approval.
- Nothing is hard-deleted except by `purge()`: parent deletion withholds,
  replacement supersedes.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable

from .db import Database, StoreError, parse_ts, ts_str


class Kind(str, Enum):
    STORY = "story"
    LETTER = "letter"
    WEATHER = "weather"
    COLORING = "coloring"
    PHRASE = "phrase"


class Safety(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class Approval(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO = "auto"


SERVABLE_APPROVALS = (Approval.APPROVED.value, Approval.AUTO.value)


def check_rel_path(p: str | PurePosixPath) -> str:
    """Media paths are POSIX paths relative to the media dir, never escaping it."""
    s = str(p).replace("\\", "/")
    pp = PurePosixPath(s)
    if not s or pp.is_absolute() or ".." in pp.parts or s.startswith("~"):
        raise StoreError(f"media path must be relative inside the media dir: {p!r}")
    return str(pp)


@dataclass(frozen=True)
class ContentItem:
    id: str
    kind: Kind
    created_at: datetime
    for_date: date | None
    media: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    safety: Safety = Safety.PENDING
    safety_note: str | None = None
    approval: Approval = Approval.PENDING
    approval_at: datetime | None = None
    first_served_at: datetime | None = None
    served_day: date | None = None
    completed_at: datetime | None = None
    withheld_at: datetime | None = None
    superseded_at: datetime | None = None
    superseded_by: str | None = None

    @property
    def withheld(self) -> bool:
        return self.withheld_at is not None

    @property
    def superseded(self) -> bool:
        return self.superseded_at is not None

    @property
    def served(self) -> bool:
        return self.first_served_at is not None

    @property
    def servable(self) -> bool:
        return (
            self.safety is Safety.PASSED
            and self.approval.value in SERVABLE_APPROVALS
            and not self.withheld
            and not self.superseded
        )

    @classmethod
    def from_row(cls, r) -> "ContentItem":
        return cls(
            id=r["id"],
            kind=Kind(r["kind"]),
            created_at=parse_ts(r["created_at"]),
            for_date=date.fromisoformat(r["for_date"]) if r["for_date"] else None,
            media=json.loads(r["media"]),
            meta=json.loads(r["meta"]),
            safety=Safety(r["safety"]),
            safety_note=r["safety_note"],
            approval=Approval(r["approval"]),
            approval_at=parse_ts(r["approval_at"]),
            first_served_at=parse_ts(r["first_served_at"]),
            served_day=date.fromisoformat(r["served_day"]) if r["served_day"] else None,
            completed_at=parse_ts(r["completed_at"]),
            withheld_at=parse_ts(r["withheld_at"]),
            superseded_at=parse_ts(r["superseded_at"]),
            superseded_by=r["superseded_by"],
        )


_SERVABLE_SQL = (
    "safety='passed' AND approval IN ('approved','auto') "
    "AND withheld_at IS NULL AND superseded_at IS NULL"
)
_LIVE_UNSERVED_SQL = (
    "first_served_at IS NULL AND withheld_at IS NULL AND superseded_at IS NULL "
    "AND safety IN ('pending','passed') AND approval IN ('pending','approved','auto')"
)


class ContentRepo:
    def __init__(
        self,
        db: Database,
        clock: Callable[[], datetime],
        box_day: Callable[[datetime | None], date],
        approval_required: Callable[[date], bool],
        on_purge: Callable[[ContentItem], None] | None = None,
        local: Callable[[datetime], datetime] | None = None,
    ):
        self._db = db
        self._local = local
        self._clock = clock
        self._box_day = box_day
        self._approval_required = approval_required
        self._on_purge = on_purge

    # -- create ---------------------------------------------------------------
    def add(
        self,
        kind: Kind | str,
        *,
        media: dict[str, str] | None = None,
        meta: dict[str, Any] | None = None,
        for_date: date | None = None,
        safety: Safety | str = Safety.PENDING,
        approval: Approval | str | None = None,
        created_at: datetime | None = None,
    ) -> ContentItem:
        """Add a generated item. approval=None picks PENDING during the first
        month (approval_required) and AUTO afterwards. Weather defaults to
        for_date=weather_day(created); once servable it supersedes earlier
        unserved weather for the same date."""
        kind = Kind(kind)
        safety = Safety(safety)
        created = created_at or self._clock()
        if approval is None:
            approval = (
                Approval.PENDING if self._approval_required(self._box_day(created)) else Approval.AUTO
            )
        approval = Approval(approval)
        if kind is Kind.WEATHER and for_date is None:
            for_date = self.weather_day(created)
        media = {str(k): check_rel_path(v) for k, v in (media or {}).items()}
        item_id = uuid.uuid4().hex
        with self._db.tx() as c:
            c.execute(
                "INSERT INTO content(id, kind, created_at, for_date, media, meta, safety, approval) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    kind.value,
                    ts_str(created),
                    for_date.isoformat() if for_date else None,
                    json.dumps(media),
                    json.dumps(meta or {}),
                    safety.value,
                    approval.value,
                ),
            )
            if kind is Kind.WEATHER:
                self._supersede_older_weather(c, item_id)
        return self.get(item_id)

    def weather_day(self, dt: datetime | None = None) -> date:
        """The date weather made at `dt` is for: the local calendar date. Equal
        to the box day except between midnight and day_start, when the box
        day is still yesterday but the forecast is wanted for this morning."""
        dt = dt or self._clock()
        if self._local is None:
            return self._box_day(dt)
        return self._local(dt).date()

    def _supersede_older_weather(self, c, item_id: str) -> int:
        """If weather item `item_id` is servable, supersede the older unserved
        weather for its date. Runs inside the caller's transaction."""
        me = c.execute(
            f"SELECT rowid, created_at, for_date FROM content WHERE id=? AND kind='weather' "
            f"AND {_SERVABLE_SQL}",
            (item_id,),
        ).fetchone()
        if me is None or me["for_date"] is None:
            return 0
        return c.execute(
            "UPDATE content SET superseded_at=?, superseded_by=? WHERE kind='weather' "
            "AND for_date=? AND id<>? AND first_served_at IS NULL AND superseded_at IS NULL "
            "AND (created_at<? OR (created_at=? AND rowid<?))",
            (
                ts_str(self._clock()),
                item_id,
                me["for_date"],
                item_id,
                me["created_at"],
                me["created_at"],
                me["rowid"],
            ),
        ).rowcount

    # -- read -----------------------------------------------------------------
    def get(self, item_id: str) -> ContentItem:
        r = self._db.query_one("SELECT * FROM content WHERE id=?", (item_id,))
        if r is None:
            raise StoreError(f"no content item {item_id!r}")
        return ContentItem.from_row(r)

    def find(self, item_id: str) -> ContentItem | None:
        r = self._db.query_one("SELECT * FROM content WHERE id=?", (item_id,))
        return ContentItem.from_row(r) if r else None

    def current_for_today(self, kind: Kind | str, today: date | None = None) -> ContentItem | None:
        kind = Kind(kind)
        day = (today or self._box_day(None)).isoformat()
        with self._db.lock:
            r = self._db.query_one(
                "SELECT * FROM content WHERE kind=? AND served_day=? AND withheld_at IS NULL "
                "ORDER BY first_served_at DESC LIMIT 1",
                (kind.value, day),
            )
            if r is not None:
                return ContentItem.from_row(r)
            if kind is Kind.WEATHER:
                r = self._db.query_one(
                    f"SELECT * FROM content WHERE kind='weather' AND for_date=? "
                    f"AND first_served_at IS NULL AND {_SERVABLE_SQL} "
                    "ORDER BY created_at DESC LIMIT 1",
                    (day,),
                )
            else:
                r = self._db.query_one(
                    f"SELECT * FROM content WHERE kind=? AND first_served_at IS NULL AND {_SERVABLE_SQL} "
                    "AND (for_date IS NULL OR for_date<=?) "
                    "ORDER BY COALESCE(for_date, substr(created_at,1,10)), created_at LIMIT 1",
                    (kind.value, day),
                )
            return ContentItem.from_row(r) if r else None

    def needs_generation(self, kind: Kind | str, today: date | None = None) -> bool:
        kind = Kind(kind)
        if kind is Kind.WEATHER:
            day = (today or self.weather_day(None)).isoformat()
            r = self._db.query_one(
                "SELECT 1 FROM content WHERE kind='weather' AND for_date=? AND withheld_at IS NULL "
                "AND superseded_at IS NULL AND safety IN ('pending','passed') "
                "AND approval IN ('pending','approved','auto') LIMIT 1",
                (day,),
            )
            return r is None
        r = self._db.query_one(
            f"SELECT 1 FROM content WHERE kind=? AND {_LIVE_UNSERVED_SQL} LIMIT 1", (kind.value,)
        )
        return r is None

    def phrase(self, key: str) -> ContentItem | None:
        """Newest servable fixed phrase whose meta['key'] == key. Phrases are
        cached permanently and never 'used up'."""
        r = self._db.query_one(
            f"SELECT * FROM content WHERE kind='phrase' AND json_extract(meta,'$.key')=? "
            f"AND {_SERVABLE_SQL} ORDER BY created_at DESC LIMIT 1",
            (key,),
        )
        return ContentItem.from_row(r) if r else None

    def list(
        self,
        kind: Kind | str | None = None,
        *,
        safety: Safety | str | None = None,
        approval: Approval | str | None = None,
        include_withheld: bool = True,
        include_superseded: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ContentItem]:
        """Newest first, for the dashboard (approval queue, safety flags, archive)."""
        where, params = [], []
        if kind is not None:
            where.append("kind=?")
            params.append(Kind(kind).value)
        if safety is not None:
            where.append("safety=?")
            params.append(Safety(safety).value)
        if approval is not None:
            where.append("approval=?")
            params.append(Approval(approval).value)
        if not include_withheld:
            where.append("withheld_at IS NULL")
        if not include_superseded:
            where.append("superseded_at IS NULL")
        sql = "SELECT * FROM content"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
        return [ContentItem.from_row(r) for r in self._db.query(sql, (*params, limit, offset))]

    def pending_approval(self) -> list[ContentItem]:
        return self.list(approval=Approval.PENDING, include_withheld=False, include_superseded=False)

    # -- state changes --------------------------------------------------------
    def _update(
        self, item_id: str, sql: str, params: Iterable = (), *, settle: bool = False
    ) -> ContentItem:
        """settle=True: the change may have made a weather item servable, so let
        it supersede older weather for its date in the same transaction."""
        with self._db.tx() as c:
            cur = c.execute(f"UPDATE content SET {sql} WHERE id=?", (*params, item_id))
            if cur.rowcount == 0:
                raise StoreError(f"no content item {item_id!r}")
            if settle:
                self._supersede_older_weather(c, item_id)
        return self.get(item_id)

    def mark_served(self, item_id: str, at: datetime | None = None) -> ContentItem:
        """Record the first serve (idempotent). Pins the item to that box day."""
        at = at or self._clock()
        return self._update(
            item_id,
            "first_served_at=COALESCE(first_served_at, ?), served_day=COALESCE(served_day, ?)",
            (ts_str(at), self._box_day(at).isoformat()),
        )

    def mark_completed(self, item_id: str, at: datetime | None = None) -> ContentItem:
        return self._update(
            item_id, "completed_at=COALESCE(completed_at, ?)", (ts_str(at or self._clock()),)
        )

    def set_safety(self, item_id: str, status: Safety | str, note: str | None = None) -> ContentItem:
        return self._update(
            item_id, "safety=?, safety_note=?", (Safety(status).value, note), settle=True
        )

    def set_media(self, item_id: str, media: dict[str, str]) -> ContentItem:
        media = {str(k): check_rel_path(v) for k, v in media.items()}
        return self._update(item_id, "media=?", (json.dumps(media),))

    def update_meta(self, item_id: str, **changes: Any) -> ContentItem:
        with self._db.tx():
            meta = dict(self.get(item_id).meta)
            meta.update(changes)
            return self._update(item_id, "meta=?", (json.dumps(meta),))

    def approve(self, item_id: str) -> ContentItem:
        return self._update(
            item_id, "approval='approved', approval_at=?", (ts_str(self._clock()),), settle=True
        )

    def reject(self, item_id: str) -> ContentItem:
        return self._update(
            item_id, "approval='rejected', approval_at=?", (ts_str(self._clock()),)
        )

    def withhold(self, item_id: str) -> ContentItem:
        """Parent 'delete': stops serving, keeps the item in the archive."""
        return self._update(
            item_id, "withheld_at=COALESCE(withheld_at, ?)", (ts_str(self._clock()),)
        )

    def unwithhold(self, item_id: str) -> ContentItem:
        return self._update(item_id, "withheld_at=NULL", settle=True)

    def supersede(self, item_id: str, by: str | None = None) -> ContentItem:
        return self._update(
            item_id,
            "superseded_at=COALESCE(superseded_at, ?), superseded_by=COALESCE(superseded_by, ?)",
            (ts_str(self._clock()), by),
        )

    def purge(self, item_id: str, *, confirm: bool = False) -> ContentItem:
        """Permanent delete (row + media files). Requires confirm=True (SPEC 7.3)."""
        if not confirm:
            raise StoreError("purge needs confirm=True")
        item = self.get(item_id)
        with self._db.tx() as c:
            c.execute("DELETE FROM content WHERE id=?", (item_id,))
        if self._on_purge:
            self._on_purge(item)
        return item
