"""Slap storms, failures, parent chord, volume, the run loop, and the
phrase/soundboard sources."""

import queue
import random
import threading
import time

from zozobox.audio import FakeTake, Output
from zozobox.buttons import Press
from zozobox.config import Btn, Role
from zozobox.engine import (
    DirPhraseBook,
    DirSoundboard,
    MoodResult,
    StorePhraseBook,
)
from zozobox.leds import LedState
from zozobox.store import Kind, Slot


def test_slap_storm_never_crashes(make_box):
    box = make_box(takes=[FakeTake(duration_s=1.0)] * 50,
                   results=[MoodResult(random.Random(i).choice(["happy", "unknown", "sad"])) for i in range(50)])
    box.add_all_content()
    box.add_grandma()
    rng = random.Random(42)
    roles = list(Role)
    for i in range(400):
        box.press(rng.choice(roles))
        for _ in range(rng.randint(0, 3)):
            if rng.random() < 0.5:
                for p in list(box.audio.plays):
                    p.proc.finish()
            box.clock.advance(rng.random() * 2)
            box.tick()
        # never more than one non-click sound at a time
        assert len(box.playing()) <= 1, box.playing()
    box.step_through(timeout=10)
    assert not box.eng.busy
    assert box.events("error") == [] or all(
        e.payload["kind"] in ("print_failed",) for e in box.events("error"))
    assert box.printer.count <= 4
    assert len(box.events("press")) == 400
    # the box still works normally afterwards
    box.press(Role.SOUNDBOARD)
    assert box.eng.current.name == "soundboard"


def test_alternating_presses_switch_every_time(box):
    box.add_story()
    for i in range(20):
        box.press(Role.STORY if i % 2 else Role.SOUNDBOARD)
        assert len(box.playing()) == 1
    assert box.clicks() == 20


def test_audio_play_exception_ends_experience_engine_survives(box):
    box.add_story()
    real_play = box.audio.play
    calls = {"n": 0}

    def flaky(path, output=Output.BOTH):
        if output is Output.BOTH:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("device unplugged")
        return real_play(path, output)

    box.audio.play = flaky
    box.press(Role.STORY)
    assert not box.eng.busy
    kinds = [e.payload["kind"] for e in box.events("error")]
    assert "experience_failed" in kinds
    box.press(Role.SOUNDBOARD)
    assert box.eng.current.name == "soundboard"
    assert box.playing()


def test_failed_playback_is_logged_and_sequence_continues(box):
    box.add_story(2)
    box.press(Role.STORY)
    box.audio.plays[-1].proc.finish(1)  # aplay exited with an error
    box.tick()
    assert box.playing() == ["chunk_1"]
    assert "play_failed" in [e.payload["kind"] for e in box.events("error")]


def test_missing_phrases_are_skipped(make_box):
    box = make_box(missing_phrases=["say_hi_grandma", "here_is_your_card", "praise_great_job",
                                    "praise_well_done", "praise_you_did_it"])
    box.add_letter()
    box.press(Role.PRINT)
    box.step_through()
    assert "here_is_your_card" not in box.sounds()
    assert box.printer.count == 1
    box.press(Role.GRANDMA)
    assert box.playing() == ["beep"]


def test_missing_beep_and_click_are_harmless(make_box, tmp_path):
    from zozobox.engine import EngineSettings

    box = make_box(settings=EngineSettings(click=tmp_path / "nope.wav", beep=None))
    box.press(Role.GRANDMA)
    box.step_through()
    assert box.clicks() == 0
    assert box.store.recordings.live(Slot.ZOZO) is not None


def test_builder_exception_is_contained(box):
    def broken(*a, **k):
        raise RuntimeError("db gone")

    box.store.content.current_for_today = broken
    box.press(Role.STORY)
    assert not box.eng.busy
    assert "build_failed" in [e.payload["kind"] for e in box.events("error")]
    box.press(Role.SOUNDBOARD)
    assert box.eng.busy


def test_store_event_failures_do_not_break_presses(box):
    def broken(*a, **k):
        raise RuntimeError("disk full")

    box.store.events.append = broken
    box.press(Role.SOUNDBOARD)
    assert box.eng.current.name == "soundboard"
    box.step_through()


def test_led_failure_is_contained(box):
    def broken(*a, **k):
        raise OSError("i2c")

    box.leds.set = broken
    box.press(Role.SOUNDBOARD)
    assert box.eng.busy


def test_unknown_event_and_unmapped_button(box):
    box.eng.handle("garbage")
    box.eng.handle(Press(Btn.RED, 0.0))  # mapped; fine
    box.eng.roles.pop(Btn.YELLOW)
    box.eng.handle(Press(Btn.YELLOW, 0.0))
    assert box.eng.current.name == "grandma"


def test_parent_chord_stops_everything_and_calls_back(box):
    box.add_story()
    box.press(Role.STORY)
    box.chord()
    assert not box.eng.busy
    assert box.audio.playing == []
    assert len(box.chords) == 1
    assert box.events("parent_chord")
    assert not box.store.daily.story_heard


def test_parent_chord_callback_error_is_contained(make_box):
    def cb(ev):
        raise RuntimeError("boom")

    box = make_box(on_parent_chord=cb)
    box.chord()
    box.press(Role.SOUNDBOARD)
    assert box.eng.busy


def test_volume_applied_at_start_and_on_change(box):
    assert box.audio.volume_calls == [0.7]
    box.store.settings.volume = 0.4
    box.tick()
    assert box.audio.volume_calls == [0.7]  # polled, not instant
    box.clock.advance(box.settings.state_poll_s)
    box.tick()
    assert box.audio.volume_calls == [0.7, 0.4]
    box.clock.advance(box.settings.state_poll_s)
    box.tick()
    assert box.audio.volume_calls == [0.7, 0.4]


def test_run_loop_thread_consumes_queue(box):
    events = queue.Queue()
    box.eng.start(events)
    try:
        events.put(Press(Btn.YELLOW, 0.0))
        deadline = time.monotonic() + 3
        while not box.playing() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert box.playing() and box.playing()[0].startswith("clip")
    finally:
        box.eng.close()
    assert box.eng._thread is not None and not box.eng._thread.is_alive()


def test_render_error_is_a_print_failure(box, monkeypatch):
    from zozobox.engine import builders

    def broken(*a, **k):
        raise ValueError("bad letter")

    monkeypatch.setattr(builders, "render_letter_card", broken)
    box.add_letter()
    box.press(Role.PRINT)
    box.step_through()
    assert box.printer.count == 0
    assert "printer_sleepy" in box.sounds()
    assert not box.store.daily.printed_today("letter")


def test_unreadable_coloring_art_counts_as_missing(box):
    c = box.add_coloring()
    box.store.write_media(c.media["art"], b"not a png")
    box.store.daily.advance_rotation()
    box.store.daily.advance_rotation()
    box.store.daily.advance_rotation()
    box.press(Role.PRINT)
    box.step_through()
    # art unreadable: treated as missing content up front
    assert box.printer.count == 0


def test_close_cancels_running_experience(box):
    box.press(Role.SOUNDBOARD)
    box.eng.close()
    assert not box.eng.busy
    assert box.playing() == []


# -------------------------------------------------------------- sources ---

def test_dir_phrasebook_and_store_phrasebook(box, tmp_path):
    d = tmp_path / "bundled"
    d.mkdir()
    (d / "sleepy.ogg").write_bytes(b"x")
    dp = DirPhraseBook(d)
    assert dp.path_for("sleepy") == d / "sleepy.ogg"
    assert dp.path_for("nope") is None
    rel = box.media("phrases/say_hi.wav")
    box.add(Kind.PHRASE, {"audio": rel}, {"key": "say_hi_grandma"})
    sp = StorePhraseBook(box.store, fallback=dp)
    assert sp.path_for("say_hi_grandma") == box.store.media_path(rel)
    assert sp.path_for("sleepy") == d / "sleepy.ogg"
    assert StorePhraseBook(box.store).path_for("sleepy") is None


def test_dir_soundboard(tmp_path):
    d = tmp_path / "pack"
    (d / "animals").mkdir(parents=True)
    (d / "animals" / "cow.wav").write_bytes(b"x")
    (d / "song.mp3").write_bytes(b"x")
    (d / "readme.txt").write_text("x")
    (d / ".hidden.wav").write_bytes(b"x")
    assert [p.name for p in DirSoundboard(d).clips()] == ["cow.wav", "song.mp3"]
    assert DirSoundboard(tmp_path / "missing").clips() == []


def test_grandma_message_led_cleared_by_hearing(box):
    box.add_grandma()
    box.refresh()
    assert box.led(Role.GRANDMA) is LedState.BRIGHT
    box.press(Role.GRANDMA)
    box.step_through()
    assert box.led(Role.GRANDMA) is LedState.DIM


def test_late_recorder_stop_is_delivered_later(make_box):
    """A recorder that ignores stop for longer than record_join_s still gets
    its take saved when it finally returns."""
    from zozobox.engine import EngineSettings, RecordStep

    box = make_box(settings=None)
    box.eng.settings.record_join_s = 0.05
    release = threading.Event()
    real = box.audio_in.record

    def slow(path, max_s, stop):
        release.wait(5)
        return real(path, max_s, threading.Event())

    box.audio_in.record = slow
    box.press(Role.GRANDMA)
    box.step_through(until=lambda: isinstance(box.eng.current.step, RecordStep))
    box.press(Role.STORY)  # switch: join times out
    assert box.store.recordings.live(Slot.ZOZO) is None
    release.set()
    deadline = time.monotonic() + 3
    while box.store.recordings.live(Slot.ZOZO) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert box.store.recordings.live(Slot.ZOZO) is not None


def test_with_real_button_arbiter_slaps(box):
    """Raw bouncy edges through the M1 arbiter: first press wins, the rest of
    the slap inside the lockout is dropped before it reaches the engine."""
    from zozobox.buttons import ButtonArbiter
    from zozobox.config import ButtonConfig

    levels = {b: False for b in Btn}
    arb = ButtonArbiter(ButtonConfig(), lambda b: levels[b])
    t = 100.0
    events = []
    rng = random.Random(3)
    for _ in range(300):  # a 1.5 s two-handed slap
        b = rng.choice(list(Btn))
        levels[b] = not levels[b]
        events += arb.edge(b, levels[b], t)
        t += 0.005
        events += arb.tick(t)
    presses = [e for e in events if isinstance(e, Press)]
    assert 1 <= len(presses) <= 2
    for e in events:
        box.eng.handle(e)
        box.tick()
    assert 1 <= box.clicks() <= len(presses)  # a Print re-press is silent
    assert len(box.playing()) <= 1
