"""LED rules (SPEC 4) and quiet hours (SPEC 6)."""

from zozobox.config import Role
from zozobox.engine import RecordStep
from zozobox.audio import FakeTake
from zozobox.leds import LedState
from zozobox.store import Slot


def states(box):
    return {r: box.led(r) for r in Role}


def test_resting_states(box):
    box.add_all_content()
    box.add_grandma()
    box.tick()
    box.clock.advance(box.settings.led_refresh_s)
    box.tick()
    assert states(box) == {
        Role.PRINT: LedState.BRIGHT,
        Role.GRANDMA: LedState.BRIGHT,
        Role.SOUNDBOARD: LedState.DIM,
        Role.STORY: LedState.BRIGHT,
    }


def test_empty_box_is_dim_but_print_has_mood(box):
    # mood needs no content, so Print is "new" until it has printed
    assert states(box) == {
        Role.PRINT: LedState.BRIGHT,
        Role.GRANDMA: LedState.DIM,
        Role.SOUNDBOARD: LedState.DIM,
        Role.STORY: LedState.DIM,
    }


def test_active_button_pulses_others_keep_state(box):
    box.add_story()
    box.press(Role.STORY)
    assert box.led(Role.STORY) is LedState.PULSE_SLOW
    assert box.led(Role.SOUNDBOARD) is LedState.DIM
    box.press(Role.SOUNDBOARD)
    assert box.led(Role.SOUNDBOARD) is LedState.PULSE_SLOW
    assert box.led(Role.STORY) is LedState.BRIGHT  # abandoned, still unheard


def test_quiet_hours_leds_off_and_back(box):
    box.store.settings.set_quiet_hours("10:30", "11:00")
    box.clock.advance(minutes=31)
    box.tick()
    assert set(states(box).values()) == {LedState.OFF}
    box.clock.advance(minutes=30)
    box.tick()
    assert box.led(Role.SOUNDBOARD) is LedState.DIM


def test_quiet_press_plays_sleepy_at_most_every_5_min(box):
    box.add_story()
    box.store.settings.set_quiet_hours("09:00", "12:00")
    box.tick()
    box.press(Role.STORY)
    assert box.sounds() == ["sleepy"]
    assert box.clicks() == 0
    assert box.led(Role.STORY) is LedState.OFF
    box.step_through()
    for role in (Role.STORY, Role.PRINT, Role.GRANDMA, Role.SOUNDBOARD):
        box.clock.advance(30)
        box.press(role)
        box.step_through()
    assert box.sounds() == ["sleepy"]  # Story blocked, nothing else either
    box.clock.advance(300)
    box.press(Role.PRINT)
    assert box.sounds() == ["sleepy", "sleepy"]
    box.step_through()
    assert box.printer.count == 0
    qp = box.events("quiet_press")
    assert len(qp) == 6 and [e.payload["sleepy"] for e in qp] == [True, False, False, False, False, True]
    assert box.events("press") == []


def test_running_experience_finishes_when_quiet_starts(box):
    box.add_story(3)
    box.press(Role.STORY)
    box.store.settings.set_quiet_hours("09:00", "12:00")
    box.clock.advance(2)
    box.tick()
    assert box.eng.busy and box.led(Role.STORY) is LedState.PULSE_SLOW
    assert box.led(Role.SOUNDBOARD) is LedState.OFF
    box.step_through()
    assert box.sounds() == ["chunk_0", "chunk_1", "chunk_2"]
    assert box.led(Role.STORY) is LedState.OFF
    box.press(Role.STORY)  # but no new ones
    assert box.sounds()[-1] == "sleepy"


def test_quiet_press_during_grandma_recording_saves_it(make_box):
    box = make_box(takes=[FakeTake(duration_s=4.0, wait_for_stop=True)])
    box.press(Role.GRANDMA)
    box.step_through(until=lambda: isinstance(box.eng.current.step, RecordStep))
    box.store.settings.set_quiet_hours("09:00", "12:00")
    box.press(Role.SOUNDBOARD)
    assert box.store.recordings.live(Slot.ZOZO) is not None
    assert box.sounds()[-1] == "sleepy"


def test_rate_limited_quiet_press_leaves_running_experience(box):
    box.store.settings.set_quiet_hours("09:00", "12:00")
    box.press(Role.STORY)  # sleepy
    box.step_through()
    box.store.settings.set_quiet_hours("13:00", "14:00")
    box.add_story(2)
    box.press(Role.STORY)
    box.store.settings.set_quiet_hours("09:00", "12:00")
    box.clock.advance(10)
    box.press(Role.SOUNDBOARD)  # within 5 min: ignored, story keeps going
    assert box.eng.current.name == "story"
    assert box.playing() == ["chunk_0"]


def test_sleepy_phrase_missing_is_harmless(make_box):
    box = make_box(missing_phrases=["sleepy"])
    box.store.settings.set_quiet_hours("09:00", "12:00")
    box.press(Role.STORY)
    box.step_through()
    assert box.sounds() == []
    assert len(box.events("quiet_press")) == 1
