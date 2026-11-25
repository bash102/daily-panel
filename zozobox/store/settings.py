"""Runtime settings set from the parent dashboard (SPEC section 8).

Every value is validated on the way in, persisted as JSON in the `settings`
table, and read back typed. A stored value that no longer parses (hand edits,
an old bug) is logged and replaced by its default rather than crashing the box.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database, ts_str

log = logging.getLogger(__name__)

# SPEC 2.1: the Print button cycles through these, restarting at Letter each morning.
PRINT_ROTATION: tuple[str, ...] = ("letter", "weather", "mood", "coloring")

REC_CAP_MIN_S = 1.0
REC_CAP_MAX_S = 300.0  # SPEC 2.2 / 8: max 5 min
APPROVAL_PERIOD_DAYS = 30  # SPEC 7.1: first month needs parent approval


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class QuietHours:
    """Local wall-clock window. May span midnight; start == end means never quiet."""

    start: time
    end: time

    def contains(self, t: time) -> bool:
        t = t.replace(tzinfo=None)
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


@dataclass(frozen=True)
class Location:
    name: str
    lat: float
    lon: float


_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(v: Any) -> time:
    if isinstance(v, time):
        return v.replace(second=0, microsecond=0, tzinfo=None)
    if isinstance(v, str):
        m = _HHMM.match(v.strip())
        if m:
            return time(int(m.group(1)), int(m.group(2)))
    raise SettingsError(f"expected HH:MM, got {v!r}")


def _hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def _number(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SettingsError(f"{name}: expected a number, got {v!r}")
    f = float(v)
    if not math.isfinite(f):
        raise SettingsError(f"{name}: must be finite")
    return f


def _range(lo: float | None, hi: float | None, name: str) -> Callable[[Any], float]:
    def parse(v: Any) -> float:
        f = _number(v, name)
        if (lo is not None and f < lo) or (hi is not None and f > hi):
            raise SettingsError(f"{name}: {f} outside {lo}..{hi}")
        return f

    return parse


def _parse_quiet(v: Any) -> QuietHours:
    if isinstance(v, QuietHours):
        return QuietHours(parse_hhmm(v.start), parse_hhmm(v.end))
    if isinstance(v, dict):
        return QuietHours(parse_hhmm(v.get("start")), parse_hhmm(v.get("end")))
    if isinstance(v, (tuple, list)) and len(v) == 2:
        return QuietHours(parse_hhmm(v[0]), parse_hhmm(v[1]))
    raise SettingsError(f"quiet_hours: expected start/end, got {v!r}")


def _parse_tz(v: Any) -> str:
    if isinstance(v, ZoneInfo):
        v = v.key
    if not isinstance(v, str) or not v:
        raise SettingsError(f"timezone: expected an IANA name, got {v!r}")
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise SettingsError(f"timezone: unknown zone {v!r}") from e
    return v


def _parse_location(v: Any) -> Location | None:
    if v is None:
        return None
    if isinstance(v, Location):
        name, lat, lon = v.name, v.lat, v.lon
    elif isinstance(v, dict):
        name, lat, lon = v.get("name"), v.get("lat"), v.get("lon")
    else:
        raise SettingsError(f"location: expected name/lat/lon, got {v!r}")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise SettingsError("location: name must be 1..100 characters")
    lat = _range(-90, 90, "location.lat")(lat)
    lon = _range(-180, 180, "location.lon")(lon)
    return Location(name.strip(), lat, lon)


def _parse_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        raise SettingsError("expected a date, not a datetime")
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError as e:
            raise SettingsError(f"expected YYYY-MM-DD, got {v!r}") from e
    raise SettingsError(f"expected a date, got {v!r}")


def _parse_int_range(lo: int, hi: int, name: str) -> Callable[[Any], int]:
    def parse(v: Any) -> int:
        f = _range(lo, hi, name)(v)
        if f != int(f):
            raise SettingsError(f"{name}: must be whole seconds")
        return int(f)

    return parse


@dataclass(frozen=True)
class _Spec:
    default: Any
    parse: Callable[[Any], Any]  # python or JSON form -> typed (validates)
    dump: Callable[[Any], Any]  # typed -> JSON-able


_ident = lambda v: v  # noqa: E731

SPECS: dict[str, _Spec] = {
    "quiet_hours": _Spec(
        QuietHours(time(19, 0), time(7, 0)),
        _parse_quiet,
        lambda q: {"start": _hhmm(q.start), "end": _hhmm(q.end)},
    ),
    "timezone": _Spec("America/New_York", _parse_tz, _ident),
    "day_start": _Spec(time(6, 0), parse_hhmm, _hhmm),
    "location": _Spec(
        None,
        _parse_location,
        lambda l: None if l is None else {"name": l.name, "lat": l.lat, "lon": l.lon},
    ),
    "rec_cap_s": _Spec(30.0, _range(REC_CAP_MIN_S, REC_CAP_MAX_S, "rec_cap_s"), _ident),
    "volume": _Spec(0.7, _range(0.0, 1.0, "volume"), _ident),
    "spend_cap_usd": _Spec(30.0, _range(0.0, 10_000.0, "spend_cap_usd"), _ident),
    "review_delay_s": _Spec(3600, _parse_int_range(0, 7 * 86400, "review_delay_s"), _ident),
    "approval_required_until": _Spec(
        None, _parse_date, lambda d: None if d is None else d.isoformat()
    ),
}


class Settings:
    def __init__(self, db: Database, clock: Callable[[], datetime]):
        self._db = db
        self._clock = clock

    # -- generic access (dashboard) -------------------------------------------
    def get(self, key: str) -> Any:
        spec = self._spec(key)
        row = self._db.query_one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return spec.default
        try:
            return spec.parse(json.loads(row["value"]))
        except (SettingsError, ValueError, TypeError):
            log.warning("settings: bad stored %s=%r, using default", key, row["value"])
            return spec.default

    def set(self, key: str, value: Any) -> Any:
        self.update({key: value})
        return self.get(key)

    def update(self, values: dict[str, Any]) -> None:
        """Validate every value first, then write all of them in one transaction."""
        parsed = {k: self._spec(k).parse(v) for k, v in values.items()}
        now = ts_str(self._clock())
        with self._db.tx() as c:
            for k, v in parsed.items():
                c.execute(
                    "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (k, json.dumps(SPECS[k].dump(v)), now),
                )

    def reset(self, key: str) -> None:
        self._spec(key)
        with self._db.tx() as c:
            c.execute("DELETE FROM settings WHERE key=?", (key,))

    def is_set(self, key: str) -> bool:
        self._spec(key)
        return self._db.query_one("SELECT 1 FROM settings WHERE key=?", (key,)) is not None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready snapshot of every setting (defaults included)."""
        return {k: s.dump(self.get(k)) for k, s in SPECS.items()}

    @staticmethod
    def _spec(key: str) -> _Spec:
        try:
            return SPECS[key]
        except KeyError:
            raise SettingsError(f"unknown setting {key!r}") from None

    # -- typed properties -----------------------------------------------------
    quiet_hours: QuietHours = property(  # type: ignore[assignment]
        lambda self: self.get("quiet_hours"), lambda self, v: self.set("quiet_hours", v)
    )
    timezone: str = property(  # type: ignore[assignment]
        lambda self: self.get("timezone"), lambda self, v: self.set("timezone", v)
    )
    day_start: time = property(  # type: ignore[assignment]
        lambda self: self.get("day_start"), lambda self, v: self.set("day_start", v)
    )
    location: Location | None = property(  # type: ignore[assignment]
        lambda self: self.get("location"), lambda self, v: self.set("location", v)
    )
    rec_cap_s: float = property(  # type: ignore[assignment]
        lambda self: self.get("rec_cap_s"), lambda self, v: self.set("rec_cap_s", v)
    )
    volume: float = property(  # type: ignore[assignment]
        lambda self: self.get("volume"), lambda self, v: self.set("volume", v)
    )
    spend_cap_usd: float = property(  # type: ignore[assignment]
        lambda self: self.get("spend_cap_usd"), lambda self, v: self.set("spend_cap_usd", v)
    )
    review_delay_s: int = property(  # type: ignore[assignment]
        lambda self: self.get("review_delay_s"), lambda self, v: self.set("review_delay_s", v)
    )
    approval_required_until: date | None = property(  # type: ignore[assignment]
        lambda self: self.get("approval_required_until"),
        lambda self, v: self.set("approval_required_until", v),
    )

    @property
    def print_rotation(self) -> tuple[str, ...]:
        return PRINT_ROTATION

    # -- helpers --------------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def set_quiet_hours(self, start: str | time, end: str | time) -> QuietHours:
        return self.set("quiet_hours", (start, end))

    def set_location(self, name: str, lat: float, lon: float) -> Location:
        return self.set("location", {"name": name, "lat": lat, "lon": lon})

    def local(self, dt: datetime | None = None) -> datetime:
        from .db import to_utc

        return to_utc(dt or self._clock()).astimezone(self.tz)

    def is_quiet(self, dt: datetime | None = None) -> bool:
        """True during quiet hours. Naive datetimes are treated as UTC."""
        return self.quiet_hours.contains(self.local(dt).time())

    def box_day(self, dt: datetime | None = None) -> date:
        """The 'box day': local date, rolling over at day_start rather than midnight."""
        local = self.local(dt)
        d = local.date()
        if local.time().replace(tzinfo=None) < self.day_start:
            d -= timedelta(days=1)
        return d

    def approval_required(self, day: date | None = None) -> bool:
        """SPEC 7.1: manual approval while day < approval_required_until."""
        until = self.approval_required_until
        if until is None:
            return False
        return (day or self.box_day()) < until

    def init_defaults(self) -> None:
        """First-run seeding: start the one-month manual-approval period."""
        if not self.is_set("approval_required_until"):
            self.set(
                "approval_required_until",
                self.box_day() + timedelta(days=APPROVAL_PERIOD_DAYS),
            )
