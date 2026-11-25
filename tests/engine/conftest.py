"""Test harness for the engine: real Store, fake audio/printer/LEDs/classifier,
and one fake clock shared by the store (datetime) and the engine (seconds)."""

from __future__ import annotations

import io
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

from zozobox.audio import FakeAudioIn, FakeAudioOut, FakeTake, Output
from zozobox.buttons import ParentChord, Press
from zozobox.config import BoxConfig, LedConfig, Role
from zozobox.engine import (
    Engine,
    EngineSettings,
    FakeMoodClassifier,
    FakePhraseBook,
    FakeSoundboard,
    PauseStep,
)
from zozobox.leds import FakeLedBackend, LedController
from zozobox.printer import FakePrinter
from zozobox.store import Kind, Slot, Store

NY = ZoneInfo("America/New_York")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 3, 10, 10, 0, tzinfo=NY).astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def mono(self) -> float:
        return self.t.timestamp()

    def advance(self, seconds: float = 0, **kw) -> None:
        self.t += timedelta(seconds=seconds, **kw)

    def set_local(self, *args) -> None:
        self.t = datetime(*args, tzinfo=NY).astimezone(timezone.utc)


def png_bytes(size=(200, 200)) -> bytes:
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    d.ellipse((20, 20, size[0] - 20, size[1] - 20), outline=0, width=8)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class Box:
    def __init__(self, tmp: Path, *, takes=None, results=(), missing_phrases=(), n_clips=6,
                 settings: EngineSettings | None = None, on_parent_chord=None):
        self.tmp = tmp
        self.clock = Clock()
        self.store = Store(tmp / "data", clock=self.clock)
        self.audio = FakeAudioOut()
        self.audio_in = FakeAudioIn(list(takes or []), muter=self.audio)
        self.printer = FakePrinter()
        self.led_backend = FakeLedBackend()
        self.leds = LedController(LedConfig(), self.led_backend, clock=self.clock.mono, start_thread=False)
        self.phrases = FakePhraseBook(tmp / "phrases", missing=missing_phrases)
        self.classifier = FakeMoodClassifier(results)
        self.clips = []
        (tmp / "clips").mkdir(exist_ok=True)
        for i in range(n_clips):
            p = tmp / "clips" / f"clip{i}.wav"
            p.write_bytes(b"RIFF")
            self.clips.append(p)
        self.soundboard = FakeSoundboard(self.clips)
        (tmp / "click.wav").write_bytes(b"RIFF")
        (tmp / "beep.wav").write_bytes(b"RIFF")
        self.settings = settings or EngineSettings(click=tmp / "click.wav", beep=tmp / "beep.wav")
        self.chords = []
        self.cfg = BoxConfig()
        self.btn = {role: btn for btn, role in self.cfg.roles.items()}
        self.eng = Engine(
            self.cfg, self.store, self.audio, self.audio_in, self.printer, self.leds,
            self.phrases, self.classifier, self.soundboard, clock=self.clock.mono,
            rng=random.Random(1), settings=self.settings,
            on_parent_chord=on_parent_chord or self.chords.append,
        )

    # -- input ------------------------------------------------------------
    def press(self, role: Role) -> None:
        self.eng.handle(Press(self.btn[role], self.clock.mono()))

    def chord(self) -> None:
        self.eng.handle(ParentChord(self.clock.mono()))

    def refresh(self) -> None:
        """Let the periodic LED re-read happen (content may have arrived)."""
        self.clock.advance(self.settings.led_refresh_s)
        self.tick()

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.eng.tick()

    # -- audio views ------------------------------------------------------
    def is_click(self, p) -> bool:
        return p.path.name == "click.wav"

    def sounds(self) -> list[str]:
        """Stems of everything played except clicks, in order."""
        return [p.path.stem for p in self.audio.plays if not self.is_click(p)]

    def clicks(self) -> int:
        return sum(1 for p in self.audio.plays if self.is_click(p))

    def playing(self) -> list[str]:
        return [p.path.stem for p in self.audio.playing if not self.is_click(p)]

    def finish_audio(self) -> None:
        for p in list(self.audio.plays):
            p.proc.finish()
        self.tick()

    def wait(self, cond, timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                raise AssertionError(f"timed out; current={self.eng.current} sounds={self.sounds()}")
            self.tick()
            time.sleep(0.002)

    def step_through(self, until=None, timeout: float = 5.0) -> None:
        """Let the running experience play out: finish sounds, skip pauses,
        wait for workers. Stops when `until()` is true or the engine is idle."""
        end = time.monotonic() + timeout
        until = until or (lambda: not self.eng.busy)
        while not until():
            if time.monotonic() > end:
                raise AssertionError(f"timed out; current={self.eng.current} sounds={self.sounds()}")
            cur = self.eng.current
            if cur is not None and isinstance(cur.step, PauseStep):
                self.clock.advance(cur.step.seconds)
            for p in list(self.audio.plays):
                if not p.playback.done:
                    p.proc.finish()
            self.tick()
            time.sleep(0.001)

    def until_playing(self, stem: str) -> None:
        self.wait(lambda: stem in self.playing())

    # -- store helpers ----------------------------------------------------
    def media(self, rel: str, data: bytes = b"RIFF") -> str:
        self.store.write_media(rel, data)
        return rel

    def add(self, kind, media=None, meta=None, **kw):
        return self.store.content.add(kind, media=media or {}, meta=meta or {}, safety="passed",
                                      approval="approved", **kw)

    def add_letter(self, letter="B", words=("ball", "bear", "bus"), drawing=True, tag="a"):
        media = {"intro": self.media(f"letter/{tag}/intro.wav")}
        for i, _ in enumerate(words):
            media[f"word_{i}"] = self.media(f"letter/{tag}/word_{i}.wav")
        if drawing:
            media["drawing"] = self.media(f"letter/{tag}/drawing.png", png_bytes())
        return self.add(Kind.LETTER, media, {"letter": letter, "words": list(words)})

    def add_weather(self):
        return self.add(Kind.WEATHER, {"audio": self.media("weather/summary.wav")},
                        {"summary": "Sunny and cold", "icon": "sunny", "hi_c": 8, "lo_c": -2})

    def add_coloring(self):
        return self.add(Kind.COLORING, {"art": self.media("coloring/art.png", png_bytes()),
                                        "intro": self.media("coloring/intro.wav")},
                        {"title": "A happy whale"})

    def add_story(self, n=3, tag="s"):
        media = {f"chunk_{i}": self.media(f"story/{tag}/chunk_{i}.wav") for i in range(n)}
        return self.add(Kind.STORY, media, {"title": "Zozo and the moon"})

    def add_all_content(self):
        self.add_letter()
        self.add_weather()
        self.add_coloring()
        self.add_story()

    def add_grandma(self, name="g1"):
        rel = self.media(f"grandma/{name}.wav")
        return self.store.recordings.put(Slot.GRANDMA, rel, duration_s=3.0)

    def events(self, type_):
        return self.store.events.query(type_, limit=None)

    def led(self, role: Role):
        return self.leds.get(self.btn[role])

    def close(self):
        self.eng.close()
        self.store.close()


@pytest.fixture
def make_box(tmp_path):
    boxes = []

    def make(**kw):
        sub = tmp_path / f"box{len(boxes)}"
        sub.mkdir()
        b = Box(sub, **kw)
        boxes.append(b)
        return b

    yield make
    for b in boxes:
        b.close()


@pytest.fixture
def box(make_box):
    return make_box()


__all__ = ["Box", "Clock", "FakeTake", "Output", "png_bytes"]
