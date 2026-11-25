from datetime import timedelta

import pytest

from zozobox.store import RecStatus, Slot


@pytest.fixture
def items(dash):
    return dash.populate()


def test_home_with_populated_store(dash, client, items):
    r = client.get("/")
    assert r.status_code == 200
    t = r.text
    assert "Tue Mar 10" in t
    assert "Next up" in t and "letter ✓" in t  # printed today
    assert "Story heard today: yes" in t
    assert "Sunny and cold" in t  # today's weather
    assert "$2.00 of $30.00" in t  # month-to-date spend, last month excluded
    assert "anthropic $1.25" in t
    assert "too scary" in t  # safety failure
    assert "in review" in t and "1 h left" in t
    assert "not heard yet" in t  # grandma unheard
    assert "waiting" in t and "Media" in t and "Free" in t
    assert "2 item(s) waiting" in t  # story + coloring; the safety-failed one is excluded


def test_home_spend_cap_alert(dash, client, items):
    dash.store.settings.spend_cap_usd = 1.0
    assert "Monthly spend cap reached" in client.get("/").text


def test_home_contains_store_failures(dash, client, items, monkeypatch):
    def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(dash.store.outbox, "stats", boom)
    monkeypatch.setattr(dash.store, "disk_usage", boom)
    r = client.get("/")
    assert r.status_code == 200
    assert "Unavailable." in r.text


def test_empty_store_pages_render(client):
    for p in ("/", "/approvals", "/recordings", "/archive", "/mood", "/stats", "/reprint",
              "/settings"):
        assert client.get(p).status_code == 200, p


# -- approvals --------------------------------------------------------------------------


def test_approvals_page_lists_pending(client, items):
    t = client.get("/approvals").text
    assert "Zozo and the Kind Cloud" in t
    assert "Once upon a time Zozo shared." in t
    assert '<audio controls' in t and items["story"].media["chunk_0"] in t
    assert '<img class="preview"' in t
    assert f"/approvals/{items['story'].id}/approve" in t


def test_approve_and_reject(dash, client, items):
    r = client.post(f"/approvals/{items['story'].id}/approve", page="/approvals")
    assert r.status_code == 303 and "msg=approved" in r.headers["location"]
    it = dash.store.content.get(items["story"].id)
    assert it.approval.value == "approved" and it.servable
    r = client.post(f"/approvals/{items['coloring'].id}/reject", page="/approvals")
    assert r.status_code == 303
    assert dash.store.content.get(items["coloring"].id).approval.value == "rejected"
    t = client.get("/approvals").text
    assert "Zozo and the Kind Cloud" not in t and "A happy whale" not in t
    types = [e.type for e in dash.store.events.query(["dashboard_approve", "dashboard_reject"])]
    assert types == ["dashboard_approve", "dashboard_reject"]


def test_cannot_approve_safety_failed(dash, client, items):
    assert "Scary one" not in client.get("/approvals").text
    r = client.post(f"/approvals/{items['failed'].id}/approve")
    assert r.status_code == 303 and "msg=error" in r.headers["location"]
    assert dash.store.content.get(items["failed"].id).approval.value == "pending"


def test_approve_unknown_and_bad_action(client):
    r = client.post("/approvals/nope/approve")
    assert r.status_code == 303 and "not_found" in r.headers["location"]
    assert client.post("/approvals/nope/delete").status_code == 404


# -- archive -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["story", "letter", "weather", "coloring", "phrase", "prints",
                                  "recordings", "mood"])
def test_archive_tabs(client, items, kind):
    r = client.get(f"/archive?kind={kind}")
    assert r.status_code == 200


def test_archive_content_by_day(client, items):
    t = client.get("/archive?kind=story&day=2026-03-10").text
    assert "Zozo and the Kind Cloud" in t
    t = client.get("/archive?kind=story&day=2026-03-01").text
    assert "Zozo and the Kind Cloud" not in t and "Nothing here" in t
    assert client.get("/archive?kind=story&day=garbage").status_code == 200
    assert client.get("/archive?kind=bogus").status_code == 200


def test_archive_prints_and_recordings(client, items):
    t = client.get("/archive?kind=prints").text
    assert items["print"].media in t and f"/reprint/{items['print'].id}" in t
    t = client.get("/archive?kind=recordings").text
    assert items["zozo"].media in t and items["grandma"].media in t
    t = client.get("/archive?kind=mood").text
    assert "I&#39;m happy!" in t and "pending" in t


def test_archive_item_detail(client, items):
    t = client.get(f"/archive/item/{items['failed'].id}").text
    assert "too scary" in t and "Type PURGE" in t
    assert client.get("/archive/item/nope").status_code == 404


def test_purge_requires_typed_confirmation(dash, client, items):
    sid = items["coloring"].id
    art = dash.store.media_path(items["coloring"].media["art"])
    for word in ("", "purge please", "yes"):
        r = client.post("/archive/purge", {"target": "content", "item_id": sid, "confirm": word})
        assert r.status_code == 303 and "purge_confirm" in r.headers["location"]
        assert dash.store.content.find(sid) is not None and art.exists()
    r = client.post("/archive/purge", {"target": "content", "item_id": sid, "confirm": "PURGE"})
    assert r.status_code == 303 and "purged" in r.headers["location"]
    assert dash.store.content.find(sid) is None and not art.exists()


def test_purge_recording(dash, client, items):
    gid = items["grandma"].id
    r = client.post("/archive/purge", {"target": "recording", "item_id": gid, "confirm": "nope"})
    assert dash.store.recordings.find(gid) is not None
    r = client.post("/archive/purge", {"target": "recording", "item_id": gid, "confirm": "PURGE"})
    assert r.status_code == 303
    assert dash.store.recordings.find(gid) is None
    assert client.post("/archive/purge", {"target": "event", "item_id": "1",
                                          "confirm": "PURGE"}).status_code == 404


# -- recordings ---------------------------------------------------------------------------


def test_recordings_page_review_window(dash, client, items):
    t = client.get("/recordings").text
    assert "In review" in t and "1 h left" in t
    end = (items["zozo"].created_at + timedelta(seconds=3600)).isoformat()
    assert f'data-ends="{end}"' in t
    dash.clock.advance(minutes=45)
    assert "15 min left" in client.get("/recordings").text
    dash.clock.advance(minutes=14, seconds=30)
    assert "1 min left" in client.get("/recordings").text
    dash.clock.advance(minutes=1)
    t = client.get("/recordings").text
    assert "review window is over but the cloud hasn't reported publishing" in t
    dash.store.recordings.mark_published(items["zozo"].id)
    assert "Published to Grandma's line" in client.get("/recordings").text
    assert "published to Grandma" in client.get("/").text


def test_review_window_uses_review_delay(dash, client, items):
    dash.store.settings.review_delay_s = 7200
    assert "2 h left" in client.get("/recordings").text


def test_withhold_zozo_enqueues_control_record(dash, client, items):
    rid = items["zozo"].id
    r = client.post(f"/recordings/{rid}/withhold", page="/recordings")
    assert r.status_code == 303 and "withheld_zozo" in r.headers["location"]
    rec = dash.store.recordings.get(rid)
    assert rec.status is RecStatus.WITHHELD
    assert dash.store.recordings.live(Slot.ZOZO) is None
    ctl = [i for i in dash.store.outbox.pending() if i.remote_key.startswith("control/")]
    assert len(ctl) == 1
    assert ctl[0].remote_key == f"control/zozo/{rid}-withhold.json"
    assert ctl[0].record == {"action": "withhold", "rec_id": rid}
    assert ctl[0].content_type == "application/json" and not ctl[0].is_file
    assert dash.store.events.query("dashboard_withhold")[0].payload == {
        "rec_id": rid, "slot": "zozo_message"}
    t = client.get("/recordings").text
    assert "No live message from Zozo" in t and "withheld" in t


def test_withhold_after_review_window_still_sends_control(dash, client, items):
    dash.clock.advance(hours=3)
    rid = items["zozo"].id
    client.post(f"/recordings/{rid}/withhold")
    keys = [i.remote_key for i in dash.store.outbox.pending()]
    assert f"control/zozo/{rid}-withhold.json" in keys


def test_withhold_superseded_zozo_message(dash, client, items):
    old = items["zozo"]
    dash.clock.advance(minutes=5)
    rel = dash.media("recordings/zozo/2026/03/10/new.wav", b"RIFF")
    dash.store.recordings.put(Slot.ZOZO, rel)
    assert dash.store.recordings.get(old.id).status is RecStatus.SUPERSEDED
    client.post(f"/recordings/{old.id}/withhold")
    assert dash.store.recordings.get(old.id).status is RecStatus.WITHHELD
    assert any(i.remote_key == f"control/zozo/{old.id}-withhold.json"
               for i in dash.store.outbox.pending())


def test_withhold_grandma_is_local_only(dash, client, items):
    gid = items["grandma"].id
    before = {i.remote_key for i in dash.store.outbox.pending()}
    r = client.post(f"/recordings/{gid}/withhold")
    assert r.status_code == 303 and "withheld_grandma" in r.headers["location"]
    assert dash.store.recordings.get(gid).status is RecStatus.WITHHELD
    assert not dash.store.recordings.has_unheard(Slot.GRANDMA)
    after = {i.remote_key for i in dash.store.outbox.pending()}
    assert after == before


def test_withhold_unknown_or_bad_id(dash, client, items):
    for rid in ("nope", "bad%20id", "bad-id", "a" * 101):
        r = client.post(f"/recordings/{rid}/withhold")
        assert r.status_code == 303 and "not_found" in r.headers["location"]
    assert not [i for i in dash.store.outbox.pending() if i.remote_key.startswith("control/")]


def test_withhold_is_atomic(dash, client, items, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(dash.store.outbox, "enqueue", boom)
    with pytest.raises(RuntimeError):
        from zozobox.dashboard import withhold_recording

        withhold_recording(dash.store, items["zozo"].id)
    assert dash.store.recordings.get(items["zozo"].id).status is RecStatus.LIVE


# -- mood / stats ---------------------------------------------------------------------------


def test_mood_log(client, items):
    t = client.get("/mood").text
    assert "<b>unknown</b>" in t  # first mood of the day
    assert "first recognized: happy" in t
    assert "I&#39;m happy!" in t and "blah" in t
    assert "waiting for recognition" in t
    assert "mood0002.wav" in t
    assert "Last 30 days" in t
    assert 'style="width:100%"' in t  # unknown = 1 of 1 first moods
    assert client.get("/mood?days=7").status_code == 200
    assert client.get("/mood?days=junk").status_code == 200


def test_mood_pending_resolved_hidden(dash, client, items):
    ev = dash.store.events.query("mood_pending")[0]
    dash.store.events.record_mood("sad", transcript="sad", source="offline", audio=ev.media,
                                  ts=ev.ts)
    assert "waiting for recognition" not in client.get("/mood").text


def test_stats(dash, client, items):
    from zozobox.dashboard import queries as Q

    rows, cols = Q.stats(dash.store, 7)
    assert cols[:3] == ["print", "grandma", "story"]
    today = rows[0]
    assert today.presses == {"print": 2, "grandma": 1, "story": 1}  # ignored excluded
    assert today.prints == 1 and today.reprints == 0 and today.story_heard
    assert len(rows) == 7
    t = client.get("/stats?days=7").text
    assert "Tue Mar 10" in t and "<th>print</th>" in t


# -- reprint ---------------------------------------------------------------------------------


def test_reprint_page(client, items):
    t = client.get("/reprint").text
    assert f"/reprint/{items['print'].id}" in t and items["print"].media in t


def test_reprint_prints_and_logs_without_daily_limit(dash, client, items):
    snap_before = dash.store.daily.snapshot()
    r = client.post(f"/reprint/{items['print'].id}", page="/reprint")
    assert r.status_code == 303 and "msg=reprinted" in r.headers["location"]
    assert dash.printer.count == 1
    prints = dash.store.events.prints()
    assert len(prints) == 2
    re_ev = prints[-1]
    assert re_ev.payload["reprint"] is True and re_ev.payload["item"] == "letter"
    assert re_ev.payload["source_event"] == items["print"].id and re_ev.payload["letter"] == "B"
    assert re_ev.media != items["print"].media and re_ev.media.endswith("-reprint.png")
    assert dash.store.daily.snapshot() == snap_before
    # reprinted copy is queued for upload like any print
    assert any(i.remote_key == re_ev.media for i in dash.store.outbox.pending())
    # unlimited
    client.post(f"/reprint/{items['print'].id}")
    client.post(f"/reprint/{re_ev.id}")
    assert dash.printer.count == 3
    assert "Sent to the printer" in client.get("/reprint?msg=reprinted").text


def test_reprint_printer_error_is_polite(dash, client, items):
    dash.printer.fail_next = 1
    r = client.post(f"/reprint/{items['print'].id}")
    assert r.status_code == 303 and "print_failed" in r.headers["location"]
    assert len(dash.store.events.prints()) == 1
    err = dash.store.events.query("error")[-1]
    assert err.payload["kind"] == "reprint_failed"
    t = client.get(r.headers["location"]).text
    assert "has paper" in t and "Traceback" not in t


def test_reprint_unexpected_printer_exception(dash, client, items, monkeypatch):
    def boom(img):
        raise OSError("usb gone")

    monkeypatch.setattr(dash.printer, "print_image", boom)
    r = client.post(f"/reprint/{items['print'].id}")
    assert "print_failed" in r.headers["location"]


def test_reprint_quiet_hours_needs_force(dash, client, items):
    dash.clock.set_local(2026, 3, 10, 21, 0)
    r = client.post(f"/reprint/{items['print'].id}", page="/reprint")
    assert "print_quiet" in r.headers["location"] and dash.printer.count == 0
    assert "print anyway" in client.get("/reprint").text
    r = client.post(f"/reprint/{items['print'].id}", {"force": "1"})
    assert "reprinted" in r.headers["location"] and dash.printer.count == 1


def test_reprint_missing_and_non_print(dash, client, items):
    dash.store.media_path(items["print"].media).unlink()
    assert "print_missing" in client.post(f"/reprint/{items['print'].id}").headers["location"]
    press = dash.store.events.query("press")[0]
    assert "not_found" in client.post(f"/reprint/{press.id}").headers["location"]
    assert "not_found" in client.post("/reprint/999999").headers["location"]
    assert dash.printer.count == 0


# -- settings ---------------------------------------------------------------------------------


def _form(**over):
    f = {"quiet_start": "19:30", "quiet_end": "07:00", "timezone": "America/Chicago",
         "day_start": "06:30", "location_name": "Home", "location_lat": "41.88",
         "location_lon": "-87.63", "rec_cap_s": "45", "volume": "0.5", "spend_cap_usd": "25",
         "review_delay_min": "90", "approval_required_until": "2026-04-01"}
    f.update(over)
    return f


def test_settings_page_shows_values(client):
    t = client.get("/settings").text
    assert 'value="19:00"' in t and "America/New_York" in t and 'value="30"' in t


def test_settings_save(dash, client):
    r = client.post("/settings", _form())
    assert r.status_code == 303 and "saved" in r.headers["location"]
    s = dash.store.settings
    assert s.quiet_hours.start.strftime("%H:%M") == "19:30"
    assert s.timezone == "America/Chicago"
    assert s.location.name == "Home" and s.location.lat == pytest.approx(41.88)
    assert s.rec_cap_s == 45 and s.volume == 0.5 and s.spend_cap_usd == 25
    assert s.review_delay_s == 5400
    assert s.approval_required_until.isoformat() == "2026-04-01"
    ev = dash.store.events.query("settings_changed")[-1]
    assert "rec_cap_s" in ev.payload["keys"]


def test_settings_clear_location_and_approval(dash, client):
    client.post("/settings", _form())
    r = client.post("/settings", _form(location_name="", location_lat="", location_lon="",
                                       approval_required_until=""))
    assert r.status_code == 303
    assert dash.store.settings.location is None
    assert dash.store.settings.approval_required_until is None


@pytest.mark.parametrize("over,needle", [
    ({"rec_cap_s": "301"}, "Recording cap"),
    ({"rec_cap_s": "0"}, "Recording cap"),
    ({"rec_cap_s": "abc"}, "Recording cap"),
    ({"volume": "1.5"}, "Volume"),
    ({"volume": "nan"}, "Volume"),
    ({"spend_cap_usd": "-1"}, "Spend cap"),
    ({"timezone": "Mars/Olympus"}, "Time zone"),
    ({"quiet_start": "25:00"}, "Quiet hours"),
    ({"day_start": ""}, "Day start"),
    ({"location_lat": "95"}, "Weather location"),
    ({"location_lat": ""}, "Latitude"),
    ({"location_name": ""}, "Weather location"),
    ({"review_delay_min": "-5"}, "Review delay"),
    ({"approval_required_until": "soon"}, "Approval required until"),
])
def test_settings_validation_rejects(dash, client, over, needle):
    before = dash.store.settings.as_dict()
    r = client.post("/settings", _form(**over))
    assert r.status_code == 400
    assert needle in r.text
    assert dash.store.settings.as_dict() == before  # nothing partially saved


def test_rec_cap_over_300_rejected_and_form_keeps_input(dash, client):
    r = client.post("/settings", _form(rec_cap_s="600"))
    assert r.status_code == 400 and 'value="600"' in r.text
    assert dash.store.settings.rec_cap_s == 30


def test_parse_settings_form_unit():
    from zozobox.dashboard import parse_settings_form

    values, errors = parse_settings_form(_form(rec_cap_s="300"))
    assert not errors and values["rec_cap_s"] == 300
    _, errors = parse_settings_form(_form(rec_cap_s="300.5"))
    assert errors


# -- media -----------------------------------------------------------------------------------


def test_media_served(dash, client, items):
    r = client.get("/media/" + items["print"].media)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    r = client.get("/media/" + items["zozo"].media)
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/wav")


@pytest.mark.parametrize("path", [
    "/media/../zozobox.db",
    "/media/%2e%2e/zozobox.db",
    "/media/%2E%2E%2Fzozobox.db",
    "/media/prints/..%2f..%2fzozobox.db",
    "/media/prints/%2e%2e/%2e%2e/dashboard_auth.json",
    "/media/..%5c..%5cdashboard_auth.json",
    "/media/%2fetc%2fpasswd",
    "//media/etc/passwd",
    "/media/prints/x.png%00.png",
    "/media/.hidden.png",
    "/media/prints/missing.png",
    "/media/secret.db",
    "/media/notes.py",
])
def test_media_traversal_rejected(dash, client, items, path):
    dash.store.write_media(".hidden.png", b"x")
    dash.store.write_media("secret.db", b"x")
    dash.store.write_media("notes.py", b"x")
    r = client.get(path)
    assert r.status_code in (404, 400, 303)
    assert b"SQLite" not in r.content and b"scrypt" not in r.content and b"root:" not in r.content


def test_media_symlink_escape_rejected(dash, client, items):
    target = dash.store.data_dir / "dashboard_auth.json"
    assert target.exists()
    link = dash.store.media_dir / "evil.json"
    link.symlink_to(target)
    r = client.get("/media/evil.json")
    assert r.status_code == 404 and b"scrypt" not in r.content


def test_resolve_media_unit(tmp_path):
    from zozobox.dashboard import resolve_media
    from zozobox.dashboard.app import _NotFound

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.png").write_bytes(b"x")
    assert resolve_media(tmp_path, "a/b.png") == (tmp_path / "a" / "b.png").resolve()
    for bad in ("../a/b.png", "a/../a/b.png", "/a/b.png", "a\\b.png", "a//b.png", "a/./b.png",
                "", "a/b.png\x00", "a/b.exe"):
        with pytest.raises(_NotFound):
            resolve_media(tmp_path, bad)


def test_generated_text_is_escaped(dash, client):
    dash.store.content.add("story", meta={"title": "<script>alert(1)</script>",
                                          "text": "<img src=x onerror=alert(1)>"},
                           safety="passed", approval="pending")
    t = client.get("/approvals").text
    assert "<script>alert(1)" not in t and "&lt;script&gt;" in t
    assert "<img src=x" not in t


def test_flash_codes_are_not_reflected(client):
    t = client.get("/?msg=<b>hi</b>").text
    assert "<b>hi</b>" not in t and "&lt;b&gt;" not in t


# -- regression: purge of Zozo's message must reach the cloud ---------------------------


def _ctl_keys(dash):
    return [i.remote_key for i in dash.store.outbox.pending() if i.remote_key.startswith("control/")]


def test_purge_live_zozo_queues_withhold_first(dash, client, items):
    rid = items["zozo"].id
    r = client.post("/archive/purge", {"target": "recording", "item_id": rid, "confirm": "PURGE"})
    assert r.status_code == 303 and "purged" in r.headers["location"]
    assert dash.store.recordings.find(rid) is None
    assert _ctl_keys(dash) == [f"control/zozo/{rid}-withhold.json"]
    assert dash.store.events.query("dashboard_withhold")[0].payload["rec_id"] == rid


def test_purge_superseded_zozo_also_queues_withhold(dash, client, items):
    old = items["zozo"]
    dash.clock.advance(minutes=5)
    dash.store.recordings.put(Slot.ZOZO, dash.media("recordings/zozo/2026/03/10/n.wav", b"RIFF"))
    client.post("/archive/purge", {"target": "recording", "item_id": old.id, "confirm": "PURGE"})
    assert f"control/zozo/{old.id}-withhold.json" in _ctl_keys(dash)


def test_purge_already_withheld_zozo_does_not_duplicate(dash, client, items):
    rid = items["zozo"].id
    client.post(f"/recordings/{rid}/withhold")
    client.post("/archive/purge", {"target": "recording", "item_id": rid, "confirm": "PURGE"})
    assert _ctl_keys(dash) == [f"control/zozo/{rid}-withhold.json"]
    assert dash.store.recordings.find(rid) is None


def test_purge_grandma_queues_nothing(dash, client, items):
    client.post("/archive/purge", {"target": "recording", "item_id": items["grandma"].id,
                                   "confirm": "PURGE"})
    assert _ctl_keys(dash) == []


def test_purge_zozo_kept_if_withhold_cannot_be_queued(dash, client, items, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(dash.store.outbox, "enqueue", boom)
    rid = items["zozo"].id
    r = client.post("/archive/purge", {"target": "recording", "item_id": rid, "confirm": "PURGE"})
    assert r.status_code == 303 and "msg=error" in r.headers["location"]
    rec = dash.store.recordings.get(rid)
    assert rec.status is RecStatus.LIVE  # not purged, not half-withheld


# -- regression: safety-failed items without a recent event ------------------------------


def test_home_lists_safety_failed_items_without_events(dash, client):
    from zozobox.store import Kind

    it = dash.store.content.add(Kind.COLORING, meta={"title": "Spooky bat"}, safety="failed",
                                approval="pending")
    dash.store.content.set_safety(it.id, "failed", "teeth too sharp")
    t = client.get("/").text
    assert "Spooky bat" in t and "teeth too sharp" in t
    assert f'href="/archive/item/{it.id}"' in t
    assert "<ul></ul>" not in t and "No safety failures" not in t


def test_home_no_safety_failures(client):
    assert "No safety failures" in client.get("/").text


# -- regression: sync status on the home page -----------------------------------------------


def _write_sync_status(dash, **status):
    import json

    (dash.store.data_dir / "sync_state.json").write_text(json.dumps({"status": status}))


def test_home_sync_never_reported(client):
    assert "sync client hasn't reported yet" in client.get("/").text


def test_home_sync_ok(dash, client):
    from zozobox.sync import iso_utc

    now = dash.clock()
    _write_sync_status(dash, last_run_at=iso_utc(now), last_ok_at=iso_utc(now), online=True)
    t = client.get("/").text
    assert "online" in t and "Last good sync: Tue Mar 10 10:00" in t
    assert "No successful sync for a while" not in t


def test_home_sync_offline_and_stale(dash, client):
    from zozobox.sync import iso_utc

    now = dash.clock()
    _write_sync_status(dash, last_run_at=iso_utc(now),
                       last_ok_at=iso_utc(now - timedelta(minutes=11)), online=False,
                       last_error="grandma: offline <script>")
    t = client.get("/").text
    assert "offline" in t and "No successful sync for a while" in t
    assert "grandma: offline &lt;script&gt;" in t


def test_home_sync_status_unreadable_is_contained(dash, client, monkeypatch):
    from zozobox.dashboard import queries

    def boom(store):
        raise RuntimeError("bad")

    monkeypatch.setattr(queries, "sync_info", boom)
    r = client.get("/")
    assert r.status_code == 200 and "Sync status unavailable" in r.text


# -- regression: review window pauses during quiet hours -------------------------------------


def test_review_window_paused_by_quiet_hours(dash, client):
    # quiet 19:00-07:00 (default), delay 1 h, recorded 18:30: 30 min run before
    # quiet hours, the other 30 min after 07:00 -> publishes 07:30 next day.
    dash.clock.set_local(2026, 3, 10, 18, 30)
    rec = dash.store.recordings.put(Slot.ZOZO, dash.media("recordings/zozo/x.wav", b"RIFF"))
    dash.clock.set_local(2026, 3, 10, 19, 30)
    t = client.get("/recordings").text
    assert "In review (paused for quiet hours)" in t and "review window is over" not in t
    assert "Wed Mar 11 07:30" in t
    assert "paused for quiet hours" in client.get("/").text
    dash.clock.set_local(2026, 3, 11, 7, 20)
    assert "10 min left" in client.get("/recordings").text
    dash.clock.set_local(2026, 3, 11, 7, 31)
    assert "cloud hasn't reported publishing" in client.get("/recordings").text
    assert rec.id


def test_effective_publish_at_unit():
    from datetime import datetime, time, timezone
    from zoneinfo import ZoneInfo

    from zozobox.dashboard.queries import effective_publish_at
    from zozobox.store.settings import QuietHours

    ny = ZoneInfo("America/New_York")
    q = QuietHours(time(19, 0), time(7, 0))

    def at(*a):
        return datetime(*a, tzinfo=ny).astimezone(timezone.utc)

    # outside quiet hours: nominal
    assert effective_publish_at(at(2026, 3, 10, 10, 0), 3600, q, ny) == at(2026, 3, 10, 11, 0)
    # recorded during quiet hours: clock starts at 07:00
    assert effective_publish_at(at(2026, 3, 10, 23, 0), 3600, q, ny) == at(2026, 3, 11, 8, 0)
    # runs out exactly at quiet start -> publishes when quiet ends
    assert effective_publish_at(at(2026, 3, 10, 18, 0), 3600, q, ny) == at(2026, 3, 11, 7, 0)
    # spans quiet hours
    assert effective_publish_at(at(2026, 3, 10, 18, 30), 3600, q, ny) == at(2026, 3, 11, 7, 30)
    # never quiet
    never = QuietHours(time(7, 0), time(7, 0))
    assert effective_publish_at(at(2026, 3, 10, 23, 0), 60, never, ny) == at(2026, 3, 10, 23, 1)
    # zero delay during quiet hours waits for them to end
    assert effective_publish_at(at(2026, 3, 10, 20, 0), 0, q, ny) == at(2026, 3, 11, 7, 0)
    # DST spring-forward night (Mar 8 2026): still 07:00 local + remainder
    assert effective_publish_at(at(2026, 3, 7, 22, 0), 1800, q, ny) == at(2026, 3, 8, 7, 30)
