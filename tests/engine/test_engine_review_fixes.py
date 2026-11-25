"""Regression tests for review findings: mute vs click on switch-away, mood
takes abandoned mid-classification, print refusals and double prints,
abandoned print failures, print timeout, close() waiting for jobs, the
day rollover before a print, failed story audio, and press-at-step-boundary."""

import threading
import time

from zozobox.audio import FakeAudioIn, FakeTake, Output, Playback
from zozobox.audio.base import muted
from zozobox.config import Role
from zozobox.engine import ClassifyStep, PrintStep, RecordStep
from zozobox.leds import LedState
from zozobox.printer import PrinterError
from zozobox.store import Slot


def start_mood(box):
    box.store.daily.advance_rotation()
    box.store.daily.advance_rotation()
    assert box.store.daily.print_item == "mood"
    box.press(Role.PRINT)


class SlowStopAudioIn(FakeAudioIn):
    """Like arecord + amixer teardown: the speaker stays muted a little after
    the recorder has seen the stop event."""

    def __init__(self, *a, hold_s: float = 0.05, **kw):
        super().__init__(*a, **kw)
        self.hold_s = hold_s

    def record(self, path, max_s, stop):
        with muted(self.muter):
            rec = super().record(path, max_s, stop)
            time.sleep(self.hold_s)
        return rec


def watch_mute(box):
    """Log (stem, output, speaker muted?) for every play request."""
    seen = []
    real = box.audio.play

    def play(path, output=Output.BOTH):
        seen.append((path.stem, output, box.audio.speaker_is_muted))
        return real(path, output)

    box.audio.play = play
    return seen


class GatePrinter:
    """Wraps a FakePrinter; print_image blocks until `gate` is set."""

    def __init__(self, inner, *, fail: bool = False):
        self.inner = inner
        self.gate = threading.Event()
        self.started = threading.Event()
        self.calls = 0
        self.fail = fail

    def print_image(self, img):
        self.calls += 1
        self.started.set()
        self.gate.wait(10)
        if self.fail:
            raise PrinterError("out of paper")
        self.inner.print_image(img)


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.005)


# ------------------------------------------------- recording mute vs click ---


def test_switch_from_mood_recording_waits_for_unmute_before_click(make_box):
    box = make_box()
    box.eng.audio_in = SlowStopAudioIn([FakeTake(duration_s=8.0, wait_for_stop=True)],
                                       muter=box.audio)
    start_mood(box)
    box.step_through(until=lambda: isinstance(box.eng.current.step, RecordStep))
    seen = watch_mute(box)
    path = box.eng.current.step.path
    box.press(Role.SOUNDBOARD)
    assert box.audio.muted_plays == []
    assert box.clicks() == 2
    assert seen[0][0] == "click" and seen[1][0].startswith("clip")
    assert not any(m for _, _, m in seen), seen  # nothing started while muted
    assert not path.exists()  # the take is still discarded


def test_quiet_press_during_mood_recording_waits_for_unmute(make_box):
    box = make_box()
    box.eng.audio_in = SlowStopAudioIn([FakeTake(duration_s=8.0, wait_for_stop=True)],
                                       muter=box.audio)
    start_mood(box)
    box.step_through(until=lambda: isinstance(box.eng.current.step, RecordStep))
    seen = watch_mute(box)
    box.store.settings.set_quiet_hours("09:00", "11:00")  # it is 10:00
    box.press(Role.STORY)
    assert seen and seen[0][0] == "sleepy" and seen[0][2] is False


def test_click_lost_to_stuck_recorder_is_replayed(make_box):
    box = make_box()
    box.eng.settings.record_join_s = 0.02
    release = threading.Event()
    inner = box.audio_in

    def stuck(path, max_s, stop):
        with muted(box.audio):
            release.wait(5)
            return inner.record(path, max_s, threading.Event())

    box.eng.audio_in.record = stuck
    start_mood(box)
    box.step_through(until=lambda: isinstance(box.eng.current.step, RecordStep))
    box.press(Role.SOUNDBOARD)
    assert box.clicks() == 1  # the Print click only; the new one was suppressed
    assert any(p.name == "click.wav" for p, _ in box.audio.muted_plays)
    release.set()
    wait_for(lambda: not box.audio.speaker_is_muted)
    box.tick()
    assert box.clicks() == 2


# ------------------------------------------- mood abandoned while classifying ---


def test_switch_during_classification_logs_mood_pending(make_box):
    box = make_box()
    gate = threading.Event()
    box.classifier.gate = gate
    try:
        start_mood(box)
        box.step_through(until=lambda: isinstance(box.eng.current.step, ClassifyStep))
        rec_step = [s for s in box.eng.current.steps if isinstance(s, RecordStep)][0]
        box.press(Role.SOUNDBOARD)
        pend = box.events("mood_pending")
        assert len(pend) == 1
        ev = pend[0]
        assert ev.payload["attempt"] == 1 and ev.payload["reason"] == "abandoned"
        assert ev.media is not None and ev.media == ev.payload["audio"]
        assert box.store.media_path(ev.media).exists()
        assert ev.ts == rec_step.started_at
    finally:
        gate.set()
    time.sleep(0.05)
    box.tick()
    assert box.events("mood") == []  # the late result is dropped; M7 handles it
    assert len(box.events("mood_pending")) == 1
    assert box.printer.count == 0


# ------------------------------------------------------ print refusals ---


def test_quiet_refused_print_does_not_advance_rotation(box):
    c = box.add_letter()
    box.press(Role.PRINT)
    box.step_through(until=lambda: "here_is_your_card" in box.playing())
    box.store.settings.set_quiet_hours("09:00", "11:00")  # it is 10:00 (nap time)
    box.step_through()
    assert box.printer.count == 0
    assert box.store.daily.print_item == "letter"
    assert box.store.content.get(c.id).completed_at is None
    # after the nap the same item runs again and prints
    box.store.settings.set_quiet_hours("19:00", "07:00")
    box.press(Role.PRINT)
    box.step_through()
    assert box.printer.count == 1
    assert box.store.daily.print_item == "weather"


def test_print_sequence_crossing_day_rollover_does_not_print(box):
    box.store.settings.set_quiet_hours("21:00", "05:00")
    box.clock.set_local(2026, 3, 11, 5, 59, 50)
    box.add_letter()
    box.press(Role.PRINT)
    box.clock.advance(15)  # now the next box day
    box.step_through()
    assert box.printer.count == 0
    assert box.store.daily.printed_items() == frozenset()
    assert box.store.daily.print_item == "letter"
    box.add_letter("C", ("cat",), tag="b")
    box.press(Role.PRINT)
    assert box.eng.current.data["will_print"] is True


# ---------------------------------------------------------- double print ---


def test_reprint_while_abandoned_job_runs_prints_once(box):
    box.add_letter()
    gp = GatePrinter(box.printer)
    box.eng.printer = gp
    try:
        box.press(Role.PRINT)
        box.step_through(until=lambda: isinstance(box.eng.current.step, PrintStep))
        wait_for(gp.started.is_set)
        box.press(Role.SOUNDBOARD)
        box.press(Role.PRINT)
        assert box.eng.current.data["will_print"] is False
        box.step_through()
    finally:
        gp.gate.set()
    wait_for(lambda: box.store.daily.printed_today("letter"))
    box.eng.close()
    assert gp.calls == 1
    assert box.printer.count == 1
    assert len(box.events("print")) == 1


def test_print_step_refuses_item_already_in_flight(box):
    box.add_letter()
    box.press(Role.PRINT)
    box.step_through(until=lambda: "here_is_your_card" in box.playing())
    assert box.eng.begin_print("letter")  # e.g. a job from an abandoned run
    try:
        box.step_through()
    finally:
        box.eng.end_print("letter")
    assert box.printer.count == 0


# ------------------------------------------- abandoned failure / timeout ---


def test_failure_of_abandoned_print_is_logged(box):
    box.add_letter()
    gp = GatePrinter(box.printer, fail=True)
    box.eng.printer = gp
    box.press(Role.PRINT)
    box.step_through(until=lambda: isinstance(box.eng.current.step, PrintStep))
    wait_for(gp.started.is_set)
    box.press(Role.SOUNDBOARD)
    gp.gate.set()
    wait_for(lambda: any(e.payload["kind"] == "print_failed" for e in box.events("error")))
    assert not box.store.daily.printed_today("letter")
    assert "printer_sleepy" not in box.sounds()  # that experience is gone


def test_print_failure_logged_once_when_not_abandoned(box):
    box.add_letter()
    box.printer.fail_next = 1
    box.press(Role.PRINT)
    box.step_through()
    assert [e.payload["kind"] for e in box.events("error")].count("print_failed") == 1


def test_hung_printer_times_out(box):
    box.add_letter()
    gp = GatePrinter(box.printer)
    box.eng.printer = gp
    try:
        box.press(Role.PRINT)
        box.step_through(until=lambda: isinstance(box.eng.current.step, PrintStep))
        wait_for(gp.started.is_set)
        box.clock.advance(box.settings.print_timeout_s)
        box.step_through()
        assert not box.eng.busy
        assert "printer_sleepy" in box.sounds()
        errs = [e.payload for e in box.events("error") if e.payload["kind"] == "print_failed"]
        assert errs and errs[0]["reason"] == "timeout"
        assert box.led(Role.PRINT) is LedState.BRIGHT
        box.press(Role.PRINT)  # the button works again (weather now)
        assert box.eng.busy
    finally:
        gp.gate.set()
    wait_for(lambda: box.store.daily.printed_today("letter"))  # the job finished late
    box.eng.close()
    assert len(box.events("print")) == 1


def test_close_waits_for_running_print_job(box):
    box.add_letter()
    gp = GatePrinter(box.printer)
    box.eng.printer = gp
    box.press(Role.PRINT)
    box.step_through(until=lambda: isinstance(box.eng.current.step, PrintStep))
    wait_for(gp.started.is_set)
    box.press(Role.SOUNDBOARD)
    threading.Timer(0.1, gp.gate.set).start()
    box.eng.close()
    assert box.store.daily.printed_today("letter")
    assert len(box.events("print")) == 1


def test_close_waits_for_late_grandma_save(make_box):
    box = make_box()
    box.eng.settings.record_join_s = 0.02
    release = threading.Event()
    real = box.audio_in.record

    def slow(path, max_s, stop):
        release.wait(5)
        return real(path, max_s, threading.Event())

    box.audio_in.record = slow
    box.press(Role.GRANDMA)
    box.step_through(until=lambda: isinstance(box.eng.current.step, RecordStep))
    box.press(Role.STORY)
    threading.Timer(0.1, release.set).start()
    box.eng.close()
    assert box.store.recordings.live(Slot.ZOZO) is not None


# ------------------------------------------------------------------ story ---


def test_story_with_all_audio_failing_is_not_marked_heard(box):
    box.add_story()
    box.refresh()
    real = box.audio.play

    def broken(path, output=Output.BOTH):
        if output is Output.BOTH:
            return Playback(failed=True)
        return real(path, output)

    box.audio.play = broken
    box.press(Role.STORY)
    box.step_through()
    assert not box.store.daily.story_heard
    assert box.led(Role.STORY) is LedState.BRIGHT


def test_story_skipped_chunks_still_count_as_heard(box):
    box.add_story(2)
    box.press(Role.STORY)
    box.press(Role.STORY)
    box.press(Role.STORY)
    assert not box.eng.busy
    assert box.store.daily.story_heard


# ------------------------------------------------- press at step boundary ---


def test_grandma_press_right_after_message_ended_marks_heard(box):
    box.add_grandma()
    box.press(Role.GRANDMA)
    assert box.playing() == ["g1"]
    box.audio.plays[-1].proc.finish()  # message ended, not polled yet
    box.press(Role.GRANDMA)
    assert not box.store.recordings.has_unheard(Slot.GRANDMA)
    assert box.playing() == ["beep"]
