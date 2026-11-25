import io
from datetime import date, timedelta

import pytest

from zozobox.store import EventType, StoreError

PIL = pytest.importorskip("PIL.Image")

PNG = b"\x89PNG\r\n\x1a\n" + b"rest-of-file"


def test_append_and_query(store, clock):
    e1 = store.events.append("press", {"button": "red"})
    clock.advance(seconds=1)
    e2 = store.events.append("play", {"what": "story"}, media="content/s.opus")
    assert e1.id < e2.id
    assert e1.ts == clock() - timedelta(seconds=1)
    assert e1.day == store.box_day()
    assert [e.id for e in store.events.query()] == [e1.id, e2.id]
    assert [e.id for e in store.events.query("play")] == [e2.id]
    assert [e.id for e in store.events.query(["press", "play"], newest_first=True)] == [e2.id, e1.id]
    assert store.events.get(e2.id).media == "content/s.opus"


def test_query_by_time_and_day_range(store, clock):
    ids = []
    for _ in range(3):
        ids.append(store.events.append("press").id)
        clock.advance(days=1)
    d0 = date(2026, 3, 10)
    assert [e.id for e in store.events.query(day_from=d0 + timedelta(days=1))] == ids[1:]
    assert [e.id for e in store.events.query(day_from=d0, day_to=d0)] == ids[:1]
    t1 = store.events.get(ids[1]).ts
    assert [e.id for e in store.events.query(since=t1)] == ids[1:]
    assert [e.id for e in store.events.query(until=t1)] == ids[:1]
    assert [e.id for e in store.events.query(after_id=ids[0], limit=1)] == [ids[1]]
    assert store.events.count("press", day=d0) == 1


def test_event_type_validation(store):
    with pytest.raises(StoreError):
        store.events.append("")
    with pytest.raises(StoreError):
        store.events.append("x", media="/abs/path")


def test_backdated_event_lands_on_its_own_day(store, clock):
    t0 = clock()
    clock.advance(days=2)
    e = store.events.append("mood", {"label": "happy"}, ts=t0)
    assert e.day == date(2026, 3, 10)


def test_record_print_bytes(store, clock):
    clock.set_local(2026, 3, 10, 9, 5, 7)
    ev = store.events.record_print("letter", PNG, content_id="abc")
    assert ev.type == EventType.PRINT
    assert ev.media.startswith("prints/2026/03/10/090507")
    assert ev.payload == {"item": "letter", "reprint": False, "content_id": "abc"}
    assert store.media_path(ev.media).read_bytes() == PNG
    # copy queued for the archive
    [ob] = store.outbox.claim()
    assert ob.remote_key == ev.media and ob.local_path == store.media_path(ev.media).resolve()


def test_record_print_pil_image_and_reprints_distinct(store):
    img = PIL.new("1", (696, 100), 1)
    a = store.events.record_print("coloring", img)
    b = store.events.record_print("coloring", img, reprint=True)
    c = store.events.record_print("coloring", img, reprint=True)
    assert len({a.media, b.media, c.media}) == 3
    back = PIL.open(io.BytesIO(store.media_path(a.media).read_bytes()))
    assert back.size == (696, 100)
    assert len(store.events.prints()) == 3
    assert [e.id for e in store.events.prints(include_reprints=False)] == [a.id]


def test_record_print_rejects_non_png(store):
    with pytest.raises(StoreError):
        store.events.record_print("letter", b"GIF89a....")
    with pytest.raises(StoreError):
        store.events.record_print("letter", 42)


def test_print_dir_uses_local_date(store, clock):
    # 01:00 local on Mar 11 is still box day Mar 10, but the folder is the calendar date.
    clock.set_local(2026, 3, 11, 1, 0)
    ev = store.events.record_print("mood", PNG)
    assert ev.media.startswith("prints/2026/03/11/")
    assert ev.day == date(2026, 3, 10)


def test_upload_prints_can_be_disabled(tmp_path, clock):
    from zozobox.store import Store

    with Store(tmp_path, clock=clock, upload_prints=False) as s:
        s.events.record_print("letter", PNG)
        assert s.outbox.stats().pending == 0


def test_moods_and_first_mood(store, clock):
    assert store.events.first_mood() is None
    m1 = store.events.record_mood("silent", source="online")
    clock.advance(minutes=5)
    m2 = store.events.record_mood("sad", transcript="sad", source="online", audio="moods/2.opus")
    assert store.events.first_mood().id == m1.id
    assert store.events.first_mood(recognized_only=True).id == m2.id
    with pytest.raises(StoreError):
        store.events.record_mood("ecstatic")


def test_offline_mood_recognized_later_keeps_original_time(store, clock):
    t_rec = clock()
    store.events.append(EventType.MOOD_PENDING, {"audio": "moods/1.opus"}, ts=t_rec)
    clock.advance(minutes=30)
    store.events.record_mood("happy", source="online")  # a later run while online
    clock.advance(hours=3)
    late = store.events.record_mood("mad", transcript="mad", source="offline", ts=t_rec)
    assert store.events.first_mood().id == late.id
    assert store.events.daily_moods(date(2026, 3, 10), date(2026, 3, 10)) == {
        date(2026, 3, 10): "mad"
    }


def test_no_update_or_delete_api(store):
    assert not hasattr(store.events, "delete")
    assert not hasattr(store.events, "update")
