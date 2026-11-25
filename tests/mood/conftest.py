import socket
import struct
import wave
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


@pytest.fixture(autouse=True)
def no_network_no_keys(monkeypatch):
    """Tests must never reach a real API: block sockets and drop real keys."""
    def refuse(*a, **k):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
              "ZOZOBOX_STT_MODEL", "ZOZOBOX_CLAUDE_MODEL", "ZOZOBOX_DATA_DIR",
              "ANTHROPIC_PROFILE", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


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


def write_wav(path, seconds=1.0, rate=16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(seconds * rate))
    return path


@pytest.fixture
def make_wav():
    return write_wav


@pytest.fixture
def wav(tmp_path):
    return write_wav(tmp_path / "clip.wav", 2.0)
