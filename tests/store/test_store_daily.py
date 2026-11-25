import pytest

from zozobox.store import PRINT_ROTATION, StoreError


def test_fresh_day(store):
    snap = store.daily.snapshot()
    assert snap.day == store.box_day()
    assert snap.printed == frozenset()
    assert store.daily.print_item == "letter"
    assert not store.daily.story_heard
    assert not store.daily.grandma_heard


def test_rotation_cycles(store):
    seen = [store.daily.print_item]
    for _ in range(4):
        seen.append(store.daily.advance_rotation())
    assert seen == list(PRINT_ROTATION) + ["letter"]
    assert store.daily.rotation_index == 0


def test_printed_once_per_day(store):
    assert not store.daily.printed_today("weather")
    store.daily.mark_printed("weather")
    store.daily.mark_printed("weather")
    assert store.daily.printed_today("weather")
    assert store.daily.printed_items() == {"weather"}
    with pytest.raises(StoreError):
        store.daily.mark_printed("sticker")


def test_flags(store):
    store.daily.mark_story_heard()
    store.daily.mark_grandma_heard()
    assert store.daily.story_heard and store.daily.grandma_heard


def test_resets_at_day_start_not_midnight(store, clock):
    store.daily.mark_printed("letter")
    store.daily.advance_rotation()
    store.daily.mark_story_heard()
    clock.set_local(2026, 3, 11, 0, 30)  # after midnight: still the same box day
    assert store.daily.printed_today("letter")
    assert store.daily.print_item == "weather"
    clock.set_local(2026, 3, 11, 6, 0)  # new box day
    assert not store.daily.printed_today("letter")
    assert store.daily.print_item == "letter"
    assert not store.daily.story_heard
    # history is kept
    from datetime import date

    assert store.daily.snapshot(date(2026, 3, 10)).printed == {"letter"}


def test_survives_restart(tmp_path, clock):
    from zozobox.store import Store

    with Store(tmp_path, clock=clock) as s:
        s.daily.mark_printed("mood")
        s.daily.advance_rotation()
    with Store(tmp_path, clock=clock) as s:
        assert s.daily.printed_today("mood")
        assert s.daily.print_item == "weather"


def test_day_start_setting_moves_boundary(store, clock):
    store.settings.day_start = "08:00"
    clock.set_local(2026, 3, 10, 7, 0)
    store.daily.mark_printed("letter")
    clock.set_local(2026, 3, 10, 7, 59)
    assert store.daily.printed_today("letter")
    clock.set_local(2026, 3, 10, 8, 0)
    assert not store.daily.printed_today("letter")
