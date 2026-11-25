"""Dashboard test helpers: a real Store in tmp, FakePrinter, one fake clock
shared by the store (datetime) and the dashboard (Unix seconds), and a
TestClient helper that knows how to set up, log in and post with CSRF."""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from zozobox.dashboard import DashboardConfig, create_app
from zozobox.printer import WIDTH_PX, FakePrinter
from zozobox.store import Kind, Slot, Store

NY = ZoneInfo("America/New_York")
PASSWORD = "correct horse battery"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class Clock:
    def __init__(self):
        self.t = datetime(2026, 3, 10, 10, 0, tzinfo=NY).astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def epoch(self) -> float:
        return self.t.timestamp()

    def advance(self, seconds: float = 0, **kw) -> None:
        self.t += timedelta(seconds=seconds, **kw)

    def set_local(self, *args) -> None:
        self.t = datetime(*args, tzinfo=NY).astimezone(timezone.utc)


def png_bytes(size=(WIDTH_PX, 300)) -> bytes:
    img = Image.new("L", size, 255)
    ImageDraw.Draw(img).ellipse((20, 20, size[0] - 20, size[1] - 20), outline=0, width=8)
    buf = io.BytesIO()
    img.convert("1").save(buf, format="PNG")
    return buf.getvalue()


WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24


class Client:
    """TestClient wrapper: csrf() fetches a page and extracts its token."""

    def __init__(self, app):
        self.c = TestClient(app, follow_redirects=False)

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def csrf(self, page: str = "/settings") -> str:
        r = self.c.get(page, follow_redirects=True)
        m = CSRF_RE.search(r.text)
        assert m, f"no csrf token on {page}: {r.status_code} {r.text[:300]}"
        return m.group(1)

    def post(self, url, data=None, *, page: str = "/settings", token: str | None = None, **kw):
        data = dict(data or {})
        if "csrf_token" not in data:
            data["csrf_token"] = token if token is not None else self.csrf(page)
        return self.c.post(url, data=data, **kw)

    def setup(self, password: str = PASSWORD):
        return self.post("/setup", {"password": password, "confirm": password}, page="/setup")

    def login(self, password: str = PASSWORD, next: str = "/"):
        return self.post("/login", {"password": password, "next": next}, page="/login")

    @property
    def cookies(self):
        return self.c.cookies


class Dash:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.clock = Clock()
        self.store = Store(tmp / "data", clock=self.clock)
        self.printer = FakePrinter()
        self.config = DashboardConfig(scrypt_n=2**10)
        self.app = create_app(self.store, self.printer, config=self.config, clock=self.clock.epoch)

    def client(self) -> Client:
        return Client(self.app)

    def authed_client(self) -> Client:
        c = self.client()
        if not self.app.state.passwords.exists():
            r = c.setup()
        else:
            r = c.login()
        assert r.status_code == 303, r.text[:500]
        return c

    # -- population helpers -------------------------------------------------------------
    def media(self, rel: str, data: bytes) -> str:
        self.store.write_media(rel, data)
        return rel

    def populate(self) -> dict:
        s = self.store
        out: dict = {}
        story_audio = self.media("content/story/2026/03/10/a/chunk_0.wav", WAV)
        out["story"] = s.content.add(
            Kind.STORY, media={"chunk_0": story_audio},
            meta={"title": "Zozo and the Kind Cloud", "text": "Once upon a time Zozo shared."},
            safety="passed", approval="pending")
        art = self.media("content/coloring/2026/03/10/b/art.png", png_bytes((400, 400)))
        out["coloring"] = s.content.add(Kind.COLORING, media={"art": art},
                                        meta={"title": "A happy whale"}, safety="passed",
                                        approval="pending")
        out["letter"] = s.content.add(Kind.LETTER, meta={"letter": "B", "words": ["ball", "bear"]},
                                      safety="passed", approval="approved")
        out["weather"] = s.content.add(Kind.WEATHER, meta={"summary": "Sunny and cold", "icon":
                                                          "sunny", "hi_c": 5, "lo_c": -2},
                                       safety="passed", approval="auto")
        out["failed"] = s.content.add(Kind.STORY, meta={"title": "Scary one"}, safety="failed",
                                      approval="pending")
        s.content.set_safety(out["failed"].id, "failed", "too scary")
        s.events.append("safety_failed", {"kind": "story", "stage": "text", "attempt": 1,
                                          "reasons": ["too scary"], "item_id": out["failed"].id})
        # prints
        out["print"] = s.events.record_print("letter", png_bytes(), extra={"letter": "B"})
        s.daily.mark_printed("letter")
        # recordings
        zrel = self.media("recordings/zozo/2026/03/10/100000000-abcd1234.wav", WAV)
        out["zozo"] = s.recordings.put(Slot.ZOZO, zrel, duration_s=4.5)
        grel = self.media("recordings/grandma/2026/03/09/g1.wav", WAV)
        out["grandma"] = s.recordings.put(Slot.GRANDMA, grel, duration_s=12.0,
                                          created_at=self.clock() - timedelta(hours=20))
        # moods
        mrel = self.media("mood/2026/03/10/100500000-mood0001.wav", WAV)
        s.events.record_mood("unknown", transcript="blah", audio=mrel, extra={"attempt": 1})
        mrel2 = self.media("mood/2026/03/10/100600000-mood0002.wav", WAV)
        s.events.record_mood("happy", transcript="I'm happy!", audio=mrel2, extra={"attempt": 2})
        prel = self.media("mood/2026/03/10/110000000-mood0003.wav", WAV)
        s.events.append("mood_pending", {"attempt": 1, "audio": prel}, media=prel)
        # presses
        for role, button in (("print", "red"), ("print", "red"), ("story", "blue"),
                             ("grandma", "green")):
            s.events.append("press", {"button": button, "role": role, "action": "start"})
        s.events.append("press", {"button": "red", "role": "print", "action": "ignored"})
        s.daily.mark_story_heard()
        # spend
        s.events.append("spend", {"provider": "anthropic", "model": "claude-opus-5-5",
                                  "purpose": "story", "usd": 1.25})
        s.events.append("spend", {"provider": "openai", "model": "gpt-image-2",
                                  "purpose": "coloring", "usd": 0.75})
        # last month's spend must not count
        s.events.append("spend", {"provider": "openai", "usd": 99.0},
                        ts=self.clock() - timedelta(days=40))
        return out


