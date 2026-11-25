from datetime import timedelta

import pytest

from zozobox.store import RecStatus, Slot, Store, StoreError


def test_empty_slots(store):
    assert store.recordings.live(Slot.ZOZO) is None
    assert store.recordings.live("grandma_message") is None
    assert not store.recordings.has_unheard()


def test_overwrite_supersedes_and_keeps_old(store, clock):
    a = store.recordings.put(Slot.ZOZO, "rec/zozo/a.opus", duration_s=4.2)
    clock.advance(minutes=1)
    b = store.recordings.put(Slot.ZOZO, "rec/zozo/b.opus", duration_s=2.0)
    assert store.recordings.live(Slot.ZOZO).id == b.id
    old = store.recordings.get(a.id)
    assert old.status is RecStatus.SUPERSEDED and old.superseded_by == b.id
    assert [r.id for r in store.recordings.history(Slot.ZOZO)] == [b.id, a.id]


def test_slots_are_independent(store):
    z = store.recordings.put(Slot.ZOZO, "z.opus")
    g = store.recordings.put(Slot.GRANDMA, "g.opus")
    assert store.recordings.live(Slot.ZOZO).id == z.id
    assert store.recordings.live(Slot.GRANDMA).id == g.id
    assert len(store.recordings.history()) == 2


def test_withhold_is_parent_delete(store):
    g = store.recordings.put(Slot.GRANDMA, "g.opus")
    store.recordings.withhold(g.id)
    assert store.recordings.live(Slot.GRANDMA) is None
    kept = store.recordings.get(g.id)
    assert kept.status is RecStatus.WITHHELD and kept.withheld_at is not None
    assert kept.heard_at is None  # archive keeps messages he never heard


def test_late_sync_of_older_message_does_not_replace_live(store, clock):
    new = store.recordings.put(Slot.GRANDMA, "g2.opus")
    old = store.recordings.put(Slot.GRANDMA, "g1.opus", created_at=clock() - timedelta(hours=2))
    assert store.recordings.live(Slot.GRANDMA).id == new.id
    assert store.recordings.get(old.id).status is RecStatus.SUPERSEDED


def test_heard_and_published(store, clock):
    g = store.recordings.put(Slot.GRANDMA, "g.opus")
    assert store.recordings.has_unheard(Slot.GRANDMA)
    store.recordings.mark_heard(g.id)
    t = store.recordings.get(g.id).heard_at
    clock.advance(minutes=1)
    store.recordings.mark_heard(g.id)
    assert store.recordings.get(g.id).heard_at == t
    assert not store.recordings.has_unheard()
    z = store.recordings.put(Slot.ZOZO, "z.opus")
    assert store.recordings.mark_published(z.id).published_at == clock()


def test_review_ends_at(store):
    z = store.recordings.put(Slot.ZOZO, "z.opus")
    end = store.recordings.review_ends_at(z, store.settings.review_delay_s)
    assert end - z.created_at == timedelta(hours=1)


def test_validation(store):
    with pytest.raises(StoreError):
        store.recordings.put(Slot.ZOZO, "/abs.opus")
    with pytest.raises(ValueError):
        store.recordings.put("dad_message", "x.opus")
    store.recordings.put(Slot.ZOZO, "x.opus", rec_id="r1")
    with pytest.raises(StoreError):
        store.recordings.put(Slot.ZOZO, "y.opus", rec_id="r1")
    with pytest.raises(StoreError):
        store.recordings.withhold("nope")


def test_survives_restart(tmp_path, clock):
    with Store(tmp_path, clock=clock) as s:
        z = s.recordings.put(Slot.ZOZO, "z.opus", meta={"rms": 0.1})
    with Store(tmp_path, clock=clock) as s:
        got = s.recordings.live(Slot.ZOZO)
        assert got.id == z.id and got.meta == {"rms": 0.1}


def test_purge(store):
    store.write_media("rec/g.opus", b"x")
    g = store.recordings.put(Slot.GRANDMA, "rec/g.opus")
    with pytest.raises(StoreError):
        store.recordings.purge(g.id)
    store.recordings.purge(g.id, confirm=True)
    assert store.recordings.find(g.id) is None
    assert not store.media_exists("rec/g.opus")
