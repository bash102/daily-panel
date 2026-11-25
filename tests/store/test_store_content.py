from datetime import date, timedelta

import pytest

from zozobox.store import Approval, Kind, Safety, StoreError


def ok(store, kind=Kind.STORY, **kw):
    """A servable item (safety passed, auto-approved)."""
    kw.setdefault("safety", Safety.PASSED)
    kw.setdefault("approval", Approval.AUTO)
    return store.content.add(kind, **kw)


@pytest.fixture
def after_month(store):
    store.settings.approval_required_until = None
    return store


def test_add_and_get_roundtrip(store, clock):
    it = store.content.add(
        "story",
        media={"audio": "content/story/a.opus", "text": "content/story/a.txt"},
        meta={"title": "Zozo and the duck"},
        for_date=date(2026, 3, 11),
    )
    got = store.content.get(it.id)
    assert got == it
    assert got.kind is Kind.STORY
    assert got.created_at == clock()
    assert got.media["audio"] == "content/story/a.opus"
    assert got.meta == {"title": "Zozo and the duck"}
    assert got.safety is Safety.PENDING
    assert not got.servable and not got.served


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x.png", "a/../../x", ""])
def test_media_paths_must_be_relative(store, bad):
    with pytest.raises(StoreError):
        store.content.add("story", media={"audio": bad})


def test_unknown_kind_rejected(store):
    with pytest.raises(ValueError):
        store.content.add("poem")


def test_first_month_needs_approval_then_auto(store, clock):
    it = store.content.add("story", safety="passed")
    assert it.approval is Approval.PENDING
    assert not it.servable
    assert store.content.current_for_today("story") is None
    assert store.content.pending_approval() == [it]
    store.content.approve(it.id)
    assert store.content.current_for_today("story").id == it.id

    clock.advance(days=31)
    it2 = store.content.add("story", safety="passed")
    assert it2.approval is Approval.AUTO


def test_servable_needs_safety_passed(after_month):
    s = after_month
    it = s.content.add("story")
    assert it.approval is Approval.AUTO
    assert s.content.current_for_today("story") is None
    s.content.set_safety(it.id, "failed", "too scary")
    assert s.content.current_for_today("story") is None
    assert s.content.list(safety="failed")[0].safety_note == "too scary"
    s.content.set_safety(it.id, "passed")
    assert s.content.current_for_today("story").id == it.id


def test_rejected_not_servable(store):
    it = store.content.add("story", safety="passed")
    store.content.reject(it.id)
    assert store.content.current_for_today("story") is None
    assert store.content.needs_generation("story")


def test_served_item_stays_todays_item(store, clock):
    a = ok(store)
    store.content.mark_served(a.id)
    b = ok(store)  # a newer item arrives the same day
    assert store.content.current_for_today("story").id == a.id
    clock.advance(hours=5)
    assert store.content.current_for_today("story").id == a.id
    # next box day: a is used up, b is offered
    clock.advance(days=1)
    assert store.content.current_for_today("story").id == b.id


def test_unserved_item_carries_over(store, clock):
    a = ok(store)
    for _ in range(3):
        clock.advance(days=1)
        assert store.content.current_for_today("story").id == a.id
        assert not store.content.needs_generation("story")
    store.content.mark_served(a.id)
    assert store.content.needs_generation("story")


def test_oldest_unserved_offered_first_and_future_dates_wait(store):
    today = store.box_day()
    later = ok(store, for_date=today + timedelta(days=2))
    first = ok(store, for_date=today)
    assert store.content.current_for_today("story").id == first.id
    store.content.mark_served(first.id)
    assert store.content.current_for_today("story", today + timedelta(days=1)) is None
    assert store.content.current_for_today("story", today + timedelta(days=2)).id == later.id


def test_mark_served_is_idempotent_and_pins_box_day(store, clock):
    a = ok(store)
    first = store.content.mark_served(a.id)
    clock.advance(hours=1)
    again = store.content.mark_served(a.id)
    assert again.first_served_at == first.first_served_at
    assert again.served_day == store.box_day()


def test_needs_generation_counts_pending_items(store):
    assert store.content.needs_generation("coloring")
    it = store.content.add("coloring")  # pending safety + approval
    assert not store.content.needs_generation("coloring")
    store.content.set_safety(it.id, "failed")
    assert store.content.needs_generation("coloring")


def test_needs_generation_kinds_are_independent(store):
    ok(store, Kind.LETTER)
    assert not store.content.needs_generation("letter")
    assert store.content.needs_generation("story")


def test_weather_regenerates_daily(store, clock):
    assert store.content.needs_generation("weather")
    w = ok(store, Kind.WEATHER, meta={"summary": "sunny"})
    assert w.for_date == store.box_day()
    assert not store.content.needs_generation("weather")
    assert store.content.current_for_today("weather").id == w.id
    clock.advance(days=1)
    # yesterday's unserved weather does not carry over
    assert store.content.needs_generation("weather")
    assert store.content.current_for_today("weather") is None


def test_weather_served_stays_and_new_one_supersedes_unserved(store):
    w1 = ok(store, Kind.WEATHER)
    w2 = ok(store, Kind.WEATHER)
    assert store.content.get(w1.id).superseded_by == w2.id
    assert store.content.current_for_today("weather").id == w2.id
    store.content.mark_served(w2.id)
    w3 = ok(store, Kind.WEATHER)  # a refresh later in the day doesn't swap it
    assert not store.content.get(w2.id).superseded
    assert store.content.current_for_today("weather").id == w2.id
    assert w3.servable


def test_withhold_stops_serving_but_keeps(store):
    a = ok(store)
    store.content.mark_served(a.id)
    store.content.withhold(a.id)
    assert store.content.current_for_today("story") is None
    kept = store.content.get(a.id)
    assert kept.withheld and kept.withheld_at is not None
    assert kept in store.content.list("story")
    assert kept not in store.content.list("story", include_withheld=False)
    store.content.unwithhold(a.id)
    assert store.content.current_for_today("story").id == a.id


def test_supersede(store):
    a = ok(store)
    b = ok(store)
    store.content.supersede(a.id, by=b.id)
    assert store.content.current_for_today("story").id == b.id
    assert store.content.get(a.id).superseded_by == b.id


def test_mark_completed(store, clock):
    a = ok(store)
    store.content.mark_completed(a.id)
    t = store.content.get(a.id).completed_at
    clock.advance(minutes=1)
    assert store.content.mark_completed(a.id).completed_at == t == clock() - timedelta(minutes=1)


def test_phrase_lookup(store):
    assert store.content.phrase("shh") is None
    p = ok(store, Kind.PHRASE, meta={"key": "shh"}, media={"audio": "phrases/shh.opus"})
    ok(store, Kind.PHRASE, meta={"key": "say_hi"})
    store.content.mark_served(p.id)
    assert store.content.phrase("shh").id == p.id  # phrases are never used up


def test_list_filters_and_order(store, clock):
    a = ok(store, Kind.STORY)
    clock.advance(seconds=1)
    b = store.content.add(Kind.LETTER)
    assert [i.id for i in store.content.list()] == [b.id, a.id]
    assert [i.id for i in store.content.list("letter")] == [b.id]
    assert [i.id for i in store.content.list(approval="pending")] == [b.id]
    assert len(store.content.list(limit=1)) == 1


def test_update_meta_and_media(store):
    a = store.content.add("letter", meta={"letter": "B"})
    store.content.update_meta(a.id, words=["ball"])
    store.content.set_media(a.id, {"card": "content/letter/b.png"})
    got = store.content.get(a.id)
    assert got.meta == {"letter": "B", "words": ["ball"]}
    assert got.media == {"card": "content/letter/b.png"}


def test_unknown_id(store):
    with pytest.raises(StoreError):
        store.content.get("nope")
    with pytest.raises(StoreError):
        store.content.approve("nope")
    assert store.content.find("nope") is None


def test_purge_needs_confirm_and_removes_media(store):
    store.write_media("content/story/x.opus", b"audio")
    a = ok(store, media={"audio": "content/story/x.opus"})
    with pytest.raises(StoreError):
        store.content.purge(a.id)
    assert store.content.find(a.id) is not None
    store.content.purge(a.id, confirm=True)
    assert store.content.find(a.id) is None
    assert not store.media_exists("content/story/x.opus")
