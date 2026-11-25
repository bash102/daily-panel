import json
import threading
from datetime import datetime, timezone

import pytest

from zozobox.store import RecStatus, Slot
from zozobox.sync import (
    CONFIG_KEY,
    SyncClient,
    enqueue_withhold,
    load_status,
    zozo_withhold_key,
)
from zozobox.sync.api import CloudError
from zozobox.sync.state import STATE_FILE, load_state

from zozobox.sync.fake import fake_normalizer

TOKEN = "box-token-for-tests"


def save_zozo_take(store, name="a", created_at=None, data=b"RIFFzozo"):
    """What the engine does when Zozo's reply is saved."""
    rel = f"recordings/zozo/2026/11/25/{name}.wav"
    path = store.write_media(rel, data)
    rec = store.recordings.put(Slot.ZOZO, rel, duration_s=4.0, created_at=created_at)
    store.outbox.enqueue(rel, path=path, content_type="audio/wav")
    return rec


# -- config -----------------------------------------------------------------------


def test_first_pass_pushes_config(client, cloud):
    rep = client.run_once()
    assert rep.ok and rep.config_queued
    assert cloud.config == {
        "quiet_hours": {"start": "19:00", "end": "07:00"},
        "timezone": "America/New_York",
        "review_delay_s": 3600,
    }
    assert cloud.puts() == [CONFIG_KEY]
    req = cloud.requests[0]
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["content-type"] == "application/json"


def test_config_pushed_again_only_when_it_changes(client, cloud, store):
    client.run_once()
    client.run_once()
    assert cloud.puts(CONFIG_KEY) == [CONFIG_KEY]
    store.settings.set_quiet_hours("20:00", "06:30")
    store.settings.review_delay_s = 1800
    rep = client.run_once()
    assert rep.config_queued
    assert cloud.config["quiet_hours"] == {"start": "20:00", "end": "06:30"}
    assert cloud.config["review_delay_s"] == 1800
    store.settings.location = {"name": "Home", "lat": 1.0, "lon": 2.0}  # not part of the cloud config
    assert not client.run_once().config_queued


def test_config_state_survives_restart(make_client, cloud, store):
    make_client().run_once()
    assert not make_client().run_once().config_queued
    assert json.loads((store.data_dir / STATE_FILE).read_text())["last_config"]["timezone"] == "America/New_York"


# -- uploads ----------------------------------------------------------------------


def test_drains_outbox_files_and_records(client, cloud, store):
    store.write_media("prints/2026/11/25/x.png", b"\x89PNG")
    store.outbox.enqueue("prints/2026/11/25/x.png", path=store.media_path("prints/2026/11/25/x.png"),
                         content_type="image/png")
    store.outbox.enqueue("content/index/abc.json", record={"id": "abc"}, content_type="application/json")
    rep = client.run_once()
    assert rep.uploaded == 3  # + config
    assert cloud.objects["prints/2026/11/25/x.png"] == (b"\x89PNG", "image/png")
    assert cloud.json_object("content/index/abc.json") == {"id": "abc"}
    assert store.outbox.stats().pending == 0
    assert store.outbox.stats().done == 3


def test_guesses_content_type_when_missing(client, cloud, store):
    store.write_media("mood/2026/11/25/m.wav", b"RIFF")
    store.outbox.enqueue("mood/2026/11/25/m.wav", path=store.media_path("mood/2026/11/25/m.wav"))
    client.run_once()
    assert cloud.objects["mood/2026/11/25/m.wav"][1] in ("audio/wav", "audio/x-wav")


def test_offline_keeps_everything_queued_and_backs_off(client, cloud, store, clock):
    store.outbox.enqueue("content/index/a.json", record={"a": 1})
    store.outbox.enqueue("content/index/b.json", record={"b": 1})
    cloud.offline = True
    rep = client.run_once()
    assert not rep.online and not rep.ok
    # only the first item was actually tried; the rest go back untouched
    assert rep.uploaded == 0 and rep.failed == 1 and rep.released == 2
    assert len(cloud.requests) == 0  # FakeCloud logs only answered requests
    st = store.outbox.stats()
    assert st.pending == 3 and st.failing == 1 and st.inflight == 0
    # the tried item backed off; the untried ones are due right away
    cloud.offline = False
    assert client.run_once().uploaded == 2
    clock.advance(seconds=31)
    rep = client.run_once()
    assert rep.ok and rep.uploaded == 1
    assert load_status(store.data_dir).online is True


def test_offline_passes_do_not_grow_backoff_of_untried_items(client, cloud, store, clock):
    client.run_once()
    cloud.offline = True
    store.outbox.enqueue("content/index/a.json", record={"a": 1})
    store.outbox.enqueue("content/index/b.json", record={"b": 1})
    for _ in range(20):
        clock.advance(minutes=10)
        client.run_once()
    # at most one real try per offline pass; untried items are not penalized
    assert sum(i.attempts for i in store.outbox.pending()) <= 20
    cloud.offline = False
    clock.advance(minutes=1)
    client.run_once()
    clock.advance(hours=1)
    client.run_once()
    assert "content/index/a.json" in cloud.objects and "content/index/b.json" in cloud.objects


def test_server_error_retries_later(client, cloud, store, clock):
    client.run_once()  # config out of the way
    store.outbox.enqueue("content/index/a.json", record={"a": 1})
    cloud.fail_next(503)
    rep = client.run_once()
    assert rep.failed == 1 and rep.online
    item = store.outbox.pending()[0]
    assert "503" in item.last_error and item.attempts == 1
    clock.advance(seconds=31)
    assert client.run_once().uploaded == 1


def test_bad_request_is_reported_and_kept(client, cloud, store, clock):
    client.run_once()
    store.outbox.enqueue("not-allowed/x.json", record={"a": 1})
    rep = client.run_once()
    assert rep.failed == 1 and not rep.ok
    assert any("not-allowed/x.json" in e for e in rep.errors)
    assert store.outbox.stats().pending == 1


def test_auth_error_stops_the_batch(make_client, cloud, store):
    store.outbox.enqueue("content/index/a.json", record={"a": 1})
    store.outbox.enqueue("content/index/b.json", record={"b": 1})
    client = SyncClient(store, cloud.base_url, "wrong-token", transport=cloud,
                        normalizer=None)
    rep = client.run_once()
    assert not rep.ok
    assert len([r for r in cloud.requests if r.method == "PUT"]) == 1
    assert store.outbox.stats().pending == 3


def test_missing_local_file_is_closed_out(client, cloud, store, tmp_path):
    ghost = tmp_path / "ghost.wav"
    store.outbox.enqueue("mood/2026/11/25/ghost.wav", path=ghost, content_type="audio/wav")
    rep = client.run_once()
    assert rep.skipped_missing == 1
    assert store.outbox.stats().pending == 0


def test_respects_max_uploads_per_run(make_client, cloud, store):
    for i in range(7):
        store.outbox.enqueue(f"content/index/{i}.json", record={"i": i})
    c = make_client(batch_size=2, max_uploads_per_run=5)
    assert c.run_once().uploaded == 5
    assert c.run_once().uploaded == 3


# -- Zozo replies -------------------------------------------------------------------


def test_announces_a_new_zozo_reply_after_its_audio(client, cloud, store, clock):
    rec = save_zozo_take(store, created_at=datetime(2026, 11, 25, 14, 59, 30, 123456, tzinfo=timezone.utc))
    rep = client.run_once()
    assert rep.announced == [rec.id]
    puts = cloud.puts()
    assert puts.index(rec.media) < puts.index(f"control/zozo/{rec.id}.json")
    cmd = cloud.json_object(f"control/zozo/{rec.id}.json")
    assert cmd == {
        "action": "new",
        "rec_id": rec.id,
        "media_key": rec.media,
        "recorded_at": "2026-11-25T14:59:30.123Z",
        "duration_s": 4.0,
    }
    assert cloud.zozo[rec.id].status == "pending"
    # announced once only
    assert client.run_once().announced == []
    assert cloud.puts("control/zozo/") == [f"control/zozo/{rec.id}.json"]


def test_superseded_and_withheld_replies_are_not_announced(client, cloud, store, clock):
    old = save_zozo_take(store, "old")
    clock.advance(minutes=1)
    new = save_zozo_take(store, "new")
    assert store.recordings.get(old.id).status is RecStatus.SUPERSEDED
    rep = client.run_once()
    assert rep.announced == [new.id]

    clock.advance(minutes=1)
    third = save_zozo_take(store, "third")
    store.recordings.withhold(third.id)
    enqueue_withhold(store, third.id)  # what the dashboard does
    rep = client.run_once()
    assert rep.announced == []
    assert cloud.puts("control/zozo/") == [f"control/zozo/{new.id}.json", zozo_withhold_key(third.id)]
    assert cloud.zozo[third.id].status == "withheld"


def test_dashboard_withhold_after_announce(client, cloud, store):
    rec = save_zozo_take(store)
    client.run_once()
    store.recordings.withhold(rec.id)
    enqueue_withhold(store, rec.id)
    client.run_once()
    assert cloud.zozo[rec.id].status == "withheld"
    assert cloud.json_object(zozo_withhold_key(rec.id)) == {"action": "withhold", "rec_id": rec.id}


def test_reply_withheld_while_offline_is_never_announced(client, cloud, store, clock):
    """Reviewer repro: recorded offline, withheld locally 40 min later, box back
    3 h later. The cloud must never get the "new" command, and the withhold
    must go out on the first pass back online."""
    client.run_once()  # config out of the way
    cloud.offline = True
    rec = save_zozo_take(store)  # 15:00
    for _ in range(4):  # offline passes queue and try the "new" command
        clock.advance(minutes=10)
        client.run_once()
    store.recordings.withhold(rec.id)  # ~15:40, what the dashboard does
    enqueue_withhold(store, rec.id)
    for _ in range(18):  # 3 more hours offline
        clock.advance(minutes=10)
        client.run_once()
    cloud.offline = False
    clock.advance(minutes=5)
    rep = client.run_once()
    assert rep.online and zozo_withhold_key(rec.id) in cloud.puts()
    assert f"control/zozo/{rec.id}.json" not in cloud.puts()
    assert cloud.zozo[rec.id].status == "withheld"
    assert cloud.zozo[rec.id].media_key is None  # never registered as "new"
    # drain everything that is still backing off: the "new" command never goes up
    for _ in range(12):
        clock.advance(minutes=10)
        client.run_once()
    assert f"control/zozo/{rec.id}.json" not in cloud.puts()
    assert store.outbox.stats().pending == 0


def test_withhold_goes_first_and_ignores_its_backoff(client, cloud, store, clock):
    rec = save_zozo_take(store)
    client.run_once()  # announced and uploaded
    cloud.offline = True
    store.recordings.withhold(rec.id)
    enqueue_withhold(store, rec.id)
    for i in range(30):  # a long outage: the withhold is tried (and fails) each pass
        clock.advance(minutes=10)
        store.outbox.enqueue(f"content/index/{i}.json", record={"i": i})
        client.run_once()
    w = [i for i in store.outbox.pending() if i.remote_key == zozo_withhold_key(rec.id)][0]
    assert w.next_attempt_at > clock()  # backed off ...
    cloud.offline = False
    cloud.requests.clear()
    client.run_once()  # ... but sent on the first pass back, before anything else
    assert cloud.puts()[0] == zozo_withhold_key(rec.id)
    assert cloud.zozo[rec.id].status == "withheld"


def test_new_command_for_purged_reply_is_dropped(client, cloud, store):
    cloud.offline = True
    rec = save_zozo_take(store)
    client.run_once()
    store.recordings.purge(rec.id, confirm=True)
    cloud.offline = False
    rep = client.run_once()
    assert f"control/zozo/{rec.id}.json" not in cloud.puts()
    assert f"control/zozo/{rec.id}.json" in rep.dropped_withheld


def test_marks_published_replies(client, cloud, store):
    rec = save_zozo_take(store)
    client.run_once()
    cloud.publish(rec.id, "2026-11-25T16:00:00.000Z")
    rep = client.run_once()
    assert rep.published == [rec.id]
    assert store.recordings.get(rec.id).published_at == datetime(2026, 11, 25, 16, tzinfo=timezone.utc)
    assert client.run_once().published == []


# -- grandma ----------------------------------------------------------------------------


def test_imports_grandma_messages_into_the_live_slot(make_client, cloud, store):
    calls = []
    client = make_client(normalizer=fake_normalizer(calls))
    cloud.add_grandma("RE1", b"one", "2026-11-24T12:00:00.000Z", 5.0)
    cloud.add_grandma("RE2", b"two", "2026-11-25T12:00:00.000Z", 7.5)
    rep = client.run_once()
    assert rep.imported == ["RE1", "RE2"]
    assert len(calls) == 2
    live = store.recordings.live(Slot.GRANDMA)
    assert live.id == "RE2"
    assert live.media == "recordings/grandma/2026/11/25/RE2.wav"
    assert live.created_at == datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
    assert live.duration_s == 7.5
    assert live.meta["source"] == "cloud"
    assert store.media_path(live.media).read_bytes() == b"NORM:two"
    assert store.recordings.get("RE1").status is RecStatus.SUPERSEDED
    assert store.recordings.has_unheard(Slot.GRANDMA)
    # second pass: nothing new, asks with the watermark
    assert client.run_once().imported == []
    last = [r for r in cloud.requests if r.path == "/api/grandma/messages"][-1]
    assert last.query["since"] == "2026-11-25T12:00:00.000Z"
    ev = store.events.query(types=["recording"])
    assert [(e.payload["rec_id"], e.payload["source"], e.media) for e in ev] == [
        ("RE1", "cloud", "recordings/grandma/2026/11/24/RE1.wav"),
        ("RE2", "cloud", "recordings/grandma/2026/11/25/RE2.wav"),
    ]


def test_new_grandma_message_supersedes_the_heard_one(client, cloud, store):
    cloud.add_grandma("RE1", b"one", "2026-11-25T12:00:00.000Z")
    client.run_once()
    store.recordings.mark_heard("RE1")
    assert not store.recordings.has_unheard(Slot.GRANDMA)
    cloud.add_grandma("RE2", b"two", "2026-11-25T13:00:00.000Z")
    client.run_once()
    assert store.recordings.live(Slot.GRANDMA).id == "RE2"
    assert store.recordings.has_unheard(Slot.GRANDMA)


def test_grandma_paging(make_client, cloud, store):
    for i in range(5):
        cloud.add_grandma(f"RE{i}", b"x", f"2026-11-25T1{i}:00:00.000Z")
    rep = make_client(grandma_page=2).run_once()
    assert rep.imported == [f"RE{i}" for i in range(5)]


def test_grandma_download_failure_keeps_watermark(client, cloud, store):
    cloud.add_grandma("RE1", b"one", "2026-11-25T12:00:00.000Z")
    cloud.add_grandma("RE2", b"two", "2026-11-25T13:00:00.000Z")
    del cloud.objects["recordings/grandma/2026/11/25/RE2.wav"]
    rep = client.run_once()
    assert rep.imported == ["RE1"] and not rep.ok
    assert load_state(store.data_dir / STATE_FILE).grandma_since == "2026-11-25T12:00:00.000Z"
    cloud.objects["recordings/grandma/2026/11/25/RE2.wav"] = (b"two", "audio/wav")
    assert client.run_once().imported == ["RE2"]


def test_rejects_grandma_keys_outside_the_grandma_prefix(client, cloud, store):
    cloud.add_grandma("RE1", b"x", "2026-11-25T12:00:00.000Z", key="prints/evil.wav")
    rep = client.run_once()
    assert rep.imported == [] and not rep.ok
    assert store.recordings.live(Slot.GRANDMA) is None


def test_normalizer_failure_keeps_original_audio(make_client, cloud, store):
    def broken(src, dst):
        raise RuntimeError("ffmpeg exploded")

    cloud.add_grandma("RE1", b"raw", "2026-11-25T12:00:00.000Z")
    rep = make_client(normalizer=broken).run_once()
    assert rep.imported == ["RE1"]
    assert store.media_path(store.recordings.get("RE1").media).read_bytes() == b"raw"


def test_no_normalizer_stores_raw(make_client, cloud, store):
    cloud.add_grandma("RE1", b"raw", "2026-11-25T12:00:00.000Z")
    make_client(normalizer=None).run_once()
    assert store.media_path(store.recordings.get("RE1").media).read_bytes() == b"raw"


def test_grandma_messages_already_in_the_store_are_skipped(client, cloud, store):
    cloud.add_grandma("RE1", b"x", "2026-11-25T12:00:00.000Z")
    client.run_once()
    (store.data_dir / STATE_FILE).unlink()  # lost state: re-lists everything
    c2 = SyncClient(store, cloud.base_url, TOKEN, transport=cloud, normalizer=None)
    assert c2.run_once().imported == []


def test_purged_grandma_message_never_comes_back(client, cloud, store):
    cloud.add_grandma("RE1", b"one", "2026-11-25T12:00:00.000Z")
    cloud.add_grandma("RE2", b"two", "2026-11-25T13:00:00.000Z")
    assert client.run_once().imported == ["RE1", "RE2"]
    store.recordings.purge("RE2", confirm=True)
    rep = client.run_once()
    assert rep.imported == [] and rep.ok
    assert store.recordings.find("RE2") is None
    assert store.recordings.live(Slot.GRANDMA) is None


def test_purged_grandma_messages_stay_gone_after_state_loss(client, cloud, store):
    cloud.add_grandma("RE1", b"one", "2026-11-25T12:00:00.000Z")
    cloud.add_grandma("RE2", b"two", "2026-11-25T13:00:00.000Z")
    client.run_once()
    store.recordings.purge("RE1", confirm=True)
    store.recordings.purge("RE2", confirm=True)
    (store.data_dir / STATE_FILE).write_text("{corrupt")
    c2 = SyncClient(store, cloud.base_url, TOKEN, transport=cloud, normalizer=None)
    assert c2.run_once().imported == []
    assert store.recordings.find("RE1") is None and store.recordings.find("RE2") is None
    cloud.add_grandma("RE3", b"three", "2026-11-25T14:00:00.000Z")
    assert c2.run_once().imported == ["RE3"]


def test_permanently_missing_grandma_message_is_skipped_after_retries(client, cloud, store):
    cloud.add_grandma("RE1", b"one", "2026-11-25T12:00:00.000Z")
    cloud.add_grandma("RE2", b"two", "2026-11-25T13:00:00.000Z")
    cloud.add_grandma("RE3", b"three", "2026-11-25T14:00:00.000Z")
    del cloud.objects["recordings/grandma/2026/11/25/RE2.wav"]  # 404 forever
    assert client.run_once().imported == ["RE1"]
    assert client.run_once().imported == []
    rep = client.run_once()  # third try: give up on RE2, carry on
    assert rep.grandma_skipped == ["RE2"] and rep.imported == ["RE3"]
    assert any("skipped RE2" in e for e in rep.errors)
    assert store.recordings.live(Slot.GRANDMA).id == "RE3"
    rep = client.run_once()
    assert rep.ok and rep.imported == [] and rep.grandma_skipped == []
    st = load_state(store.data_dir / STATE_FILE)
    assert st.grandma_skipped == ["RE2"] and st.grandma_failures == {}
    cloud.add_grandma("RE4", b"four", "2026-11-25T15:00:00.000Z")
    assert client.run_once().imported == ["RE4"]


def test_bad_grandma_key_is_skipped_after_retries(client, cloud, store):
    cloud.add_grandma("RE1", b"x", "2026-11-25T12:00:00.000Z", key="prints/evil.wav")
    cloud.add_grandma("RE2", b"ok", "2026-11-25T13:00:00.000Z")
    client.run_once()
    client.run_once()
    rep = client.run_once()
    assert rep.grandma_skipped == ["RE1"] and rep.imported == ["RE2"]


def test_empty_grandma_download_is_skipped_after_retries(client, cloud, store):
    cloud.add_grandma("RE1", b"", "2026-11-25T12:00:00.000Z")
    cloud.add_grandma("RE2", b"ok", "2026-11-25T13:00:00.000Z")
    reps = [client.run_once() for _ in range(3)]
    assert reps[-1].grandma_skipped == ["RE1"] and reps[-1].imported == ["RE2"]


def test_grandma_server_errors_are_retried_forever(client, cloud, store):
    cloud.add_grandma("RE1", b"one", "2026-11-25T12:00:00.000Z")
    real = client.api.get_object

    def flaky(key):
        raise CloudError("503", status=503)

    client.api.get_object = flaky
    for _ in range(10):
        rep = client.run_once()
        assert rep.grandma_skipped == [] and rep.imported == []
    client.api.get_object = real
    assert client.run_once().imported == ["RE1"]


# -- robustness / status ------------------------------------------------------------------


def test_corrupt_state_file_starts_fresh(store, cloud):
    (store.data_dir / STATE_FILE).write_text("{nope")
    c = SyncClient(store, cloud.base_url, TOKEN, transport=cloud, normalizer=None)
    assert c.run_once().ok


def test_status_for_dashboard(client, cloud, store, clock):
    client.run_once()
    st = load_status(store.data_dir)
    assert st.online is True and st.last_ok_at == "2026-11-25T15:00:00.000Z" and st.last_error is None
    cloud.offline = True
    store.outbox.enqueue("content/index/a.json", record={"a": 1})
    clock.advance(minutes=1)
    client.run_once()
    st = load_status(store.data_dir)
    assert st.online is False and st.last_ok_at == "2026-11-25T15:00:00.000Z"
    assert "unreachable" in st.last_error


def test_unexpected_exception_in_a_step_is_contained(client, cloud, store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db on fire")

    monkeypatch.setattr(store.recordings, "history", boom)
    rep = client.run_once()
    assert not rep.ok and any("announce" in e for e in rep.errors)
    assert CONFIG_KEY in cloud.objects  # other steps still ran


def test_token_never_in_repr_or_status(client, cloud, store):
    cloud.offline = True
    client.run_once()
    assert TOKEN not in repr(client) and TOKEN not in repr(client.api)
    assert TOKEN not in (store.data_dir / STATE_FILE).read_text()


def test_requires_url_and_token(store, cloud):
    with pytest.raises(ValueError):
        SyncClient(store, "", TOKEN, transport=cloud)
    with pytest.raises(ValueError):
        SyncClient(store, cloud.base_url, "", transport=cloud)


# -- loop ----------------------------------------------------------------------------------


def test_run_loop_backs_off_while_offline(client, cloud):
    cloud.offline = True
    waits = []
    client.run(interval_s=30, max_backoff_s=200, max_passes=5, wait=waits.append)
    assert waits == [60, 120, 200, 200]


def test_run_loop_survives_thousands_of_failed_passes(client, cloud, monkeypatch):
    from zozobox.sync.client import SyncReport

    monkeypatch.setattr(client, "run_once", lambda: SyncReport(online=False))
    waits = []
    client.run(interval_s=30, max_backoff_s=600, max_passes=1100, wait=waits.append)
    assert len(waits) == 1099 and waits[-1] == 600 and max(waits) == 600


def test_run_loop_normal_interval_and_stop(client, cloud):
    stop = threading.Event()
    waits = []

    def wait(s):
        waits.append(s)
        if len(waits) == 3:
            stop.set()

    client.run(stop, interval_s=30, wait=wait)
    assert waits == [30, 30, 30]


def test_run_loop_recovers_after_outage(client, cloud):
    waits = []

    def wait(s):
        waits.append(s)
        cloud.offline = len(waits) < 2

    cloud.offline = True
    client.run(interval_s=10, max_passes=4, wait=wait)
    assert waits == [20, 40, 10]


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")
def test_real_ffmpeg_normalization_of_a_twilio_style_wav(make_client, cloud, store):
    import io
    import math
    import struct
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:  # 8 kHz mono, like Twilio recordings
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(i / 8000 * 2 * math.pi * 440)))
                               for i in range(8000)))
    cloud.add_grandma("RE1", buf.getvalue(), "2026-11-25T12:00:00.000Z", 1.0)
    rep = make_client(normalizer="auto").run_once()
    assert rep.imported == ["RE1"]
    with wave.open(str(store.media_path(store.recordings.get("RE1").media))) as w:
        assert w.getframerate() == 48000 and w.getnchannels() == 1
