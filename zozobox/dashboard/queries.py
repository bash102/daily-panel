"""Read-side helpers that turn the store into what each dashboard page shows.

Every function here only reads. Each section of the home page is computed
separately and failures are contained (logged, shown as unavailable), so a
problem in one table never takes the whole dashboard down.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Callable

from ..store import (
    PRINT_ROTATION,
    Approval,
    ContentItem,
    Event,
    Kind,
    Recording,
    RecStatus,
    Safety,
    Slot,
    Store,
)

log = logging.getLogger(__name__)

SPEND_EVENT = "spend"
SPEND_CAP_EVENT = "spend_cap_reached"
SAFETY_FAILED_EVENT = "safety_failed"
WITHHOLD_EVENT = "dashboard_withhold"

AUDIO_EXTS = {".wav": "audio/wav", ".opus": "audio/ogg", ".ogg": "audio/ogg",
              ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4"}
IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif"}
OTHER_EXTS = {".json": "application/json", ".txt": "text/plain; charset=utf-8"}
MEDIA_TYPES = {**AUDIO_EXTS, **IMAGE_EXTS, **OTHER_EXTS}

CONTENT_KINDS = [k.value for k in Kind]
ARCHIVE_KINDS = CONTENT_KINDS + ["prints", "recordings", "mood"]
MOOD_BAR_LABELS = ("happy", "mad", "sad", "neutral", "unknown", "silent")


def media_kind(rel: str | None) -> str | None:
    """'audio' | 'image' | 'other' | None, from the file extension."""
    if not rel:
        return None
    ext = PurePosixPath(rel).suffix.lower()
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    return "other"


def safe(fn: Callable[[], Any], default: Any = None, what: str = "") -> Any:
    try:
        return fn()
    except Exception:
        log.exception("dashboard: could not load %s", what or getattr(fn, "__name__", "data"))
        return default


def _num(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    f = float(v)
    return f if math.isfinite(f) else 0.0


# -- spend ------------------------------------------------------------------


@dataclass(frozen=True)
class Spend:
    month_to_date_usd: float
    cap_usd: float
    cap_reached_at: datetime | None
    by_provider: dict[str, float]

    @property
    def fraction(self) -> float:
        if self.cap_usd <= 0:
            return 1.0
        return min(self.month_to_date_usd / self.cap_usd, 1.0)

    @property
    def over(self) -> bool:
        return self.month_to_date_usd >= self.cap_usd or self.cap_reached_at is not None


def month_start_utc(store: Store, now: datetime | None = None) -> datetime:
    local = store.settings.local(now or store.now())
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def spend(store: Store) -> Spend:
    since = month_start_utc(store)
    total = 0.0
    by: dict[str, float] = defaultdict(float)
    for ev in store.events.query(SPEND_EVENT, since=since, limit=None):
        usd = _num(ev.payload.get("usd"))
        total += usd
        by[str(ev.payload.get("provider") or "other")] += usd
    capped = store.events.query(SPEND_CAP_EVENT, since=since, limit=1, newest_first=True)
    return Spend(round(total, 4), float(store.settings.spend_cap_usd),
                 capped[0].ts if capped else None, dict(by))


# -- recordings / review window ------------------------------------------------


def _next_quiet_transition(t: datetime, quiet: Any, tz: Any) -> datetime:
    """The next instant after `t` (UTC) where quiet hours start or end."""
    local = t.astimezone(tz)
    best: datetime | None = None
    for days in (0, 1, 2):
        d = local.date() + timedelta(days=days)
        for hm in (quiet.start, quiet.end):
            cand = datetime.combine(d, hm.replace(tzinfo=None), tzinfo=tz).astimezone(timezone.utc)
            if cand > t and (best is None or cand < best):
                best = cand
    return best or (t + timedelta(days=1))


def effective_publish_at(recorded_at: datetime, review_delay_s: float, quiet: Any,
                         tz: Any) -> datetime:
    """When the cloud publishes a pending Zozo message (docs/SYNC.md): the
    review clock runs only outside quiet hours. Mirrors computePublishAt in
    cloud/worker/src/time.ts: a message recorded in quiet hours starts its
    clock when they end, and a window that runs out exactly as quiet hours
    start publishes when they end."""
    remaining = max(0.0, float(review_delay_s))
    t = recorded_at.astimezone(timezone.utc)
    if quiet.start == quiet.end:
        return t + timedelta(seconds=remaining)
    for _ in range(2000):
        nt = _next_quiet_transition(t, quiet, tz)
        if quiet.contains(t.astimezone(tz).time()):
            t = nt
            continue
        left = (nt - t).total_seconds()
        if remaining < left:
            return t + timedelta(seconds=remaining)
        remaining -= left
        t = nt
    return t + timedelta(seconds=remaining)


@dataclass(frozen=True)
class ReviewInfo:
    """Zozo's live message as the parent should see it. `ends_at` is the
    effective publish time (review clock paused in quiet hours, like the
    cloud). The cloud is authoritative: `published` comes from the
    published_at the sync client copies back."""
    rec: Recording
    ends_at: datetime
    remaining_s: float
    quiet_now: bool = False

    @property
    def published(self) -> bool:
        return self.rec.published_at is not None

    @property
    def in_review(self) -> bool:
        return self.remaining_s > 0 and self.rec.status is RecStatus.LIVE and not self.published

    @property
    def paused(self) -> bool:
        """In review and the clock is currently stopped for quiet hours."""
        return self.in_review and self.quiet_now

    @property
    def awaiting_publish(self) -> bool:
        """Window over locally, but the cloud hasn't reported publishing it."""
        return (not self.in_review and not self.published
                and self.rec.status is RecStatus.LIVE)


def review_info(store: Store, rec: Recording | None) -> ReviewInfo | None:
    if rec is None:
        return None
    s = store.settings
    delay = s.review_delay_s
    try:
        ends = effective_publish_at(rec.created_at, delay, s.quiet_hours, s.tz)
    except Exception:
        log.exception("dashboard: could not compute review end; using the nominal one")
        ends = store.recordings.review_ends_at(rec, delay)
    now = store.now()
    quiet = safe(lambda: s.is_quiet(now), False, "quiet hours")
    return ReviewInfo(rec, ends, (ends - now).total_seconds(), bool(quiet))


# -- sync status ------------------------------------------------------------------

SYNC_STALE_S = 10 * 60


@dataclass(frozen=True)
class SyncInfo:
    last_run_at: datetime | None
    last_ok_at: datetime | None
    online: bool | None
    last_error: str | None
    stale: bool          # no successful pass for SYNC_STALE_S (or never)
    never: bool          # the sync client has never reported


def _parse_iso(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def sync_info(store: Store) -> SyncInfo:
    """The sync client's last-pass summary (zozobox.sync.load_status)."""
    from ..sync.state import load_status

    st = load_status(store.data_dir)
    run, ok = _parse_iso(st.last_run_at), _parse_iso(st.last_ok_at)
    now = store.now()
    stale = ok is None or (now - ok).total_seconds() > SYNC_STALE_S
    err = st.last_error if isinstance(st.last_error, str) and st.last_error else None
    online = st.online if isinstance(st.online, bool) else None
    return SyncInfo(run, ok, online, err, stale, run is None and ok is None)


# -- home ---------------------------------------------------------------------


@dataclass
class Home:
    today: date | None = None
    quiet: bool | None = None
    content: list[dict[str, Any]] = field(default_factory=list)
    pending_approvals: int | None = None
    snapshot: Any = None
    rotation: tuple[str, ...] = PRINT_ROTATION
    grandma_unheard: bool | None = None
    grandma_live: Recording | None = None
    zozo_review: ReviewInfo | None = None
    outbox: Any = None
    sync: SyncInfo | None = None
    disk: dict[str, int] | None = None
    spend: Spend | None = None
    safety_events: list[Event] = field(default_factory=list)
    safety_items: list[ContentItem] = field(default_factory=list)
    errors: list[Event] = field(default_factory=list)


def home(store: Store) -> Home:
    h = Home()
    h.today = safe(store.box_day, None, "box day")
    h.quiet = safe(store.is_quiet, None, "quiet hours")

    def content_rows():
        rows = []
        for kind in ("story", "letter", "weather", "coloring"):
            item = store.content.current_for_today(kind)
            rows.append({"kind": kind, "item": item})
        return rows

    h.content = safe(content_rows, [], "today's content")
    h.pending_approvals = safe(lambda: len(pending_approvals(store)), None, "approvals")
    h.snapshot = safe(store.daily.snapshot, None, "daily state")
    h.grandma_unheard = safe(lambda: store.recordings.has_unheard(Slot.GRANDMA), None, "grandma")
    h.grandma_live = safe(lambda: store.recordings.live(Slot.GRANDMA), None, "grandma")
    h.zozo_review = safe(lambda: review_info(store, store.recordings.live(Slot.ZOZO)), None,
                         "zozo message")
    h.outbox = safe(store.outbox.stats, None, "outbox")
    h.sync = safe(lambda: sync_info(store), None, "sync status")
    h.disk = safe(store.disk_usage, None, "disk usage")
    h.spend = safe(lambda: spend(store), None, "spend")
    since = store.now() - timedelta(days=14)
    h.safety_events = safe(lambda: store.events.query(
        SAFETY_FAILED_EVENT, since=since, limit=10, newest_first=True), [], "safety events")
    h.safety_items = safe(lambda: store.content.list(safety=Safety.FAILED, limit=10), [],
                          "safety items")
    h.errors = safe(lambda: store.events.query(
        "error", since=store.now() - timedelta(days=1), limit=10, newest_first=True), [],
        "errors")
    return h


# -- archive --------------------------------------------------------------------


def parse_day(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        return None


def content_local_day(store: Store, item: ContentItem) -> date:
    return item.for_date or store.settings.local(item.created_at).date()


def archive(store: Store, kind: str, day: date | None, limit: int = 200) -> list[Any]:
    """Items for one archive tab, newest first, optionally limited to one day
    (box day for events, for_date / local creation date for content,
    local recording date for recordings)."""
    if kind in CONTENT_KINDS:
        items = store.content.list(kind, limit=5000)
        if day is not None:
            items = [i for i in items if content_local_day(store, i) == day]
        return items[:limit]
    if kind == "prints":
        return store.events.query("print", day_from=day, day_to=day, limit=limit,
                                  newest_first=True)
    if kind == "recordings":
        recs = store.recordings.history(limit=5000)
        if day is not None:
            recs = [r for r in recs if store.settings.local(r.created_at).date() == day]
        return recs[:limit]
    if kind == "mood":
        return store.events.query(["mood", "mood_pending"], day_from=day, day_to=day,
                                  limit=limit, newest_first=True)
    return []


# -- mood log -------------------------------------------------------------------


@dataclass
class MoodDay:
    day: date
    first: Event | None
    first_recognized: Event | None
    entries: list[Event]
    pending: list[Event]


def mood_log(store: Store, days: int = 30) -> tuple[list[MoodDay], list[tuple[str, int, int]]]:
    """Days newest first, plus a (label, count, percent) summary of each
    day's first mood over the window."""
    today = store.box_day()
    start = today - timedelta(days=days - 1)
    by_day: dict[date, list[Event]] = defaultdict(list)
    for ev in store.events.moods(start, today):
        by_day[ev.day].append(ev)
    resolved = {e.media for e in store.events.query("mood", limit=None) if e.media}
    pend: dict[date, list[Event]] = defaultdict(list)
    for ev in store.events.query("mood_pending", day_from=start, day_to=today, limit=None):
        if ev.media not in resolved:
            pend[ev.day].append(ev)
    out = []
    counts: Counter[str] = Counter()
    for i in range(days):
        d = today - timedelta(days=i)
        evs = by_day.get(d, [])
        first = evs[0] if evs else None
        rec = next((e for e in evs if e.payload.get("label") in ("happy", "mad", "sad",
                                                                 "neutral")), None)
        if first is not None:
            counts[str(first.payload.get("label"))] += 1
        if evs or pend.get(d):
            out.append(MoodDay(d, first, rec, evs, pend.get(d, [])))
    total = sum(counts.values()) or 1
    summary = [(lbl, counts.get(lbl, 0), round(100 * counts.get(lbl, 0) / total))
               for lbl in MOOD_BAR_LABELS]
    return out, summary


# -- stats ----------------------------------------------------------------------


@dataclass
class StatsDay:
    day: date
    presses: dict[str, int]
    quiet_presses: int
    story_heard: bool
    prints: int
    reprints: int


def stats(store: Store, days: int = 14) -> tuple[list[StatsDay], list[str]]:
    today = store.box_day()
    start = today - timedelta(days=days - 1)
    presses: dict[date, Counter[str]] = defaultdict(Counter)
    columns: set[str] = set()
    for ev in store.events.query("press", day_from=start, day_to=today, limit=None):
        if ev.payload.get("action") == "ignored":
            continue
        role = str(ev.payload.get("role") or ev.payload.get("button") or "?")
        columns.add(role)
        presses[ev.day][role] += 1
    quiet: Counter[date] = Counter(
        ev.day for ev in store.events.query("quiet_press", day_from=start, day_to=today,
                                            limit=None))
    prints: Counter[date] = Counter()
    reprints: Counter[date] = Counter()
    for ev in store.events.query("print", day_from=start, day_to=today, limit=None):
        (reprints if ev.payload.get("reprint") else prints)[ev.day] += 1
    order = ["print", "grandma", "story", "soundboard"]
    cols = [c for c in order if c in columns] + sorted(columns - set(order))
    if not cols:
        cols = order
    rows = []
    for i in range(days):
        d = today - timedelta(days=i)
        snap = store.daily.snapshot(d)
        rows.append(StatsDay(d, {c: presses[d].get(c, 0) for c in cols}, quiet[d],
                             snap.story_heard, prints[d], reprints[d]))
    return rows, cols


def pending_approvals(store: Store) -> list[ContentItem]:
    """The approval queue: pending items that haven't failed the safety check
    (failed ones are never servable and show under safety checks instead)."""
    return [i for i in store.content.pending_approval()
            if i.approval is Approval.PENDING and i.safety is not Safety.FAILED]
