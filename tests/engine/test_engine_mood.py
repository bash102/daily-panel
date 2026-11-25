"""Mood item: ask, record, classify, retry once, print a face or all four."""

import threading

from zozobox.audio import FakeTake
from zozobox.config import Role
from zozobox.engine import ClassifyStep, MoodResult, RecordStep
from zozobox.leds import LedState


def start_mood(box):
    box.store.daily.advance_rotation()
    box.store.daily.advance_rotation()
    assert box.store.daily.print_item == "mood"
    box.press(Role.PRINT)


def mood_events(box):
    return [(e.payload["label"], e.payload["source"], e.payload.get("attempt")) for e in box.events("mood")]


def test_recognized_mood_prints_that_face(make_box):
    box = make_box(results=[MoodResult("happy", "I'm happy!")])
    start_mood(box)
    assert box.playing() == ["ask_mood"]
    box.finish_audio()
    box.wait(lambda: box.audio_in.calls)
    box.step_through()
    assert box.sounds() == ["ask_mood", "you_feel_happy", "here_is_your_card"]
    assert box.audio_in.calls[0][1] == 8.0
    assert box.audio_in.muted_during == [True]
    assert mood_events(box) == [("happy", "online", 1)]
    ev = box.events("mood")[0]
    assert ev.payload["transcript"] == "I'm happy!"
    assert box.store.media_exists(ev.media)
    assert any(i.remote_key == ev.media for i in box.store.outbox.pending())
    p = box.events("print")[0]
    assert p.payload["item"] == "mood" and p.payload["mood"] == "happy"
    assert box.store.daily.printed_today("mood")
    assert box.store.events.first_mood().payload["label"] == "happy"


def test_unknown_then_recognized(make_box):
    box = make_box(results=[MoodResult("unknown", "blah"), MoodResult("sad", "sad")])
    start_mood(box)
    box.step_through()
    assert box.sounds() == ["ask_mood", "ask_mood_again", "you_feel_sad", "here_is_your_card"]
    assert len(box.audio_in.calls) == 2
    assert mood_events(box) == [("unknown", "online", 1), ("sad", "online", 2)]
    assert box.events("print")[0].payload["mood"] == "sad"


def test_unknown_twice_prints_all_faces(make_box):
    box = make_box(results=[MoodResult("unknown", "a"), MoodResult("unknown", "b")])
    start_mood(box)
    box.step_through()
    assert box.sounds() == ["ask_mood", "ask_mood_again", "here_is_your_card"]
    assert mood_events(box) == [("unknown", "online", 1), ("unknown", "online", 2)]
    assert box.events("print")[0].payload["mood"] == "all"
    assert box.printer.count == 1


def test_silent_answer_prints_all_faces_without_classifying(make_box):
    box = make_box(takes=[FakeTake(duration_s=8.0, silent=True)])
    start_mood(box)
    box.step_through()
    assert box.classifier.calls == []
    assert mood_events(box) == [("silent", "box", 1)]
    assert box.events("print")[0].payload["mood"] == "all"


def test_offline_queues_for_later_and_prints_all_faces(make_box):
    box = make_box(results=[MoodResult("unknown", "", offline=True)])
    start_mood(box)
    box.step_through()
    assert box.events("mood") == []  # M7 logs it once recognized
    pend = box.events("mood_pending")
    assert len(pend) == 1 and box.store.media_exists(pend[0].media)
    assert any(i.remote_key == pend[0].media for i in box.store.outbox.pending())
    assert box.events("print")[0].payload["mood"] == "all"
    assert "ask_mood_again" not in box.sounds()


def test_classifier_error_counts_as_offline(make_box):
    box = make_box(results=[RuntimeError("network down")])
    start_mood(box)
    box.step_through()
    assert len(box.events("mood_pending")) == 1
    assert box.printer.count == 1


def test_classifier_bad_label_counts_as_unknown(make_box):
    box = make_box(results=[MoodResult("ecstatic"), MoodResult("mad")])
    start_mood(box)
    box.step_through()
    assert mood_events(box) == [("unknown", "online", 1), ("mad", "online", 2)]


def test_classifier_timeout_counts_as_offline(make_box):
    box = make_box()
    gate = threading.Event()
    box.classifier.gate = gate
    try:
        start_mood(box)
        box.step_through(until=lambda: isinstance(box.eng.current.step, ClassifyStep))
        box.tick()
        assert box.eng.busy and box.printer.count == 0
        box.clock.advance(box.settings.classify_timeout_s)
        box.step_through()
        assert len(box.events("mood_pending")) == 1
        assert box.events("print")[0].payload["mood"] == "all"
    finally:
        gate.set()


def test_mood_already_printed_does_not_print_again(make_box):
    box = make_box(results=[MoodResult("happy")])
    box.store.daily.mark_printed("mood")
    start_mood(box)
    box.step_through()
    assert box.printer.count == 0
    assert "you_feel_happy" in box.sounds()
    assert "here_is_your_card" not in box.sounds()
    assert mood_events(box) == [("happy", "online", 1)]  # still logged


def test_switching_mid_mood_recording_discards(make_box):
    box = make_box(takes=[FakeTake(duration_s=8.0, wait_for_stop=True)])
    start_mood(box)
    box.step_through(until=lambda: isinstance(box.eng.current.step, RecordStep))
    assert box.led(Role.PRINT) is LedState.PULSE_FAST
    path = box.eng.current.step.path
    box.press(Role.SOUNDBOARD)
    box.wait(lambda: not path.exists())
    assert box.eng.current.name == "soundboard"
    box.step_through()
    assert box.printer.count == 0
    assert box.events("mood") == [] and box.events("mood_pending") == []
    assert box.store.daily.print_item == "mood"  # abandoned: same item next time
    assert box.classifier.calls == []


def test_print_press_during_mood_recording_is_ignored(make_box):
    box = make_box(takes=[FakeTake(duration_s=8.0, wait_for_stop=True)], results=[MoodResult("happy")])
    start_mood(box)
    box.step_through(until=lambda: isinstance(box.eng.current.step, RecordStep))
    step = box.eng.current.step
    box.press(Role.PRINT)
    assert not step.stop_event.is_set()
    # the 8 s cap still ends the take (engine clock)
    box.clock.advance(8.0)
    box.step_through()
    assert mood_events(box) == [("happy", "online", 1)]


def test_recorder_failure_prints_all_faces(make_box):
    box = make_box()

    def broken(*a, **k):
        raise OSError("mic gone")

    box.audio_in.record = broken
    start_mood(box)
    box.step_through()
    assert box.events("print")[0].payload["mood"] == "all"
    assert "record_failed" in [e.payload["kind"] for e in box.events("error")]
