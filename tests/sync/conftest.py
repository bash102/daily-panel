from datetime import datetime, timedelta, timezone

import pytest

from zozobox.store import Store
from zozobox.sync import SyncClient
from zozobox.sync.fake import FakeCloud, fake_normalizer

TOKEN = "box-token-for-tests"  # keep in sync with the test modules


class FakeClock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> datetime:
        self.t += timedelta(**kw)
        return self.t


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc))  # 10:00 New York


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "data", clock=clock, recover_outbox=True)
    yield s
    s.close()


@pytest.fixture
def cloud():
    return FakeCloud(token=TOKEN)


@pytest.fixture
def make_client(store, cloud, clock):
    def make(**kw):
        kw.setdefault("transport", cloud)
        kw.setdefault("normalizer", fake_normalizer())
        return SyncClient(store, cloud.base_url, TOKEN, clock, **kw)
    return make


@pytest.fixture
def client(make_client):
    return make_client()
