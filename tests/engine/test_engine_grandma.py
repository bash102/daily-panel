"""Grandma: play her message, say hi, beep, record the reply (SPEC 2.2)."""

from zozobox.audio import FakeTake, Output
from zozobox.config import Role
from zozobox.engine import RecordStep
from zozobox.leds import LedState
from zozobox.store import RecStatus, Slot


def recording(box):
    return lambda: box.eng.current is not None and isinstance(box.eng.current.step, RecordStep)


def test_full_flow_with_message(make_box):
    box = make_box(takes=[FakeTake(duration_s=4.0)])
    msg = box.add_grandma()  # e.g. arrived via sync
    box.tick()
    assert box.led(Role.GRANDMA) is LedState.DIM  # not re-read yet
    box.clock.advance(box.settings.led_refresh_s)
    box.tick()
    assert box.led(Role.GRANDMA) is LedState.BRIGHT
    box.press(Role.GRANDMA)
    assert box.playing() == ["g1"]
    box.finish_audio()
    assert box.store.recordings.get(msg.id).heard_at is not None
    assert box.store.daily.grandma_heard
    assert box.playing() == ["say_hi_grandma"]
    box.finish_audio()
    beep = box.audio.plays[-1]
    assert beep.path.name == "beep.wav" and beep.output is Output.SPEAKER
    box.step_through()
    assert box.sounds() == ["g1", "say_hi_grandma", "beep"]
    assert box.audio_in.calls[0][1] == 30.0  # default rec_cap_s
    assert box.audio_in.muted_during == [True]
    live = box.store.recordings.live(Slot.ZOZO)
    assert live is not None and live.duration_s == 4.0
    assert box.store.media_exists(live.media)
    assert any(i.remote_key == live.media for i in box.store.outbox.pending())
    ev = box.events("recording")[-1]
    assert ev.payload["saved"] is True and ev.media == live.media
    assert box.led(Role.GRANDMA) is LedState.DIM  # message heard


def test_no_message_starts_at_say_hi(box):
    box.press(Role.GRANDMA)
    assert box.playing() == ["say_hi_grandma"]
    box.step_through()
    assert box.sounds() == ["say_hi_grandma", "beep"]
    assert box.store.recordings.live(Slot.ZOZO) is not None


def test_led_fast_pulse_while_recording_and_no_speaker_audio(make_box):
    box = make_box(takes=[FakeTake(duration_s=10.0, wait_for_stop=True)])
    box.press(Role.GRANDMA)
    box.step_through(until=recording(box))
    assert box.led(Role.GRANDMA) is LedState.PULSE_FAST
    box.wait(lambda: box.audio.speaker_is_muted)
    n = len(box.audio.plays)
    box.tick(5)
    assert len(box.audio.plays) == n  # the engine plays nothing while recording
    box.press(Role.GRANDMA)  # stop and save
    assert box.audio.muted_plays == []  # the stop click came after unmuting
    assert box.audio.plays[-1].path.name == "click.wav"
    box.step_through()
    assert box.led(Role.GRANDMA) is LedState.DIM


def test_same_press_during_message_skips_to_beep(box):
    msg = box.add_grandma()
    box.press(Role.GRANDMA)
    assert box.playing() == ["g1"]
    box.press(Role.GRANDMA)
    assert box.playing() == ["beep"]
    assert box.audio.plays[1].playback.stopped  # message stopped
    assert box.store.recordings.get(msg.id).heard_at is None  # not played to the end
    box.step_through()
    assert box.store.recordings.live(Slot.ZOZO) is not None


def test_same_press_during_say_hi_skips_to_beep(box):
    box.press(Role.GRANDMA)
    box.press(Role.GRANDMA)
    assert box.playing() == ["beep"]


def test_same_press_during_recording_stops_and_saves(make_box):
    box = make_box(takes=[FakeTake(duration_s=6.0, wait_for_stop=True)])
    box.press(Role.GRANDMA)
    box.step_through(until=recording(box))
    box.press(Role.GRANDMA)
    box.tick()
    assert not box.eng.busy  # Grandma itself just stops and saves
    assert box.store.recordings.live(Slot.ZOZO).duration_s == 6.0


def test_other_button_during_recording_saves_then_starts_it(make_box):
    box = make_box(takes=[FakeTake(duration_s=5.0, wait_for_stop=True)])
    box.add_story()
    box.press(Role.GRANDMA)
    box.step_through(until=recording(box))
    box.press(Role.STORY)
    assert box.store.recordings.live(Slot.ZOZO) is not None  # saved first
    assert box.eng.current.name == "story"
    assert box.playing() == ["chunk_0"]


def test_silent_reply_is_discarded_and_previous_kept(make_box):
    box = make_box(takes=[FakeTake(duration_s=3.0), FakeTake(duration_s=5.0, silent=True)])
    box.press(Role.GRANDMA)
    box.step_through()
    first = box.store.recordings.live(Slot.ZOZO)
    box.press(Role.GRANDMA)
    box.step_through()
    assert box.store.recordings.live(Slot.ZOZO).id == first.id
    assert len(box.store.recordings.history(Slot.ZOZO)) == 1
    last = box.events("recording")[-1]
    assert last.payload == {"slot": "zozo_message", "saved": False, "reason": "silent", "duration_s": 5.0}
    assert not box.audio_in.calls[1][0].exists()  # silent file removed


def test_empty_reply_when_stopped_instantly_is_discarded(make_box):
    box = make_box(takes=[FakeTake(duration_s=0.1, wait_for_stop=True)])
    box.press(Role.GRANDMA)
    box.step_through(until=recording(box))
    box.press(Role.GRANDMA)
    box.step_through()
    assert box.store.recordings.live(Slot.ZOZO) is None


def test_new_reply_supersedes_old(make_box):
    box = make_box(takes=[FakeTake(duration_s=3.0), FakeTake(duration_s=4.0)])
    box.press(Role.GRANDMA)
    box.step_through()
    old = box.store.recordings.live(Slot.ZOZO)
    box.clock.advance(60)
    box.press(Role.GRANDMA)
    box.step_through()
    new = box.store.recordings.live(Slot.ZOZO)
    assert new.id != old.id
    assert box.store.recordings.get(old.id).status is RecStatus.SUPERSEDED


def test_record_cap_comes_from_settings(make_box):
    box = make_box(takes=[FakeTake(duration_s=100.0)])
    box.store.settings.rec_cap_s = 12
    box.press(Role.GRANDMA)
    box.step_through()
    assert box.audio_in.calls[0][1] == 12.0
    assert box.store.recordings.live(Slot.ZOZO).duration_s == 12.0


def test_cap_enforced_on_engine_clock(make_box):
    box = make_box(takes=[FakeTake(duration_s=20.0, wait_for_stop=True)])
    box.store.settings.rec_cap_s = 15
    box.press(Role.GRANDMA)
    box.step_through(until=recording(box))
    step = box.eng.current.step
    box.clock.advance(14)
    box.tick()
    assert not step.stop_event.is_set()
    box.clock.advance(1)
    box.tick()
    assert step.stop_event.is_set()
    box.step_through()
    assert box.store.recordings.live(Slot.ZOZO).duration_s == 15.0


def test_recording_failure_keeps_engine_alive(make_box):
    box = make_box()

    def broken(*a, **k):
        raise OSError("no mic")

    box.audio_in.record = broken
    box.press(Role.GRANDMA)
    box.step_through()
    assert box.store.recordings.live(Slot.ZOZO) is None
    assert "record_failed" in [e.payload["kind"] for e in box.events("error")]
    box.press(Role.SOUNDBOARD)
    assert box.eng.current.name == "soundboard"


def test_withheld_or_missing_message_audio_is_skipped(box):
    rec = box.add_grandma()
    box.store.media_path(rec.media).unlink()
    box.press(Role.GRANDMA)
    assert box.playing() == ["say_hi_grandma"]


def test_parent_chord_during_recording_saves_and_stops(make_box):
    box = make_box(takes=[FakeTake(duration_s=7.0, wait_for_stop=True)])
    box.press(Role.GRANDMA)
    box.step_through(until=recording(box))
    box.chord()
    assert not box.eng.busy
    assert box.store.recordings.live(Slot.ZOZO).duration_s == 7.0
    assert len(box.chords) == 1
    assert box.audio.stop_all_calls == 1
