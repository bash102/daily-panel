from datetime import date, time, timedelta

import pytest

from zozobox.store import PRINT_ROTATION, Location, QuietHours, SettingsError, Store

from zoneinfo import ZoneInfo
from datetime import datetime

NY = ZoneInfo("America/New_York")


def local(*a):
    return datetime(*a, tzinfo=NY)


def test_defaults(store):
    s = store.settings
    assert s.timezone == "America/New_York"
    assert s.day_start == time(6, 0)
    assert s.rec_cap_s == 30
    assert s.spend_cap_usd == 30
    assert s.review_delay_s == 3600
    assert 0 <= s.volume <= 1
    assert s.location is None
    assert isinstance(s.quiet_hours, QuietHours)
    assert s.print_rotation == PRINT_ROTATION == ("letter", "weather", "mood", "coloring")


def test_first_run_starts_one_month_approval(store, clock):
    until = store.settings.approval_required_until
    assert until == date(2026, 3, 10) + timedelta(days=30)
    assert store.settings.approval_required(date(2026, 3, 10))
    assert store.settings.approval_required(until - timedelta(days=1))
    assert not store.settings.approval_required(until)


def test_approval_seed_not_reset_on_reopen(tmp_path, clock):
    with Store(tmp_path, clock=clock) as s:
        first = s.settings.approval_required_until
    clock.advance(days=10)
    with Store(tmp_path, clock=clock) as s:
        assert s.settings.approval_required_until == first
        s.settings.approval_required_until = None
    with Store(tmp_path, clock=clock) as s:
        assert s.settings.approval_required_until is None
        assert not s.settings.approval_required()


def test_persisted_across_reopen(tmp_path, clock):
    with Store(tmp_path, clock=clock) as s:
        s.settings.set_quiet_hours("20:30", "06:15")
        s.settings.set_location("Home", 40.7, -74.0)
        s.settings.rec_cap_s = 120
        s.settings.timezone = "Europe/London"
        s.settings.day_start = "05:00"
    with Store(tmp_path, clock=clock) as s:
        assert s.settings.quiet_hours == QuietHours(time(20, 30), time(6, 15))
        assert s.settings.location == Location("Home", 40.7, -74.0)
        assert s.settings.rec_cap_s == 120
        assert s.settings.timezone == "Europe/London"
        assert s.settings.day_start == time(5, 0)


@pytest.mark.parametrize(
    "key,value",
    [
        ("rec_cap_s", 0),
        ("rec_cap_s", 301),
        ("rec_cap_s", "30"),
        ("rec_cap_s", True),
        ("rec_cap_s", float("nan")),
        ("volume", -0.1),
        ("volume", 1.01),
        ("spend_cap_usd", -1),
        ("review_delay_s", -5),
        ("review_delay_s", 1.5),
        ("timezone", "Mars/Olympus"),
        ("timezone", ""),
        ("day_start", "6am"),
        ("day_start", "24:00"),
        ("quiet_hours", ("19:00",)),
        ("quiet_hours", {"start": "19:00", "end": "7"}),
        ("location", {"name": "", "lat": 0, "lon": 0}),
        ("location", {"name": "x", "lat": 91, "lon": 0}),
        ("location", {"name": "x", "lat": 0, "lon": 181}),
        ("approval_required_until", "next month"),
        ("nope", 1),
    ],
)
def test_validation_rejects(store, key, value):
    before = store.settings.as_dict()
    with pytest.raises(SettingsError):
        store.settings.set(key, value)
    assert store.settings.as_dict() == before


def test_rec_cap_bounds_accepted(store):
    store.settings.rec_cap_s = 1
    assert store.settings.rec_cap_s == 1
    store.settings.rec_cap_s = 300
    assert store.settings.rec_cap_s == 300


def test_update_is_all_or_nothing(store):
    with pytest.raises(SettingsError):
        store.settings.update({"volume": 0.2, "rec_cap_s": 999})
    assert store.settings.volume != 0.2


def test_as_dict_is_json_ready(store):
    import json

    store.settings.set_location("Park", 1.5, 2.5)
    d = store.settings.as_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["location"] == {"name": "Park", "lat": 1.5, "lon": 2.5}
    assert d["quiet_hours"] == {"start": "19:00", "end": "07:00"}


def test_corrupt_stored_value_falls_back_to_default(store):
    with store.db.tx() as c:
        c.execute("INSERT INTO settings VALUES ('volume', '\"loud\"', 'x')")
    assert store.settings.volume == 0.7


def test_reset(store):
    store.settings.volume = 0.1
    store.settings.reset("volume")
    assert store.settings.volume == 0.7


def test_quiet_hours_spanning_midnight(store):
    store.settings.set_quiet_hours("19:00", "07:00")
    assert store.settings.is_quiet(local(2026, 3, 10, 19, 0))
    assert store.settings.is_quiet(local(2026, 3, 10, 23, 59))
    assert store.settings.is_quiet(local(2026, 3, 11, 0, 0))
    assert store.settings.is_quiet(local(2026, 3, 11, 6, 59))
    assert not store.settings.is_quiet(local(2026, 3, 11, 7, 0))
    assert not store.settings.is_quiet(local(2026, 3, 10, 18, 59))


def test_quiet_hours_same_day_and_disabled(store):
    store.settings.set_quiet_hours("13:00", "15:00")  # nap time
    assert store.settings.is_quiet(local(2026, 3, 10, 14, 0))
    assert not store.settings.is_quiet(local(2026, 3, 10, 15, 0))
    assert not store.settings.is_quiet(local(2026, 3, 10, 12, 59))
    store.settings.set_quiet_hours("00:00", "00:00")
    assert not store.settings.is_quiet(local(2026, 3, 10, 3, 0))


def test_is_quiet_uses_clock_and_timezone(store, clock):
    clock.set_local(2026, 3, 10, 20, 0)
    assert store.is_quiet()
    # 20:00 New York is 01:00 London: still quiet; 12:00 NY is 16:00 London: not.
    store.settings.timezone = "Europe/London"
    assert store.is_quiet()
    clock.set_local(2026, 3, 10, 12, 0)
    assert not store.is_quiet()


def test_box_day_rolls_at_day_start(store):
    assert store.box_day(local(2026, 3, 10, 5, 59)) == date(2026, 3, 9)
    assert store.box_day(local(2026, 3, 10, 6, 0)) == date(2026, 3, 10)
    assert store.box_day(local(2026, 3, 10, 23, 59)) == date(2026, 3, 10)
    assert store.box_day(local(2026, 3, 11, 1, 0)) == date(2026, 3, 10)
    store.settings.day_start = "00:00"
    assert store.box_day(local(2026, 3, 11, 1, 0)) == date(2026, 3, 11)


def test_box_day_across_dst(store):
    # 2026-03-08 US DST starts at 02:00. 06:00 local is still the boundary.
    assert store.box_day(local(2026, 3, 8, 5, 30)) == date(2026, 3, 7)
    assert store.box_day(local(2026, 3, 8, 6, 30)) == date(2026, 3, 8)
