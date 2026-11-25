
import pytest

from zozobox.store import Store, StoreError


def test_enqueue_file_and_record(store, tmp_path):
    f = tmp_path / "a.opus"
    f.write_bytes(b"x")
    a = store.outbox.enqueue("audio/a.opus", path=f, content_type="audio/ogg")
    b = store.outbox.enqueue("index/mood/1.json", record={"label": "happy"})
    assert a.is_file and a.local_path == f.resolve()
    assert not b.is_file and b.record == {"label": "happy"}
    assert store.outbox.stats().pending == 2


def test_enqueue_validation(store):
    with pytest.raises(StoreError):
        store.outbox.enqueue("k")
    with pytest.raises(StoreError):
        store.outbox.enqueue("k", path="x", record={})
    with pytest.raises(StoreError):
        store.outbox.enqueue("/abs", record={})


def test_claim_done(store):
    store.outbox.enqueue("a", record=1)
    store.outbox.enqueue("b", record=2)
    batch = store.outbox.claim(limit=1)
    assert [i.remote_key for i in batch] == ["a"]
    assert batch[0].status == "inflight"
    assert [i.remote_key for i in store.outbox.claim()] == ["b"]
    assert store.outbox.claim() == []
    store.outbox.mark_done(batch[0].id)
    st = store.outbox.stats()
    assert (st.done, st.inflight, st.pending) == (1, 1, 0)


def test_failed_backs_off_exponentially(store, clock):
    item = store.outbox.enqueue("a", record=1)
    delays = []
    for _ in range(9):
        [c] = store.outbox.claim()
        f = store.outbox.mark_failed(c.id, "offline")
        delays.append((f.next_attempt_at - clock()).total_seconds())
        assert store.outbox.claim() == []
        clock.t = f.next_attempt_at
    assert delays[:4] == [30, 60, 120, 240]
    assert delays[-1] == 3600  # capped
    got = store.outbox.get(item.id)
    assert got.attempts == 9 and got.last_error == "offline"
    assert store.outbox.stats().failing == 1


def test_not_due_yet_is_skipped_but_other_items_go(store, clock):
    a = store.outbox.enqueue("a", record=1)
    store.outbox.claim()
    store.outbox.mark_failed(a.id, RuntimeError("boom"))
    store.outbox.enqueue("b", record=2)
    assert [i.remote_key for i in store.outbox.claim()] == ["b"]
    assert store.outbox.get(a.id).last_error == "boom"


def test_pending_same_key_coalesces(store):
    a = store.outbox.enqueue("index/settings.json", record={"v": 1})
    b = store.outbox.enqueue("index/settings.json", record={"v": 2})
    assert a.id == b.id and store.outbox.get(a.id).record == {"v": 2}
    store.outbox.claim()
    c = store.outbox.enqueue("index/settings.json", record={"v": 3})
    assert c.id != a.id  # in-flight one isn't touched


def test_survives_restart_and_recovers_inflight(tmp_path, clock):
    with Store(tmp_path, clock=clock) as s:
        s.outbox.enqueue("a", record=1)
        s.outbox.enqueue("b", record=2)
        s.outbox.claim(limit=1)  # power cut mid-upload
    with Store(tmp_path, clock=clock, recover_outbox=True) as s:  # the sync client
        assert s.outbox.stats().inflight == 0
        assert [i.remote_key for i in s.outbox.claim()] == ["a", "b"]


def test_stale_lease_is_reclaimed(store, clock):
    store.outbox.enqueue("a", record=1)
    store.outbox.claim()
    clock.advance(seconds=store.outbox.lease_s - 1)
    assert store.outbox.claim() == []
    clock.advance(seconds=2)
    assert [i.remote_key for i in store.outbox.claim()] == ["a"]


def test_prune_done(store, clock):
    a = store.outbox.enqueue("a", record=1)
    store.outbox.claim()
    store.outbox.mark_done(a.id)
    assert store.outbox.prune_done() == 0
    clock.advance(days=31)
    assert store.outbox.prune_done() == 1
    with pytest.raises(StoreError):
        store.outbox.get(a.id)


def test_pending_and_stats(store, clock):
    store.outbox.enqueue("a", record=1)
    t = clock()
    clock.advance(seconds=5)
    store.outbox.enqueue("b", record=1)
    assert [i.remote_key for i in store.outbox.pending()] == ["a", "b"]
    assert store.outbox.stats().oldest_pending_at == t


def test_unknown_ids(store):
    with pytest.raises(StoreError):
        store.outbox.mark_done(99)
    with pytest.raises(StoreError):
        store.outbox.mark_failed(99)
