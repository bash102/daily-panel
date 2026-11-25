"""Print button: rotation, re-press rule, forfeiture, once per day, failures."""

from zozobox.audio import Output
from zozobox.config import Role
from zozobox.engine import PauseStep, PrintStep
from zozobox.leds import LedState
from zozobox.store import Kind


def test_letter_full_sequence_prints_and_advances(box):
    letter = box.add_letter("B", ("ball", "bear"))
    box.press(Role.PRINT)
    assert box.clicks() == 1
    assert box.audio.plays[0].output is Output.SPEAKER  # the click
    assert box.playing() == ["intro"]
    assert box.led(Role.PRINT) is LedState.PULSE_SLOW

    box.finish_audio()
    assert box.playing() == ["word_0"]
    box.finish_audio()
    # repeat-after-me pause: nothing new until the pause is over
    assert isinstance(box.eng.current.step, PauseStep)
    box.clock.advance(2.9)
    box.tick()
    assert box.playing() == []
    box.clock.advance(0.2)
    box.tick()
    assert box.playing() == ["word_1"]

    box.step_through()
    assert box.sounds() == ["intro", "word_0", "word_1", box.sounds()[3], "here_is_your_card"]
    assert box.sounds()[3].startswith("praise_")
    assert box.printer.count == 1
    assert box.store.daily.printed_today("letter")
    assert box.store.daily.print_item == "weather"
    assert box.store.content.get(letter.id).completed_at is not None
    prints = box.events("print")
    assert len(prints) == 1 and prints[0].payload["item"] == "letter"
    assert prints[0].payload["content_id"] == letter.id
    assert box.store.media_exists(prints[0].media)  # PNG copy archived
    assert box.printer.images[0].width == 696


def test_full_rotation_then_audio_only(make_box):
    from zozobox.engine import MoodResult

    box = make_box(results=[MoodResult("happy", "happy")])
    box.add_all_content()
    for expected in ("letter", "weather", "mood", "coloring"):
        assert box.store.daily.print_item == expected
        box.press(Role.PRINT)
        box.step_through()
    assert box.printer.count == 4
    assert [e.payload["item"] for e in box.events("print")] == ["letter", "weather", "mood", "coloring"]
    assert box.store.daily.print_item == "letter"
    assert box.led(Role.PRINT) is LedState.DIM  # nothing left to print today

    n = len(box.sounds())
    box.press(Role.PRINT)
    box.step_through()
    assert box.printer.count == 4  # once per day
    replay = box.sounds()[n:]
    assert replay[0] == "intro" and "here_is_your_card" not in replay
    assert replay[-1] == "all_printed_today"
    assert box.store.daily.print_item == "weather"


def test_print_press_while_running_is_ignored(box):
    box.add_letter()
    box.press(Role.PRINT)
    clicks = box.clicks()
    for _ in range(5):
        box.press(Role.PRINT)
    assert box.clicks() == clicks  # no click, no skip
    assert box.playing() == ["intro"]
    actions = [e.payload["action"] for e in box.events("press")]
    assert actions == ["start"] + ["ignored"] * 5
    box.step_through()
    assert box.printer.count == 1


def test_switching_away_forfeits_print_and_restarts_item(box):
    box.add_letter()
    box.add_story()
    box.press(Role.PRINT)
    box.step_through(until=lambda: "here_is_your_card" in box.playing())
    box.press(Role.STORY)  # abandon right before the print
    assert box.eng.current.name == "story"
    box.step_through()
    assert box.printer.count == 0
    assert box.store.daily.print_item == "letter"  # not advanced
    n = len(box.sounds())
    box.press(Role.PRINT)
    assert box.sounds()[n] == "intro"  # same item restarts from the top
    box.step_through()
    assert box.printer.count == 1


def test_printer_failure_not_marked_and_says_sleepy(box):
    box.add_letter()
    box.printer.fail_next = 1
    box.press(Role.PRINT)
    box.step_through()
    assert box.printer.count == 0
    assert "printer_sleepy" in box.sounds()
    assert not box.store.daily.printed_today("letter")
    assert box.events("print") == []
    errs = [e.payload["kind"] for e in box.events("error")]
    assert "print_failed" in errs
    assert box.store.daily.print_item == "weather"  # the sequence itself finished
    assert box.led(Role.PRINT) is LedState.BRIGHT


def test_missing_content_plays_not_ready_and_moves_on(box):
    box.press(Role.PRINT)  # no letter content at all
    box.step_through()
    assert box.sounds() == ["print_not_ready"]
    assert box.printer.count == 0
    assert box.store.daily.print_item == "weather"


def test_weather_and_coloring_cards(box):
    box.add_weather()
    box.add_coloring()
    box.store.daily.advance_rotation()  # -> weather
    box.press(Role.PRINT)
    box.step_through()
    assert box.sounds() == ["summary", "here_is_your_card"]
    box.store.daily.advance_rotation()  # mood -> coloring
    box.press(Role.PRINT)
    box.step_through()
    assert box.sounds()[-2:] == ["intro", "here_is_your_card"]
    assert [e.payload["item"] for e in box.events("print")] == ["weather", "coloring"]


def test_letter_without_drawing_and_missing_word_audio(box):
    c = box.add_letter("z", ("zoo",), drawing=False)
    box.store.content.set_media(c.id, {"intro": c.media["intro"]})  # word audio gone
    box.press(Role.PRINT)
    box.step_through()
    assert box.printer.count == 1
    assert "word_0" not in box.sounds()


def test_day_rollover_resets_rotation_and_leds(box):
    box.add_letter()
    box.press(Role.PRINT)
    box.step_through()
    assert box.store.daily.print_item == "weather"
    assert box.led(Role.PRINT) is LedState.BRIGHT  # mood is always printable
    box.clock.set_local(2026, 3, 11, 7, 30)  # next box day, after quiet hours
    box.add_letter("C", ("cat",), tag="b")
    box.tick()
    assert box.store.daily.print_item == "letter"
    assert box.store.daily.printed_items() == frozenset()
    box.press(Role.PRINT)
    box.step_through()
    assert box.printer.count == 2
    assert box.events("print")[-1].payload["letter"] == "C"


def test_rollover_mid_item_does_not_advance_new_day(box):
    box.add_letter()
    box.press(Role.PRINT)
    box.step_through(until=lambda: "here_is_your_card" in box.playing())
    box.clock.set_local(2026, 3, 11, 7, 1)
    box.step_through()
    assert box.store.daily.print_item == "letter"


def test_no_printing_when_quiet_hours_begin_mid_item(box):
    box.add_letter()
    box.press(Role.PRINT)
    box.step_through(until=lambda: "here_is_your_card" in box.playing())
    box.store.settings.set_quiet_hours("09:00", "11:00")  # it is 10:00
    box.step_through()
    assert box.printer.count == 0
    assert not box.store.daily.printed_today("letter")


def test_print_step_skips_already_printed(box):
    box.add_letter()
    box.press(Role.PRINT)
    box.step_through(until=lambda: "here_is_your_card" in box.playing())
    box.store.daily.mark_printed("letter")  # e.g. raced with another run
    box.step_through()
    assert box.printer.count == 0


def test_switch_during_print_keeps_bookkeeping(box):
    box.add_letter()
    box.press(Role.PRINT)
    box.step_through(until=lambda: isinstance(box.eng.current.step, PrintStep))
    box.press(Role.SOUNDBOARD)
    box.wait(lambda: box.printer.count == 1)
    box.wait(lambda: box.store.daily.printed_today("letter"))
    assert box.store.daily.print_item == "letter"  # abandoned: next press replays audio only


def test_unapproved_content_is_not_served(box):
    box.store.content.add(Kind.LETTER, media={"intro": box.media("x/intro.wav")},
                          meta={"letter": "Q", "words": []}, safety="passed", approval="pending")
    box.press(Role.PRINT)
    box.step_through()
    assert box.sounds() == ["print_not_ready"]
