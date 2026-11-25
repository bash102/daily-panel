from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from zozobox.content import Clients, FakeClaude, FakeMedia, FakeWeather, Pipeline
from zozobox.store import Store

NY = ZoneInfo("America/New_York")


class FakeClock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> datetime:
        self.t += timedelta(**kw)
        return self.t

    def set_local(self, *args, tz=NY) -> datetime:
        self.t = datetime(*args, tzinfo=tz).astimezone(timezone.utc)
        return self.t


def copy_normalizer(src: Path, dst: Path) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return {}


@pytest.fixture
def clock():
    c = FakeClock(datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc))
    c.set_local(2026, 3, 10, 2, 0)  # 02:00 New York: a typical nightly-run time
    return c


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "data", clock=clock)
    s.settings.set_location("Home", 40.7, -74.0)
    yield s
    s.close()


@pytest.fixture
def normalizer():
    return copy_normalizer


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def media():
    return FakeMedia()


@pytest.fixture
def weather():
    return FakeWeather()


@pytest.fixture
def clients(claude, media, weather):
    return Clients(claude=claude, media=media, weather=weather, normalizer=copy_normalizer)


@pytest.fixture
def pipeline(store, clients, clock):
    return Pipeline(store, clients, clock)
