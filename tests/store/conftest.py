from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

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


@pytest.fixture
def clock():
    c = FakeClock(datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc))
    c.set_local(2026, 3, 10, 9, 0)  # Tue 09:00 New York
    return c


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "data", clock=clock)
    yield s
    s.close()
