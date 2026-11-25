from pathlib import Path

import pytest

from zozobox.engine import MoodResult
from zozobox.mood import (
    SPEND, SPEND_CAP_REACHED, FakeLabeler, FakeTranscriber, Label,
    OpenAIClaudeMoodClassifier, ServiceUnavailable, SpendItem, SpendLedger, Transcript,
)

STT_SPEND = SpendItem("openai", "gpt-4o-mini-transcribe", 0.0001, 2.0, 0, "seconds")
LAB_SPEND = SpendItem("anthropic", "claude-opus-5-5", 0.002, 300, 50)


def make(store=None, transcripts=(), labels=(), **kw):
    t = FakeTranscriber(transcripts, spend=STT_SPEND)
    lab = FakeLabeler(labels, spend=LAB_SPEND)
    led = SpendLedger(store) if store is not None else None
    return OpenAIClaudeMoodClassifier(t, lab, ledger=led, **kw), t, lab


def test_keyword_hit_skips_claude(wav, store):
    clf, t, lab = make(store, ["I happy"])
    assert clf.classify(wav) == MoodResult("happy", "I happy")
    assert lab.calls == []
    [sp] = store.events.query(SPEND)
    assert sp.payload["provider"] == "openai" and sp.payload["purpose"] == "mood_stt"


def test_ambiguous_goes_to_claude(wav, store):
    clf, t, lab = make(store, ["not happy, sad"], ["sad"])
    assert clf.classify(wav) == MoodResult("sad", "not happy, sad")
    assert lab.calls == ["not happy, sad"]
    purposes = [e.payload["purpose"] for e in store.events.query(SPEND)]
    assert purposes == ["mood_stt", "mood_classify"]


def test_keywords_can_be_disabled(wav):
    clf, t, lab = make(None, ["mad"], ["mad"], use_keywords=False)
    assert clf.classify(wav).label == "mad"
    assert lab.calls == ["mad"]


@pytest.mark.parametrize("text", ["", "   ", "..."])
def test_empty_transcript_unknown_no_claude(wav, text):
    clf, t, lab = make(None, [text])
    r = clf.classify(wav)
    assert r.label == "unknown" and not r.offline
    assert lab.calls == []


@pytest.mark.parametrize("kind", ["network", "timeout", "auth", "server"])
def test_stt_failure_is_offline(wav, store, kind):
    clf, t, lab = make(store, [ServiceUnavailable("openai", kind)])
    r = clf.classify(wav)
    assert r.offline and r.label == "unknown"
    assert store.events.query(SPEND) == []


def test_claude_failure_is_offline_keeps_transcript(wav):
    clf, *_ = make(None, ["doggy"], [ServiceUnavailable("anthropic", "network")])
    r = clf.classify(wav)
    assert (r.label, r.transcript, r.offline) == ("unknown", "doggy", True)
    assert (r.provider, r.kind, r.per_take) == ("anthropic", "network", False)


def test_bad_label_becomes_unknown(wav):
    clf, *_ = make(None, ["hmm"], [Label("silent")])
    assert clf.classify(wav) == MoodResult("unknown", "hmm")


def test_unexpected_exception_never_raises(wav):
    clf, *_ = make(None, [RuntimeError("boom")])
    assert clf.classify(wav).offline


def test_missing_audio_unknown(tmp_path):
    clf, t, _ = make(None, ["happy"])
    r = clf.classify(tmp_path / "missing.wav")
    assert r.label == "unknown" and not r.offline and t.calls == []


def test_spend_cap_blocks_before_stt(wav, store):
    store.settings.spend_cap_usd = 0.5
    SpendLedger(store).record(SpendItem("openai", "m", 0.5, 1, 1), "other")
    clf, t, lab = make(store, ["happy"])
    assert clf.classify(wav).offline
    assert t.calls == []
    [ev] = store.events.query(SPEND_CAP_REACHED)
    assert ev.payload["purpose"] == "mood_stt"


def test_spend_cap_reached_between_stt_and_claude(wav, store):
    store.settings.spend_cap_usd = 0.00005  # STT_SPEND pushes it over
    clf, t, lab = make(store, ["doggy"])
    r = clf.classify(wav)
    assert r == MoodResult("unknown", "doggy", offline=True)
    assert lab.calls == []
    assert store.events.query(SPEND_CAP_REACHED)[0].payload["purpose"] == "mood_classify"


def test_classify_transcript(store):
    clf, t, lab = make(store, labels=["neutral"])
    assert clf.classify_transcript("mm okay-ish") == MoodResult("neutral", "mm okay-ish")
    assert clf.classify_transcript("sad") == MoodResult("sad", "sad")
    assert t.calls == []


def test_purpose_suffix(wav, store):
    clf, *_ = make(store, ["happy"], purpose_suffix="_offline")
    clf.classify(wav)
    assert store.events.query(SPEND)[0].payload["purpose"] == "mood_stt_offline"


def test_from_store_without_keys_is_offline(wav, store):
    """Real SDK classes, no keys, sockets blocked: offline, never raises."""
    clf = OpenAIClaudeMoodClassifier.from_store(store)
    assert clf.classify(wav).offline
    assert clf.classify_transcript("doggy").offline


def test_engine_protocol_compatible(wav):
    from zozobox.engine import MoodClassifier  # noqa: F401  (Protocol)
    clf, *_ = make(None, ["sad sad"])
    r = clf.classify(Path(wav))
    assert isinstance(r, MoodResult) and r.label == "sad"


def test_stt_result_transcript_object(wav):
    t = FakeTranscriber([Transcript("grumpy", None)])
    clf = OpenAIClaudeMoodClassifier(t, FakeLabeler())
    assert clf.classify(wav).label == "mad"


# -- review fixes: config errors, deadline, truncated spend --------------------

class Mono:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def _errors(store):
    return [e.payload for e in store.events.query("error", limit=None)]


@pytest.mark.parametrize("kind", ["config", "auth"])
def test_config_error_logged_as_error_event_throttled(wav, store, kind):
    mono = Mono()
    t = FakeTranscriber([ServiceUnavailable("openai", kind, "AuthenticationError")] * 3)
    clf = OpenAIClaudeMoodClassifier(t, FakeLabeler(), ledger=SpendLedger(store), monotonic=mono)
    assert clf.classify(wav).offline
    assert clf.classify(wav).offline  # same hour: not logged again
    mono.t += 3601
    assert clf.classify(wav).offline
    errs = _errors(store)
    assert len(errs) == 2
    assert errs[0] == {"kind": "mood_config_error", "provider": "openai",
                       "error_kind": kind, "detail": "AuthenticationError"}


def test_transient_error_not_logged_as_config_error(wav, store):
    clf, *_ = make(store, [ServiceUnavailable("openai", "network")])
    assert clf.classify(wav).offline
    assert _errors(store) == []


def test_from_store_without_keys_logs_config_errors_without_secrets(wav, store, monkeypatch):
    clf = OpenAIClaudeMoodClassifier.from_store(store)
    assert clf.classify(wav).offline
    assert clf.classify_transcript("doggy").offline
    errs = _errors(store)
    assert {(e["provider"], e["error_kind"]) for e in errs} == {("openai", "auth"),
                                                                ("anthropic", "auth")}


def test_truncated_claude_answer_is_pending_and_billed(wav, store):
    sp = SpendItem("anthropic", "claude-opus-5-5", 0.3, 300, 16000)
    clf, *_ = make(store, ["doggy"], [ServiceUnavailable("anthropic", "truncated", spend=sp)])
    r = clf.classify(wav)
    assert r.offline and r.per_take and r.transcript == "doggy"
    purposes = [e.payload["purpose"] for e in store.events.query(SPEND)]
    assert purposes == ["mood_stt", "mood_classify"]


class SlowTranscriber(FakeTranscriber):
    def __init__(self, mono, took, *a, **kw):
        super().__init__(*a, **kw)
        self.mono, self.took = mono, took

    def transcribe(self, path):
        self.mono.t += self.took
        return super().transcribe(path)


def test_deadline_passes_remaining_budget_to_claude(wav):
    mono = Mono()
    lab = FakeLabeler(["sad"])
    clf = OpenAIClaudeMoodClassifier(SlowTranscriber(mono, 5.0, ["doggy"]), lab,
                                     deadline_s=18.0, monotonic=mono)
    assert clf.classify(wav).label == "sad"
    assert lab.timeouts == [pytest.approx(13.0)]


def test_deadline_too_little_left_skips_claude(wav, store):
    mono = Mono()
    lab = FakeLabeler(["sad"])
    clf = OpenAIClaudeMoodClassifier(SlowTranscriber(mono, 16.0, ["doggy"], spend=STT_SPEND), lab,
                                     ledger=SpendLedger(store), deadline_s=18.0, monotonic=mono)
    r = clf.classify(wav)
    assert r.offline and r.transcript == "doggy" and lab.calls == []
    assert [e.payload["purpose"] for e in store.events.query(SPEND)] == ["mood_stt"]


def test_no_deadline_no_timeout_override(wav):
    lab = FakeLabeler(["sad"])
    clf = OpenAIClaudeMoodClassifier(FakeTranscriber(["doggy"]), lab)
    clf.classify(wav)
    clf.classify_transcript("doggy")
    assert lab.timeouts == [None, None]


def test_from_store_deadline_fits_engine_timeout(store):
    from zozobox.engine.engine import EngineSettings
    clf = OpenAIClaudeMoodClassifier.from_store(store)
    assert clf.deadline_s is not None
    assert clf.deadline_s < EngineSettings().classify_timeout_s
    assert clf.transcriber.max_retries == 0 and clf.labeler.max_retries == 0
    assert OpenAIClaudeMoodClassifier.from_store(store, deadline_s=None).deadline_s is None
