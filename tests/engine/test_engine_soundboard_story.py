"""Soundboard (SPEC 2.3) and Story (SPEC 2.4)."""

from zozobox.config import Role
from zozobox.leds import LedState
from zozobox.store import Kind


def test_soundboard_plays_a_clip_and_avoids_recent(box):
    picks = []
    for _ in range(30):
        box.press(Role.SOUNDBOARD)
        picks.append(box.playing()[0])
        box.step_through()
    assert all(p.startswith("clip") for p in picks)
    for i in range(1, len(picks)):
        window = picks[max(0, i - 5):i]
        assert picks[i] not in window  # 6 clips: no repeat within the last 5
    assert len(set(picks)) == 6


def test_soundboard_same_press_stops_and_plays_another(box):
    box.press(Role.SOUNDBOARD)
    first = box.audio.plays[-1]
    box.press(Role.SOUNDBOARD)
    assert first.playback.stopped
    now = box.playing()
    assert len(now) == 1 and now[0] != first.path.stem
    assert box.led(Role.SOUNDBOARD) is LedState.PULSE_SLOW
    box.step_through()
    assert box.led(Role.SOUNDBOARD) is LedState.DIM


def test_soundboard_with_one_clip_repeats_it(make_box):
    box = make_box(n_clips=1)
    box.press(Role.SOUNDBOARD)
    box.press(Role.SOUNDBOARD)
    assert box.playing() == ["clip0"]


def test_soundboard_without_clips_does_nothing(make_box):
    box = make_box(n_clips=0)
    box.press(Role.SOUNDBOARD)
    assert not box.eng.busy
    assert box.clicks() == 1


def test_story_plays_chunks_and_marks_heard(box):
    story = box.add_story(3)
    box.refresh()
    assert box.led(Role.STORY) is LedState.BRIGHT
    box.press(Role.STORY)
    assert box.playing() == ["chunk_0"]
    assert box.led(Role.STORY) is LedState.PULSE_SLOW
    box.step_through()
    assert box.sounds() == ["chunk_0", "chunk_1", "chunk_2"]
    assert box.store.daily.story_heard
    item = box.store.content.get(story.id)
    assert item.served_day == box.store.box_day() and item.completed_at is not None
    assert box.led(Role.STORY) is LedState.DIM
    # unlimited replays of today's story
    box.press(Role.STORY)
    box.step_through()
    assert box.sounds() == ["chunk_0", "chunk_1", "chunk_2"] * 2


def test_story_same_press_skips_to_next_chunk(box):
    box.add_story(3)
    box.press(Role.STORY)
    box.press(Role.STORY)
    assert box.playing() == ["chunk_1"]
    box.press(Role.STORY)
    assert box.playing() == ["chunk_2"]
    assert not box.store.daily.story_heard
    box.press(Role.STORY)  # skipping the last chunk finishes the story
    assert not box.eng.busy
    assert box.store.daily.story_heard


def test_story_abandoned_is_not_heard(box):
    box.add_story(2)
    box.press(Role.STORY)
    box.press(Role.SOUNDBOARD)
    box.step_through()
    assert not box.store.daily.story_heard
    assert box.led(Role.STORY) is LedState.BRIGHT


def test_no_story_says_so(box):
    box.press(Role.STORY)
    box.step_through()
    assert box.sounds() == ["no_story_yet"]
    assert box.led(Role.STORY) is LedState.DIM


def test_story_chunk_order_from_meta_and_natural_sort(box):
    media = {k: box.media(f"st/{k}.wav") for k in ("chunk_10", "chunk_2", "chunk_1")}
    box.add(Kind.STORY, media)
    box.press(Role.STORY)
    box.step_through()
    assert box.sounds() == ["chunk_1", "chunk_2", "chunk_10"]


def test_story_meta_chunks_list(box):
    media = {k: box.media(f"st2/{k}.wav") for k in ("b", "a")}
    box.add(Kind.STORY, media, {"chunks": ["b", "a", "missing"]})
    box.press(Role.STORY)
    box.step_through()
    assert box.sounds() == ["b", "a"]


def test_story_with_missing_files_plays_what_exists(box):
    s = box.add_story(3)
    box.store.media_path(s.media["chunk_1"]).unlink()
    box.press(Role.STORY)
    box.step_through()
    assert box.sounds() == ["chunk_0", "chunk_2"]


def test_story_with_no_files_says_no_story(box):
    s = box.add_story(1)
    box.store.media_path(s.media["chunk_0"]).unlink()
    box.press(Role.STORY)
    box.step_through()
    assert box.sounds() == ["no_story_yet"]
