"""Append-only event log (SPEC 7.3): presses, plays, prints, recordings, moods.

Rows are only ever inserted. A late result (e.g. a mood recognized after the
box came back online) is a new event carrying the original timestamp via `ts=`.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .content import check_rel_path
from .db import Database, StoreError, parse_ts, ts_str

log = logging.getLogger(__name__)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

MOOD_LABELS = ("happy", "mad", "sad", "neutral", "unknown", "silent")
RECOGNIZED_MOODS = ("happy", "mad", "sad", "neutral")


class EventType:
    """Well-known event types. Any other non-empty string is allowed too."""

    PRESS = "press"
    PLAY = "play"
    PRINT = "print"
    RECORDING = "recording"
    MOOD = "mood"
    MOOD_PENDING = "mood_pending"  # audio queued for offline recognition
    QUIET_PRESS = "quiet_press"
    ERROR = "error"


@dataclass(frozen=True)
class Event:
    id: int
    ts: datetime
    day: date
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    media: str | None = None

    @classmethod
    def from_row(cls, r) -> "Event":
        return cls(
            id=r["id"],
            ts=parse_ts(r["ts"]),
            day=date.fromisoformat(r["day"]),
            type=r["type"],
            payload=json.loads(r["payload"]),
            media=r["media"],
        )


def _png_bytes(img: Any) -> bytes:
    if isinstance(img, (bytes, bytearray, memoryview)):
        data = bytes(img)
        if not data.startswith(PNG_MAGIC):
            raise StoreError("record_print: bytes are not a PNG")
        return data
    if hasattr(img, "save"):  # PIL.Image.Image, without importing Pillow here
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    raise StoreError(f"record_print: expected PNG bytes or a PIL image, got {type(img).__name__}")


class EventLog:
    def __init__(
        self,
        db: Database,
        clock: Callable[[], datetime],
        box_day: Callable[[datetime | None], date],
        local: Callable[[datetime | None], datetime],
        write_media: Callable[[str, bytes], Path],
        media_exists: Callable[[str], bool],
        on_print: Callable[[Event, str], None] | None = None,
        remove_media: Callable[[str], None] | None = None,
    ):
        self._db = db
        self._remove_media = remove_media
        self._clock = clock
        self._box_day = box_day
        self._local = local
        self._write_media = write_media
        self._exists = media_exists
        self._on_print = on_print

    def _discard_media(self, rel: str) -> None:
        if self._remove_media is None:
            return
        try:
            self._remove_media(rel)
        except Exception:  # pragma: no cover - best effort cleanup
            log.exception("store: could not remove orphaned print %s", rel)

    def append(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        media: str | None = None,
        ts: datetime | None = None,
    ) -> Event:
        if not isinstance(type, str) or not type:
            raise StoreError("event type must be a non-empty string")
        ts = ts or self._clock()
        if media is not None:
            media = check_rel_path(media)
        with self._db.tx() as c:
            cur = c.execute(
                "INSERT INTO events(ts, day, type, payload, media) VALUES (?,?,?,?,?)",
                (ts_str(ts), self._box_day(ts).isoformat(), type, json.dumps(payload or {}), media),
            )
            row_id = cur.lastrowid
        return self.get(row_id)

    def get(self, event_id: int) -> Event:
        r = self._db.query_one("SELECT * FROM events WHERE id=?", (event_id,))
        if r is None:
            raise StoreError(f"no event {event_id}")
        return Event.from_row(r)

    def query(
        self,
        types: str | Iterable[str] | None = None,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        day_from: date | None = None,
        day_to: date | None = None,
        after_id: int | None = None,
        limit: int | None = 1000,
        newest_first: bool = False,
    ) -> list[Event]:
        """Filter by type(s), timestamp range [since, until), box-day range
        [day_from, day_to] (inclusive) and/or id cursor (for the sync client)."""
        where, params = [], []
        if types is not None:
            tl = [types] if isinstance(types, str) else list(types)
            where.append(f"type IN ({','.join('?' * len(tl))})")
            params += tl
        if since is not None:
            where.append("ts>=?")
            params.append(ts_str(since))
        if until is not None:
            where.append("ts<?")
            params.append(ts_str(until))
        if day_from is not None:
            where.append("day>=?")
            params.append(day_from.isoformat())
        if day_to is not None:
            where.append("day<=?")
            params.append(day_to.isoformat())
        if after_id is not None:
            where.append("id>?")
            params.append(after_id)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        order = "DESC" if newest_first else "ASC"
        sql += f" ORDER BY ts {order}, id {order}" if after_id is None else " ORDER BY id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [Event.from_row(r) for r in self._db.query(sql, params)]

    def count(self, type: str | None = None, day: date | None = None) -> int:
        where, params = [], []
        if type is not None:
            where.append("type=?")
            params.append(type)
        if day is not None:
            where.append("day=?")
            params.append(day.isoformat())
        sql = "SELECT COUNT(*) FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        return self._db.query_one(sql, params)[0]

    # -- prints ---------------------------------------------------------------
    def record_print(
        self,
        item: str,
        img: Any,
        *,
        reprint: bool = False,
        content_id: str | None = None,
        extra: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> Event:
        """Archive a copy of a print (PNG bytes or PIL image) under
        media/prints/YYYY/MM/DD/ (local date) and log a 'print' event."""
        ts = ts or self._clock()
        data = _png_bytes(img)
        local = self._local(ts)
        stamp = local.strftime("%H%M%S") + f"{local.microsecond // 1000:03d}"
        safe_item = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in item) or "print"
        rel = f"prints/{local:%Y/%m/%d}/{stamp}-{safe_item}{'-reprint' if reprint else ''}.png"
        n = 1
        base = rel[:-4]
        while self._exists(rel):
            n += 1
            rel = f"{base}-{n}.png"
        self._write_media(rel, data)
        payload = {"item": item, "reprint": reprint, **(extra or {})}
        if content_id is not None:
            payload["content_id"] = content_id
        try:
            # One transaction: the print event and its upload queue entry
            # commit together or not at all (SPEC 7.3).
            with self._db.tx():
                ev = self.append(EventType.PRINT, payload, media=rel, ts=ts)
                if self._on_print:
                    self._on_print(ev, rel)
        except BaseException:
            self._discard_media(rel)
            raise
        return ev

    def prints(self, day: date | None = None, *, include_reprints: bool = True) -> list[Event]:
        evs = self.query(EventType.PRINT, day_from=day, day_to=day, limit=None)
        return evs if include_reprints else [e for e in evs if not e.payload.get("reprint")]

    # -- moods ----------------------------------------------------------------
    def record_mood(
        self,
        label: str,
        *,
        transcript: str | None = None,
        source: str = "online",
        audio: str | None = None,
        ts: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Event:
        """Log one mood run (SPEC 2.1). For offline recognition, pass the
        original recording time as ts so it lands on the right day and order."""
        if label not in MOOD_LABELS:
            raise StoreError(f"mood label must be one of {MOOD_LABELS}, got {label!r}")
        payload = {"label": label, "transcript": transcript, "source": source, **(extra or {})}
        return self.append(EventType.MOOD, payload, media=audio, ts=ts)

    def moods(self, day_from: date | None = None, day_to: date | None = None) -> list[Event]:
        return self.query(EventType.MOOD, day_from=day_from, day_to=day_to, limit=None)

    def first_mood(self, day: date | None = None, *, recognized_only: bool = False) -> Event | None:
        """'Today's mood' = the earliest mood run of the box day (SPEC 2.1)."""
        day = day or self._box_day(None)
        for ev in self.query(EventType.MOOD, day_from=day, day_to=day, limit=None):
            if not recognized_only or ev.payload.get("label") in RECOGNIZED_MOODS:
                return ev
        return None

    def daily_moods(self, day_from: date, day_to: date) -> dict[date, str]:
        """Box day -> today's-mood label, for the dashboard mood log."""
        out: dict[date, str] = {}
        for ev in self.moods(day_from, day_to):
            out.setdefault(ev.day, ev.payload.get("label"))
        return out
