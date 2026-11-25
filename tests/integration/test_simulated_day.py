"""A whole box day, end to end, with fake hardware and fake cloud clients:

nightly content (bootstrap + nightly) -> parent approves -> Zozo presses
through the Print rotation, Story, Grandma (records a reply) and the
Soundboard -> prints archived -> the sync client drains the outbox to a fake
worker over real localhost HTTP (docs/SYNC.md) -> grandma calls in a
message -> it is imported -> the Grandma LED is bright.

Production paths on the way: prints go through LockedPrinter +
SupervisedPrinter (the cross-process lock the dashboard shares), the speaker
is muted for every recording, and the sync side opens its own
Store(recover_outbox=True) like the separate sync process. A second test
drives presses through FakeButtonBackend -> ButtonInput thread -> events
queue -> Engine thread, as `python -m zozobox.app` does.
"""

from __future__ import annotations

import dataclasses
import random
import threading
import time

from zozobox.app import App, Backends
from zozobox.audio import FakeAudioIn, FakeAudioOut, FakeTake
from zozobox.boxconfig import AppConfig
from zozobox.buttons import FakeButtonBackend, Press
from zozobox.config import Role
from zozobox.content import Clients, FakeClaude, FakeMedia, FakeWeather, run_bootstrap, run_nightly
from zozobox.content.soundboard import STARTER_CLIPS
from zozobox.engine import FakeMoodClassifier, MoodResult
from zozobox.leds import FakeLedBackend, LedState
from zozobox.printer import FakePrinter, LockedPrinter, SupervisedPrinter, lock_path
from zozobox.store import Kind, Slot, Store
from zozobox.sync import SyncClient
from zozobox.sync.fake import fake_normalizer

from .conftest import Clock, copy_normalizer, step_through

GRANDMA_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24


def test_simulated_day(tmp_path, worker):
    clock = Clock(2026, 3, 10, 2, 30)  # the nightly timer's slot
    data = tmp_path / "data"
    store = Store(data, clock=clock)
    store.settings.set_location("Home", 40.7, -74.0)
    cfg = AppConfig(data_dir=data)

    # -- 02:30 nightly content: bootstrap (first run) then the nightly jobs ----
    media = FakeMedia()
    clients = Clients(FakeClaude(), media, FakeWeather(), copy_normalizer)
    boot = run_bootstrap(store, clients, clock, soundboard=True)
    assert set(boot["phrases"].values()) == {"generated"}
    assert set(boot["soundboard"].values()) == {"generated"}
    nightly = run_nightly(store, clients, clock)
    assert nightly == {"weather": "generated", "story": "generated", "letter": "generated",
                       "coloring": "generated"}

    # -- the parent approves everything on the dashboard (first month) -------
    pending = store.content.pending_approval()
    assert {it.kind for it in pending} >= {"phrase", "story", "letter", "coloring", "weather",
                                           "soundboard"}
    for it in pending:
        if it.kind is Kind.SOUNDBOARD and it.meta["key"] != "cow":
            store.content.reject(it.id)  # only one starter clip approved
        else:
            store.content.approve(it.id)

    # -- 10:00, the engine process wakes up (fake hardware) ---------------------
    clock.set_local(2026, 3, 10, 10, 0)
    audio = FakeAudioOut()
    takes = [FakeTake(duration_s=2.0),   # mood answer
             FakeTake(duration_s=4.0)]   # grandma reply
    audio_in = FakeAudioIn(takes, muter=audio)
    printer = FakePrinter(tmp_path / "prints")
    # The engine prints through the same lock + supervisor as in production.
    locked = LockedPrinter(SupervisedPrinter(lambda: printer, name="fake QL-800"),
                           lock_path(data), timeout_s=5)
    leds = FakeLedBackend()
    buttons = FakeButtonBackend(cfg.box.buttons.pins)
    backends = Backends(buttons=buttons, leds=leds, audio=(audio, audio_in), printer=locked,
                        classifier=FakeMoodClassifier([MoodResult("happy", "happy!")]))
    app = App(cfg, backends=backends, store=store, clock=clock.mono, start_threads=False)
    app.build()
    eng = app.engine
    eng.rng = random.Random(7)
    btn = {role: b for b, role in cfg.box.roles.items()}

    def press(role):
        clock.advance(2)  # beyond the 1 s lockout
        eng.handle(Press(btn[role], clock.mono()))
        step_through(eng, audio, clock)
        clock.advance(1)
        eng.tick()

    def led(role):
        return app.leds.get(btn[role])

    try:
        assert led(Role.PRINT) is LedState.BRIGHT  # four cards waiting
        assert led(Role.STORY) is LedState.BRIGHT
        assert led(Role.GRANDMA) is LedState.DIM  # no message yet

        # Print rotation: letter -> weather -> mood -> coloring, one card each.
        for _ in range(4):
            press(Role.PRINT)
        assert printer.count == 4
        prints = store.events.query("print", limit=None)
        assert [e.payload["item"] for e in sorted(prints, key=lambda e: e.ts)] == \
            ["letter", "weather", "mood", "coloring"]
        assert lock_path(data).exists()  # the shared cross-process printer lock
        moods = store.events.query("mood", limit=None)
        assert [m.payload["label"] for m in moods] == ["happy"]
        assert led(Role.PRINT) is LedState.DIM
        press(Role.PRINT)  # a fifth press replays audio, no fifth card
        assert printer.count == 4

        # Story: plays all chunks, then it's heard.
        press(Role.STORY)
        assert store.daily.story_heard
        assert led(Role.STORY) is LedState.DIM

        # Grandma: no message yet, so "say hi", beep, record the reply.
        press(Role.GRANDMA)
        zozo = store.recordings.live(Slot.ZOZO)
        assert zozo is not None and zozo.duration_s == 4.0
        # SPEC: the box speaker is muted while recording (mood take + reply).
        assert audio_in.muted_during == [True, True]
        assert not audio.speaker_is_muted

        # Soundboard: the one approved clip of the TTS starter pack (SPEC 7.1:
        # unapproved generated items are never served in the first month).
        for _ in range(3):
            press(Role.SOUNDBOARD)
        sb_played = [p for p in audio.plays if p.path.parent.name == "soundboard"]
        assert len(sb_played) == 3
        assert {p.path.name.split("-")[0] for p in sb_played} == {"cow"}
        assert "cow" in STARTER_CLIPS

        # Every print is archived locally with a PNG copy and queued.
        for e in prints:
            assert e.media and store.media_exists(e.media)
        assert store.outbox.stats().pending > 0

        # -- the sync process drains the outbox to the worker -----------------
        # The sync process has its own store connection and owns outbox recovery.
        sync_store = Store(data, clock=clock, recover_outbox=True)
        sync = SyncClient(sync_store, worker.base_url, "box-token", normalizer=fake_normalizer())
        for _ in range(20):
            rep = sync.run_once()
            assert rep.online, rep.errors
            if store.outbox.stats().pending == 0:
                break
            clock.advance(60)  # past any backoff
        assert store.outbox.stats().pending == 0
        cloud = worker.cloud
        keys = set(cloud.objects)
        assert sum(k.startswith("prints/") for k in keys) == 4
        assert zozo.media in keys
        assert any(k.startswith("mood/") for k in keys)
        assert f"control/zozo/{zozo.id}.json" in keys
        assert cloud.zozo[zozo.id].media_key == zozo.media
        assert cloud.config is not None and "quiet_hours" in cloud.config

        # -- grandma calls in; the worker archives her message -----------------
        with worker.lock:
            cloud.add_grandma("RE0123456789abcdef", GRANDMA_WAV, clock.t, 6.0)
            cloud.publish(zozo.id, clock.t.isoformat().replace("+00:00", "Z"))
        rep = sync.run_once()
        assert rep.imported == ["RE0123456789abcdef"], rep.errors
        assert rep.published == [zozo.id]
        g = store.recordings.live(Slot.GRANDMA)
        assert g is not None and store.media_path(g.media).read_bytes().startswith(b"NORM:")

        # The engine notices on its periodic LED refresh: grandma is bright.
        clock.advance(eng.settings.led_refresh_s + 1)
        eng.tick()
        assert led(Role.GRANDMA) is LedState.BRIGHT

        # Zozo listens: it plays, and the LED goes back to dim.
        press(Role.GRANDMA)
        assert any(p.path.name.startswith("RE0123456789abcdef") for p in audio.plays)
        assert led(Role.GRANDMA) is LedState.DIM
    finally:
        app.close()
        if "sync_store" in locals():
            sync_store.close()
        store.close()


def test_presses_flow_through_the_button_and_engine_threads(tmp_path):
    """FakeButtonBackend edges -> ButtonInput thread (debounce, lockout) ->
    events queue -> Engine thread: the path a real press takes."""
    cfg = AppConfig(data_dir=tmp_path / "d")
    audio = FakeAudioOut(auto_finish=True)
    buttons = FakeButtonBackend(cfg.box.buttons.pins)
    backends = Backends(buttons=buttons, leds=FakeLedBackend(),
                        audio=(audio, FakeAudioIn(muter=audio)), printer=FakePrinter(),
                        classifier=FakeMoodClassifier())
    app = App(cfg, backends=backends).build()
    stop = threading.Event()
    runner = threading.Thread(target=app.run, args=(stop,), kwargs={"poll_s": 0.05})
    try:
        app.store.settings.set_quiet_hours("00:00", "00:00")  # never quiet
        sb_dir = cfg.soundboard_dir / "imported"
        sb_dir.mkdir(parents=True, exist_ok=True)
        clip = sb_dir / "moo.wav"
        clip.write_bytes(b"RIFF")
        runner.start()
        btn = next(b for b, r in cfg.box.roles.items() if r is Role.SOUNDBOARD)
        buttons.set(btn, True)
        time.sleep(0.15)  # held past the debounce
        buttons.set(btn, False)
        end = time.monotonic() + 5
        while time.monotonic() < end and not any(p.path == clip for p in audio.plays):
            time.sleep(0.02)
        assert any(p.path == clip for p in audio.plays), [p.path for p in audio.plays]
        assert any(p.path == app.engine.settings.click for p in audio.plays)
        assert app.engine._thread is not None and app.engine._thread.is_alive()
    finally:
        stop.set()
        if runner.is_alive():
            runner.join(5)
        app.close()


def test_soundboard_dir_and_bundled_chimes_are_used(tmp_path):
    """The app wires the configured soundboard dir and generated chimes."""
    cfg = dataclasses.replace(AppConfig(data_dir=tmp_path / "d"))
    app = App(cfg, fake=True, start_threads=False).build()
    try:
        assert app.engine.soundboard.dir.directory == cfg.soundboard_dir
        app.store.settings.set_quiet_hours("00:00", "00:00")  # never quiet
        click = app.engine.settings.click
        app.engine.handle(Press(next(iter(cfg.box.roles)), app.clock()))
        assert any(p.path == click for p in app.audio_out.plays)
    finally:
        app.close()
