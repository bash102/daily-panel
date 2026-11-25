import json
from datetime import timedelta

import pytest

from zozobox.engine import FakeMoodClassifier, MoodResult
from zozobox.mood import (
    SPEND_CAP_REACHED, FakeLabeler, FakeTranscriber, OpenAIClaudeMoodClassifier,
    SpendItem, SpendLedger, reprocess_pending, unmatched_pending,
)
from zozobox.mood.__main__ import main



write_wav = None  # set from the conftest fixture (tests/ has no packages)


@pytest.fixture(autouse=True)
def _wav_writer(make_wav):
    global write_wav
    write_wav = make_wav


def pending(store, clock, name, attempt=1, transcript="", reason=None, wav=True):
    rel = f"mood/2026/03/10/{name}.wav"
    if wav:
        write_wav(store.media_path(rel), 1.0)
    payload = {"attempt": attempt, "audio": rel, "transcript": transcript}
    if reason:
        payload["reason"] = reason
    ev = store.events.append("mood_pending", payload, media=rel, ts=clock())
    clock.advance(minutes=5)
    return ev


def test_records_with_original_ts_and_source_offline(store, clock):
    p1 = pending(store, clock, "a")
    p2 = pending(store, clock, "b", attempt=2, reason="abandoned")
    clock.advance(days=2)
    clf = FakeMoodClassifier([MoodResult("happy", "I happy"), MoodResult("joy", "x")])
    rep = reprocess_pending(store, clf)
    assert (rep.pending, rep.recognized, rep.unknown, rep.stopped) == (2, 1, 1, None)
    m1, m2 = store.events.moods()
    assert m1.ts == p1.ts and m1.day == p1.day and m1.media == p1.media
    assert m1.payload["label"] == "happy" and m1.payload["source"] == "offline"
    assert m1.payload["transcript"] == "I happy" and m1.payload["attempt"] == 1
    assert m2.payload["label"] == "unknown" and m2.payload["attempt"] == 2  # bad label
    assert store.events.first_mood(p1.day).id == m1.id


def test_idempotent(store, clock):
    pending(store, clock, "a")
    clf = FakeMoodClassifier(default=MoodResult("sad", "sad"))
    reprocess_pending(store, clf)
    rep = reprocess_pending(store, clf)
    assert rep.pending == 0 and rep.recorded_ids == []
    assert len(store.events.moods()) == 1
    assert len(clf.calls) == 1


def test_skips_takes_already_recognized_online_and_duplicates(store, clock):
    p = pending(store, clock, "a")
    store.events.record_mood("mad", transcript="mad", source="online", audio=p.media, ts=p.ts)
    q = pending(store, clock, "b")
    store.events.append("mood_pending", {"attempt": 1}, media=q.media, ts=q.ts)  # dup
    store.events.append("mood_pending", {"attempt": 1})  # no media
    assert [e.id for e in unmatched_pending(store)] == [q.id]


def test_offline_stops_and_leaves_pending(store, clock):
    pending(store, clock, "a")
    pending(store, clock, "b")
    clf = FakeMoodClassifier([MoodResult(offline=True)])
    rep = reprocess_pending(store, clf)
    assert rep.stopped == "offline" and rep.left_pending == 2
    assert store.events.moods() == [] and len(clf.calls) == 1


def test_classifier_exception_counts_as_offline(store, clock):
    pending(store, clock, "a")
    rep = reprocess_pending(store, FakeMoodClassifier([RuntimeError("x")]))
    assert rep.stopped == "offline" and store.events.moods() == []


def test_missing_audio_recorded_unknown(store, clock):
    pending(store, clock, "gone", wav=False)
    clf = FakeMoodClassifier()
    rep = reprocess_pending(store, clf)
    [m] = store.events.moods()
    assert m.payload["label"] == "unknown" and m.payload["reason"] == "audio_missing"
    assert clf.calls == [] and rep.unknown == 1


def test_reuses_pending_transcript(store, clock):
    pending(store, clock, "a", transcript="doggy maybe")
    t = FakeTranscriber(["should not be used"])
    lab = FakeLabeler(["sad"])
    clf = OpenAIClaudeMoodClassifier(t, lab)
    reprocess_pending(store, clf)
    assert t.calls == [] and lab.calls == ["doggy maybe"]
    assert store.events.moods()[0].payload["label"] == "sad"


def test_limit(store, clock):
    for n in "abc":
        pending(store, clock, n)
    rep = reprocess_pending(store, FakeMoodClassifier(default=MoodResult("sad", "sad")), limit=2)
    assert len(rep.recorded_ids) == 2 and rep.left_pending == 1 and rep.stopped is None


def test_dry_run(store, clock):
    pending(store, clock, "a")
    rep = reprocess_pending(store, None, dry_run=True)
    assert rep.pending == 1 and rep.left_pending == 1 and store.events.moods() == []


def test_spend_cap_stops(store, clock):
    pending(store, clock, "a")
    store.settings.spend_cap_usd = 1.0
    SpendLedger(store).record(SpendItem("openai", "m", 2.0, 1, 1), "nightly")
    clf = FakeMoodClassifier(default=MoodResult("sad", "sad"))
    rep = reprocess_pending(store, clf)
    assert rep.stopped == "spend_cap" and clf.calls == []
    assert store.events.query(SPEND_CAP_REACHED)[0].payload["purpose"] == "mood_offline"


def test_cap_from_last_month_does_not_block(store, clock):
    store.settings.spend_cap_usd = 1.0
    SpendLedger(store).record(SpendItem("openai", "m", 2.0, 1, 1), "nightly")
    clock.advance(days=31)
    pending(store, clock, "a")
    rep = reprocess_pending(store, FakeMoodClassifier(default=MoodResult("sad", "sad")))
    assert rep.recognized == 1


# -- CLI ---------------------------------------------------------------------

def test_cli_reprocess(store, clock, capsys):
    pending(store, clock, "a")
    rc = main(["reprocess", "--data-dir", "ignored"],
              make_store=lambda d: _NoClose(store),
              make_classifier=lambda s: FakeMoodClassifier(default=MoodResult("mad", "mad")))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["recognized"] == 1
    assert store.events.moods()[0].payload["source"] == "offline"


def test_cli_offline_exit_2(store, clock, capsys):
    pending(store, clock, "a")
    rc = main(["reprocess", "--data-dir", "x"], make_store=lambda d: _NoClose(store),
              make_classifier=lambda s: FakeMoodClassifier(default=MoodResult(offline=True)))
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["stopped"] == "offline"


def test_cli_needs_data_dir(capsys):
    assert main(["reprocess"]) == 1


def test_cli_real_store_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ZOZOBOX_DATA_DIR", str(tmp_path / "d"))
    assert main(["reprocess", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == 0


def test_cli_real_wiring_without_keys_stays_pending(tmp_path, capsys):
    from zozobox.store import Store
    d = tmp_path / "d"
    with Store(d) as s:
        rel = "mood/2026/03/10/a.wav"
        write_wav(s.media_path(rel))
        s.events.append("mood_pending", {"attempt": 1}, media=rel)
    assert main(["reprocess", "--data-dir", str(d)]) == 2
    with Store(d) as s:
        assert s.events.moods() == []


class _NoClose:
    def __init__(self, store):
        self._s = store

    def __getattr__(self, n):
        return getattr(self._s, n)

    def close(self):
        pass


# -- review fix: a poisoned take must not block the queue ---------------------

from zozobox.mood import ServiceUnavailable, Transcript  # noqa: E402
from zozobox.mood.reprocess import MAX_ATTEMPTS, MOOD_REPROCESS_FAILED  # noqa: E402


class PoisonTranscriber(FakeTranscriber):
    """Rejects audio whose file name starts with `bad`; else says `text`."""

    def __init__(self, exc, text="I happy"):
        super().__init__()
        self.exc, self.text = exc, text

    def transcribe(self, path):
        self.calls.append(path)
        if path.name.startswith("bad"):
            raise self.exc
        return Transcript(self.text)


@pytest.mark.parametrize("kind,reason", [("bad_request", "stt_rejected"),
                                         ("unreadable", "audio_unreadable")])
def test_poisoned_first_take_does_not_block_queue(store, clock, kind, reason):
    bad = pending(store, clock, "bad1")
    good = [pending(store, clock, n) for n in ("a", "b")]
    clf = OpenAIClaudeMoodClassifier(
        PoisonTranscriber(ServiceUnavailable("openai", kind, "BadRequestError")), FakeLabeler())
    rep = reprocess_pending(store, clf)
    assert rep.as_dict() | {"recorded_ids": None} == {
        "pending": 3, "recognized": 2, "unknown": 0, "failed": 1, "gave_up": 0,
        "left_pending": 1, "stopped": None, "recorded_ids": None}
    assert {m.media for m in store.events.moods()} == {g.media for g in good}
    # later runs retry only the poisoned take, then give up on it
    for run in range(2, MAX_ATTEMPTS + 1):
        rep = reprocess_pending(store, clf)
        assert rep.pending == 1
        if run < MAX_ATTEMPTS:
            assert rep.failed == 1 and rep.recorded_ids == []
    assert rep.gave_up == 1 and rep.unknown == 1 and rep.left_pending == 0
    m = [m for m in store.events.moods() if m.media == bad.media][0]
    assert m.payload["label"] == "unknown" and m.payload["reason"] == reason
    assert m.payload["source"] == "offline" and m.ts == bad.ts
    assert m.payload["pending_id"] == bad.id and m.payload["attempt"] == 1
    fails = store.events.query(MOOD_REPROCESS_FAILED, limit=None)
    assert len(fails) == MAX_ATTEMPTS
    assert fails[0].payload == {"pending_id": bad.id, "media": bad.media,
                                "provider": "openai", "kind": kind}
    assert fails[0].media is None  # no media reference: nothing to upload/clean
    assert reprocess_pending(store, clf).pending == 0


def test_transient_error_still_stops_run(store, clock):
    for n in ("bad1", "a"):
        pending(store, clock, n)
    clf = OpenAIClaudeMoodClassifier(
        PoisonTranscriber(ServiceUnavailable("openai", "network")), FakeLabeler())
    rep = reprocess_pending(store, clf)
    assert rep.stopped == "offline" and rep.left_pending == 2 and rep.failed == 0
    assert store.events.query(MOOD_REPROCESS_FAILED, limit=None) == []


def test_claude_bad_request_is_global_not_per_take(store, clock):
    pending(store, clock, "a")
    pending(store, clock, "b")
    clf = OpenAIClaudeMoodClassifier(
        FakeTranscriber(default="doggy"),
        FakeLabeler([ServiceUnavailable("anthropic", "bad_request")]))
    rep = reprocess_pending(store, clf)
    assert rep.stopped == "offline" and rep.left_pending == 2


def test_many_failures_in_a_row_stop_the_run(store, clock):
    for n in ("bad1", "bad2", "bad3", "bad4", "a"):
        pending(store, clock, n)
    clf = OpenAIClaudeMoodClassifier(
        PoisonTranscriber(ServiceUnavailable("openai", "bad_request")), FakeLabeler())
    rep = reprocess_pending(store, clf)
    assert rep.stopped == "offline" and rep.failed == 3 and rep.left_pending == 5
    # each run makes progress on the attempt counters; eventually all resolve
    for _ in range(MAX_ATTEMPTS + 2):
        rep = reprocess_pending(store, clf)
    assert unmatched_pending(store) == []
    labels = sorted(m.payload["label"] for m in store.events.moods())
    assert labels == ["happy", "unknown", "unknown", "unknown", "unknown"]


def test_truncated_claude_answer_stays_pending_then_retried(store, clock):
    """Regression: stop_reason max_tokens/pause_turn must not record unknown."""
    p = pending(store, clock, "a", transcript="doggy")
    lab = FakeLabeler([ServiceUnavailable("anthropic", "truncated", "stop_reason=max_tokens"),
                       "sad"])
    clf = OpenAIClaudeMoodClassifier(FakeTranscriber(), lab)
    rep = reprocess_pending(store, clf)
    assert rep.failed == 1 and rep.recorded_ids == [] and store.events.moods() == []
    assert [e.id for e in unmatched_pending(store)] == [p.id]
    rep = reprocess_pending(store, clf)
    assert rep.recognized == 1 and store.events.moods()[0].payload["label"] == "sad"


def test_truncated_via_real_labeler_response_stays_pending(store, clock):
    from types import SimpleNamespace as NS
    from zozobox.mood import ClaudeMoodLabeler

    class Client:
        def __init__(self):
            self.beta = NS(messages=NS(create=lambda **kw: NS(
                stop_reason="max_tokens", content=[], model="claude-opus-5-5",
                usage=NS(input_tokens=300, output_tokens=16000))))

    pending(store, clock, "a", transcript="doggy")
    clf = OpenAIClaudeMoodClassifier(FakeTranscriber(), ClaudeMoodLabeler(client=Client()),
                                     ledger=SpendLedger(store), purpose_suffix="_offline")
    rep = reprocess_pending(store, clf)
    assert rep.failed == 1 and store.events.moods() == []
    [sp] = store.events.query("spend", limit=None)
    assert sp.payload["purpose"] == "mood_classify_offline" and sp.payload["usd"] > 0
