from __future__ import annotations

import threading
import time
from datetime import date

import pytest

from zozobox.content import (
    CONTENT_ERROR, SAFETY_FAILED, ClaudeError, ClaudeRefusal, FakeClaude, FakeMedia, FakeWeather,
    Pipeline, WeatherError, default_fake_response, rechunk,
)
from zozobox.content import evergreen as eg
from zozobox.content.pipeline import GenerationError
from zozobox.engine.sources import PHRASES, StorePhraseBook
from zozobox.store import Approval, Safety

KINDS = ("story", "letter", "weather", "coloring")


def servable(store, kind):
    # The run is at 02:00: the box day is still yesterday, weather is for this morning.
    if kind == "weather":
        return store.content.current_for_today(kind, store.weather_day())
    return store.content.current_for_today(kind)


def approve_all(store):
    for it in store.content.pending_approval():
        store.content.approve(it.id)


def events(store, type_):
    return store.events.query(type_, limit=None)


def responder(overrides):
    """overrides: purpose -> list of results consumed in order (then default)."""
    queues = {k: list(v) for k, v in overrides.items()}

    def r(call):
        q = queues.get(call.purpose)
        if q:
            return q.pop(0)
        return default_fake_response(call)

    return r


FAIL = {"verdict": "fail", "reasons": ["too scary"]}


# ------------------------------------------------------------ happy path ----


def test_nightly_generates_all_items_in_contract_shape(pipeline, store, claude, media):
    res = pipeline.run_nightly()
    assert res == {k: "generated" for k in ("weather", "story", "letter", "coloring")}
    # first month: approval pending, nothing servable yet
    assert all(servable(store, k) is None for k in KINDS)
    assert len(store.content.pending_approval()) == 4
    approve_all(store)

    story = servable(store, "story")
    assert story.safety is Safety.PASSED
    keys = [f"chunk_{i}" for i in range(4)]
    assert list(story.media) == keys and story.meta["chunks"] == keys
    assert story.meta["title"] == "Zozo and the Sharing Picnic"
    assert all(store.media_exists(r) and r.endswith(".wav") for r in story.media.values())

    letter = servable(store, "letter")
    assert letter.meta["letter"] == "A"  # no previous letters: start of the cycle
    assert letter.meta["words"][:3] == ["apple", "ant", "alligator"]
    assert set(letter.media) == {"intro", "word_0", "word_1", "word_2", "word_3", "drawing"}

    w = servable(store, "weather")
    assert w.for_date == store.weather_day()
    assert w.meta["icon"] == "sunny" and w.meta["hi_c"] == 3.0 and w.meta["lo_c"] == -2.0
    assert w.meta["summary"] and "audio" in w.media

    col = servable(store, "coloring")
    assert col.meta["title"] == "Happy Turtle"
    assert col.media["art"].endswith(".png") and col.media["intro"].endswith(".wav")
    assert store.media_path(col.media["art"]).read_bytes().startswith(b"\x89PNG")


@pytest.fixture
def b_letter(store, pipeline):
    """Make the next letter B."""
    it = store.content.add("letter", meta={"letter": "A", "words": ["apple"]}, safety="passed")
    store.content.mark_served(it.id)
    return pipeline


def test_letter_item_shape(b_letter, store, media):
    assert b_letter.job_letter() == "generated"
    it = store.content.list("letter", limit=1)[0]
    assert it.meta["letter"] == "B"
    assert it.meta["words"] == ["ball", "bear", "banana", "boat"]
    assert set(it.media) == {"intro", "word_0", "word_1", "word_2", "word_3", "drawing"}
    assert "Today's letter is B! B says buh!" in media.tts_texts
    assert "B... ball! Can you say ball?" in media.tts_texts
    assert any("a ball" in p for p in media.image_prompts)


def test_after_auto_approval_items_are_servable(store, pipeline):
    store.settings.approval_required_until = date(2026, 3, 1)
    pipeline.run_nightly()
    for k in KINDS:
        it = servable(store, k)
        assert it is not None and it.approval is Approval.AUTO


def test_every_paid_call_logs_spend(pipeline, store, claude, media):
    pipeline.run_nightly()
    spend = events(store, "spend")
    n_paid = len(claude.calls) + len(media.tts_texts) + len(media.image_prompts)
    assert len(spend) == n_paid
    p = spend[0].payload
    assert set(p) == {"provider", "model", "purpose", "usd", "input_units", "output_units"}
    assert {e.payload["provider"] for e in spend} == {"anthropic", "openai"}
    purposes = {e.payload["purpose"] for e in spend}
    assert {"story", "story_review", "story_tts", "coloring_image", "coloring_image_review",
            "weather", "weather_tts"} <= purposes


def test_generation_and_review_settings(pipeline, claude):
    pipeline.run_nightly()
    gen = [c for c in claude.calls if "review" not in c.purpose]
    rev = [c for c in claude.calls if "review" in c.purpose]
    assert gen and rev
    assert all(c.effort == "medium" for c in gen)
    assert all(c.effort == "low" for c in rev)
    assert all("independent child-safety reviewer" in c.system for c in rev)
    assert all("independent" not in c.system for c in gen)
    img = [c for c in rev if "image" in c.purpose]
    assert img and all(c.has_image for c in img)


def test_privacy_only_nickname_and_age(pipeline, claude, store):
    pipeline.run_nightly()
    for c in claude.calls:
        blob = c.system + c.text()
        assert "Home" not in blob and "40.7" not in blob and "-74" not in blob
    assert any("Zozo" in c.system and "2-3" in c.system for c in claude.calls)


def test_rerun_is_idempotent(pipeline, store, claude, media):
    pipeline.run_nightly()
    n_claude, n_tts = len(claude.calls), len(media.tts_texts)
    res = pipeline.run_nightly()
    assert res == {k: "skipped" for k in ("weather", "story", "letter", "coloring")}
    assert len(claude.calls) == n_claude and len(media.tts_texts) == n_tts


def test_only_served_kinds_regenerate(pipeline, store, clock, claude):
    store.settings.approval_required_until = date(2026, 3, 1)
    pipeline.run_nightly()
    story = servable(store, "story")
    store.content.mark_served(story.id)
    clock.advance(days=1)
    res = pipeline.run_nightly()
    assert res["story"] == "generated"
    assert res["letter"] == res["coloring"] == "skipped"
    assert res["weather"] == "generated"  # new day: weather refreshes regardless
    assert len(store.content.list("story")) == 2


def test_story_avoids_recent_titles(pipeline, store, claude, clock):
    store.content.add("story", meta={"title": "Zozo Goes to the Zoo"}, safety="passed")
    store.content.mark_served(store.content.list("story")[0].id)
    pipeline.job_story()
    call = next(c for c in claude.calls if c.purpose == "story")
    assert "Zozo Goes to the Zoo" in call.text()


# ------------------------------------------------------------ safety gate ----


def test_story_rejected_once_then_passes(store, clients, clock):
    clients.claude = FakeClaude(responder({"story_review": [FAIL]}))
    p = Pipeline(store, clients, clock)
    assert p.job_story() == "generated"
    items = store.content.list("story")
    failed = [i for i in items if i.safety is Safety.FAILED]
    passed = [i for i in items if i.safety is Safety.PASSED]
    assert len(failed) == 1 and len(passed) == 1
    assert failed[0].safety_note == "too scary" and failed[0].media == {}
    ev = events(store, SAFETY_FAILED)
    assert len(ev) == 1 and ev[0].payload["kind"] == "story"
    assert ev[0].payload["reasons"] == ["too scary"] and ev[0].payload["attempt"] == 1
    # the retry was told to avoid the rejected title
    second = [c for c in clients.claude.calls if c.purpose == "story"][1]
    assert "Zozo and the Sharing Picnic" in second.text()


def test_story_rejected_twice_falls_back_to_evergreen(store, clients, clock):
    p = Pipeline(store, clients, clock)
    p.bootstrap()
    clients.claude = FakeClaude(responder({"story_review": [FAIL, FAIL]}))
    p = Pipeline(store, clients, clock)
    assert p.job_story() == "fallback"
    it = [i for i in store.content.list("story") if i.safety is Safety.PASSED][0]
    assert it.meta["source"] == "evergreen"
    assert it.meta["evergreen_slug"] in {s.slug for s in eg.STORIES}
    assert all(store.media_exists(r) for r in it.media.values())
    assert not any(r.startswith("evergreen/") for r in it.media.values())  # a copy
    assert len(events(store, SAFETY_FAILED)) == 2
    # SPEC 7.1: in the first month evergreen copies need parent approval too
    assert it.approval is Approval.PENDING and servable(store, "story") is None
    store.content.approve(it.id)
    assert servable(store, "story").id == it.id


def test_evergreen_copy_after_first_month_is_auto(store, clients, clock):
    Pipeline(store, clients, clock).bootstrap()
    store.settings.approval_required_until = date(2026, 3, 1)
    clients.claude = FakeClaude(responder({"story_review": [FAIL, FAIL]}))
    assert Pipeline(store, clients, clock).job_story() == "fallback"
    it = servable(store, "story")
    assert it.meta["source"] == "evergreen" and it.approval is Approval.AUTO


def test_fallback_without_bootstrap_fails_cleanly(store, clients, clock):
    clients.claude = FakeClaude(responder({"story_review": [FAIL, FAIL]}))
    p = Pipeline(store, clients, clock)
    assert p.job_story() == "failed"
    assert any("evergreen" in e.payload["error"] for e in events(store, CONTENT_ERROR))


def test_reviewer_error_withholds_but_is_not_a_safety_failure(store, clients, clock):
    clients.claude = FakeClaude(responder({"story_review": [ClaudeError("boom")]}))
    p = Pipeline(store, clients, clock)
    assert p.job_story() == "generated"  # attempt 2 reviewed fine
    assert events(store, SAFETY_FAILED) == []
    assert store.content.list("story", safety="failed") == []
    errs = events(store, CONTENT_ERROR)
    assert len(errs) == 1 and errs[0].payload["reason"] == "review_unavailable"
    assert "ReviewUnavailable" in errs[0].payload["error"]
    # the unreviewed first story was never stored
    assert len(store.content.list("story")) == 1


def test_reviewer_outage_falls_back_without_safety_failed(store, clients, clock):
    Pipeline(store, clients, clock).bootstrap()
    clients.claude = FakeClaude(responder({"story_review": [ClaudeError("a"), ClaudeError("b")]}))
    assert Pipeline(store, clients, clock).job_story() == "fallback"
    assert events(store, SAFETY_FAILED) == []
    assert [i.meta["source"] for i in store.content.list("story")] == ["evergreen"]


def test_reviewer_non_retryable_error_skips_retry(store, clients, clock):
    clients.claude = FakeClaude(responder({"story_review": [ClaudeError("bad key",
                                                                           retryable=False)]}))
    p = Pipeline(store, clients, clock)
    assert p.job_story() == "failed"  # no bootstrap -> no evergreen
    assert [c.purpose for c in clients.claude.calls] == ["story", "story_review"]
    assert events(store, SAFETY_FAILED) == []
    assert all(i.safety is not Safety.FAILED for i in store.content.list("story"))


def test_image_reviewer_outage_is_not_a_safety_failure(store, clients, clock):
    clients.claude = FakeClaude(responder({"coloring_image_review": [ClaudeError("x")]}))
    p = Pipeline(store, clients, clock)
    assert p.job_coloring() == "generated"  # second image reviewed fine
    assert events(store, SAFETY_FAILED) == []
    assert any(e.payload.get("reason") == "review_unavailable"
               for e in events(store, CONTENT_ERROR))


def test_image_reviewer_down_for_coloring_is_an_error_not_rejection(store, clients, clock):
    clients.claude = FakeClaude(responder({"coloring_image_review": [ClaudeError("x")] * 4}))
    assert Pipeline(store, clients, clock).job_coloring() == "failed"
    assert events(store, SAFETY_FAILED) == []
    assert store.content.list("coloring", safety="failed") == []


def test_refusal_is_retried_then_falls_back(store, clients, clock):
    Pipeline(store, clients, clock).bootstrap()
    clients.claude = FakeClaude(responder({"story": [ClaudeRefusal("no"), ClaudeRefusal("no")]}))
    p = Pipeline(store, clients, clock)
    assert p.job_story() == "fallback"
    assert len([c for c in clients.claude.calls if c.purpose == "story"]) == 2
    # refused calls are still billed -> logged
    assert sum(e.payload["purpose"] == "story" for e in events(store, "spend")) == 2


def test_non_retryable_error_skips_retry(store, clients, clock):
    clients.claude = FakeClaude(responder({"story": [ClaudeError("bad key", retryable=False)]}))
    p = Pipeline(store, clients, clock)
    assert p.job_story() == "failed"
    assert [c.purpose for c in clients.claude.calls] == ["story"]


def test_coloring_image_rejected_retries_image(store, clients, clock, media):
    clients.claude = FakeClaude(responder({"coloring_image_review": [FAIL]}))
    p = Pipeline(store, clients, clock)
    assert p.job_coloring() == "generated"
    assert len(media.image_prompts) == 2
    ev = events(store, SAFETY_FAILED)
    assert len(ev) == 1 and ev[0].payload["stage"] == "image"


def test_coloring_all_images_rejected_falls_back(store, clients, clock):
    Pipeline(store, clients, clock).bootstrap()
    clients.claude = FakeClaude(responder({"coloring_image_review": [FAIL] * 4}))
    p = Pipeline(store, clients, clock)
    assert p.job_coloring() == "fallback"
    assert servable(store, "coloring") is None  # first month: pending approval
    approve_all(store)
    it = servable(store, "coloring")
    assert it.meta["source"] == "evergreen" and store.media_exists(it.media["art"])
    # no orphaned generated directories with rejected art left behind
    gen_dirs = list((store.media_dir / "content" / "coloring").rglob("art.png"))
    assert all(str(p_).find("evergreen") < 0 for p_ in gen_dirs)
    assert len(gen_dirs) == 1  # only the evergreen copy


def test_letter_drawing_rejected_keeps_letter_without_drawing(b_letter, store, clients, clock):
    clients.claude = FakeClaude(responder({"letter_image_review": [FAIL, FAIL]}))
    p = Pipeline(store, clients, clock)
    assert p.job_letter() == "generated"
    it = [i for i in store.content.list("letter") if i.safety is Safety.PASSED
          and i.meta.get("source") == "generated"][0]
    assert "drawing" not in it.media and it.meta["letter"] == "B"
    assert len(events(store, SAFETY_FAILED)) == 2


def test_letter_bad_words_fall_back_to_bundled(store, clients, clock):
    Pipeline(store, clients, clock).bootstrap()
    bad = {"words": ["ball", "bear", "boat"]}  # the letter is A -> unusable twice
    clients.claude = FakeClaude(responder({"letter_words": [bad, bad]}))
    p = Pipeline(store, clients, clock)
    assert p.job_letter() == "fallback"
    it = [i for i in store.content.list("letter") if i.meta.get("source") == "evergreen"][0]
    assert it.meta["letter"] == "A" and it.meta["words"] == list(eg.LETTER_WORDS["A"])
    assert {"intro", "word_0", "word_1", "word_2"} == set(it.media)


def test_weather_falls_back_to_template(store, clients, clock):
    clients.claude = FakeClaude(responder({"weather": [ClaudeError("x"), ClaudeError("y")]}))
    p = Pipeline(store, clients, clock)
    assert p.job_weather() == "fallback"
    it = store.content.list("weather")[0]
    assert it.meta["source"] == "template" and it.safety is Safety.PASSED
    assert it.meta["sentence"] == "Today it is sunny and cold, so wear your warm coat and hat!"


def test_weather_fallback_uses_prerendered_audio_at_spend_cap(store, clients, clock, media):
    out = Pipeline(store, clients, clock).bootstrap_evergreen()
    assert sum(k.startswith("weather:") and v == "ready" for k, v in out.items()) == 32
    assert "Today it is rainy and chilly, so wear your raincoat and boots!" in media.tts_texts
    store.settings.spend_cap_usd = 0.0
    clients.weather = FakeWeather(code=63, hi_c=11.0, lo_c=4.0)
    n_tts = len(media.tts_texts)
    p = Pipeline(store, clients, clock)
    assert p.job_weather() == "fallback"
    assert len(media.tts_texts) == n_tts  # no paid call
    it = store.content.list("weather")[0]
    assert it.meta["source"] == "template" and it.meta["evergreen_audio"] is True
    assert it.meta["icon"] == "rain" and it.meta["hi_c"] == 11.0 and it.meta["lo_c"] == 4.0
    assert it.meta["sentence"] == "Today it is rainy and chilly, so wear your raincoat and boots!"
    assert it.for_date == store.weather_day()
    assert store.media_exists(it.media["audio"])
    assert not it.media["audio"].startswith("evergreen/")
    approve_all(store)
    assert servable(store, "weather").id == it.id


def test_weather_fallback_uses_prerendered_audio_when_tts_down(store, clients, clock):
    Pipeline(store, clients, clock).bootstrap_evergreen()
    clients.media = FakeMedia(fail_tts=True)
    assert Pipeline(store, clients, clock).job_weather() == "fallback"
    assert store.content.list("weather")[0].meta["evergreen_audio"] is True


def test_template_bands_cover_every_forecast():
    from zozobox.content.weather import FEEL_BANDS, TEMPLATE_ICONS, feel_slug, template_text
    for icon in TEMPLATE_ICONS:
        for hi in (-10, 0, 4.9, 5, 10, 13.9, 14, 20, 23.9, 24, 35):
            band = FEEL_BANDS[feel_slug(hi)]
            assert template_text(icon, hi) == template_text(icon, band)


def test_weather_without_location_or_forecast(store, clients, clock):
    store.settings.set("location", None)
    p = Pipeline(store, clients, clock)
    assert p.job_weather() == "failed"
    store.settings.set_location("Home", 1, 2)
    clients.weather = FakeWeather(error=WeatherError("down"))
    assert p.job_weather() == "failed"
    assert len(events(store, CONTENT_ERROR)) == 2


def test_weather_force_refresh(store, clients, clock):
    store.settings.approval_required_until = date(2026, 3, 1)
    p = Pipeline(store, clients, clock)
    assert p.job_weather() == "generated"
    assert p.job_weather() == "skipped"
    clients.weather = FakeWeather(code=63, hi_c=12)
    assert p.job_weather(force=True) == "generated"
    assert servable(store, "weather").meta["icon"] == "rain"


# ------------------------------------------------------------ robustness ----


def test_partial_failure_does_not_abort_other_jobs(store, clients, clock):
    clients.media = FakeMedia(fail_tts=lambda t: "so wear" in t)
    clients.weather = FakeWeather()
    p = Pipeline(store, clients, clock)
    res = p.run_nightly()
    assert res["weather"] == "failed"
    assert res["story"] == res["coloring"] == "generated"


def test_unexpected_exception_is_contained(store, clients, clock):
    clients.claude = FakeClaude(responder({"story": [KeyError("x"), KeyError("y")]}))
    p = Pipeline(store, clients, clock)
    res = p.run_nightly(jobs=("story", "coloring"))
    assert res == {"story": "failed", "coloring": "generated"}


def test_job_timeout(store, clients, clock):
    gate = threading.Event()

    def slow(call):
        if call.purpose == "story":
            gate.wait(5)
        return default_fake_response(call)

    clients.claude = FakeClaude(slow)
    p = Pipeline(store, clients, clock)
    try:
        assert p.run_job("story", p.job_story, timeout_s=0.2) == "timeout"
        assert any(e.payload.get("reason") == "timeout" for e in events(store, CONTENT_ERROR))
        assert p.run_job("coloring", p.job_coloring, timeout_s=5) == "generated"
    finally:
        gate.set()
        assert p.wait_stragglers(5)


class BlockingTTS(FakeMedia):
    """TTS blocks on `gate` from the `block_at`-th call on."""

    def __init__(self, block_at: int = 2):
        super().__init__()
        self.gate, self.blocked = threading.Event(), threading.Event()
        self.block_at, self.n = block_at, 0

    def tts(self, text):
        self.n += 1
        if self.n >= self.block_at:
            self.blocked.set()
            self.gate.wait(10)
        return super().tts(text)


def _leftovers(store):
    dirs = [d for d in (store.media_dir / "content").rglob("*")
            if d.is_dir() and len(d.relative_to(store.media_dir).parts) == 6]
    tts = list(store.data_dir.glob(".tts-*"))
    return dirs, tts


def test_timed_out_job_cancels_and_cleans_up(store, clients, clock):
    clients.media = BlockingTTS(block_at=2)  # story: chunk 0 done, blocks in chunk 1
    p = Pipeline(store, clients, clock)
    assert p.run_job("story", p.job_story, timeout_s=0.3) == "timeout"
    assert clients.media.blocked.is_set()
    assert _leftovers(store)[0]  # chunk 0 written into the item dir
    clients.media.gate.set()  # the API call returns after the timeout
    assert p.wait_stragglers(5)
    # the straggler stopped at its next checkpoint: no item, no partial media
    assert store.content.list("story") == []
    assert _leftovers(store) == ([], [])
    # no paid call after the cancel (chunk 1's call was already in flight)
    assert clients.media.n == 2


def test_run_holds_lock_until_timed_out_job_exits(store, clients, clock):
    from zozobox.content import ContentConfig

    clients.media = BlockingTTS(block_at=1)
    cfg = ContentConfig(job_timeout_s=0.2, cancel_grace_s=10)
    p = Pipeline(store, clients, clock, cfg)
    done = threading.Event()
    out = {}

    def run():
        out["r"] = p.run_nightly(jobs=("story",))
        done.set()

    t = threading.Thread(target=run)
    t.start()
    try:
        assert clients.media.blocked.wait(5)
        time.sleep(0.5)  # past the job timeout: the run waits for the straggler
        assert not done.is_set()
        assert Pipeline(store, clients, clock).run_nightly(jobs=("story",)) == {"locked": True}
    finally:
        clients.media.gate.set()
        t.join(10)
    assert out["r"] == {"story": "timeout"}
    assert store.content.list("story") == []
    assert _leftovers(store) == ([], [])


def test_straggler_past_grace_makes_job_busy_not_duplicated(store, clients, clock):
    from zozobox.content import ContentConfig

    media = BlockingTTS(block_at=1)
    clients.media = media
    cfg = ContentConfig(job_timeout_s=0.2, cancel_grace_s=0.1)
    p = Pipeline(store, clients, clock, cfg)
    try:
        assert p.run_nightly(jobs=("story",)) == {"story": "timeout"}
        # lock released, straggler still blocked: a new run must not start it again
        res = Pipeline(store, clients, clock, cfg).run_nightly(jobs=("story",))
        assert res == {"story": "busy"}
        assert any(e.payload.get("reason") == "busy" for e in events(store, CONTENT_ERROR))
    finally:
        media.gate.set()
    assert p.wait_stragglers(5)
    assert store.content.list("story") == []
    clients.media = FakeMedia()
    assert Pipeline(store, clients, clock).run_nightly(jobs=("story",)) == {"story": "generated"}


def test_sweep_removes_orphans_from_killed_run(store, clients, clock):
    p = Pipeline(store, clients, clock)
    assert p.job_story() == "generated"
    keep = store.content.list("story")[0]
    orphan = store.media_dir / "content" / "story" / "2026" / "03" / "09" / "deadbeef0000"
    orphan.mkdir(parents=True)
    (orphan / "chunk_0.wav").write_bytes(b"x")
    tmp = store.data_dir / ".tts-abc123"
    tmp.mkdir()
    (tmp / "raw.wav").write_bytes(b"x")
    assert p.sweep_orphans() == {"tts_tmp": 1, "media_dirs": 1}
    assert not orphan.exists() and not tmp.exists()
    assert all(store.media_exists(r) for r in keep.media.values())
    # run_nightly sweeps first
    orphan.mkdir(parents=True)
    p.run_nightly(jobs=("weather",))
    assert not orphan.exists()


def test_failed_tts_cleans_up_partial_media(store, clients, clock):
    n = {"i": 0}

    def fail_third(text):
        n["i"] += 1
        return n["i"] == 3

    clients.media = FakeMedia(fail_tts=fail_third)
    p = Pipeline(store, clients, clock)
    assert p.job_story() == "generated"  # attempt 2 succeeds
    dirs = [d for d in (store.media_dir / "content" / "story").rglob("*") if d.is_dir()
            and any(d.glob("*.wav"))]
    assert len(dirs) == 1


def test_concurrent_run_is_locked(store, clients, clock):
    gate, started = threading.Event(), threading.Event()

    def slow(call):
        started.set()
        gate.wait(5)
        return default_fake_response(call)

    clients.claude = FakeClaude(slow)
    p = Pipeline(store, clients, clock)
    t = threading.Thread(target=p.run_nightly, kwargs={"jobs": ("story",)})
    t.start()
    try:
        assert started.wait(5)
        assert Pipeline(store, clients, clock).run_nightly() == {"locked": True}
    finally:
        gate.set()
        t.join(5)


# ------------------------------------------------------------ spend cap ----


def test_spend_cap_blocks_paid_calls(store, clients, clock, claude, media):
    store.events.append("spend", {"provider": "openai", "model": "x", "purpose": "t",
                                  "usd": 30.0, "input_units": 0, "output_units": 0})
    res = Pipeline(store, clients, clock).run_nightly()
    assert res == {"weather": "capped", "story": "capped", "letter": "capped",
                   "coloring": "capped"}
    assert claude.calls == [] and media.tts_texts == [] and media.image_prompts == []
    assert len(events(store, "spend_cap_reached")) == 1


def test_spend_cap_serves_evergreen(store, clients, clock):
    Pipeline(store, clients, clock).bootstrap()
    store.settings.spend_cap_usd = 0.0
    n_calls = len(clients.claude.calls)
    res = Pipeline(store, clients, clock).run_nightly(jobs=("story", "coloring", "letter"))
    assert res == {"story": "fallback", "coloring": "fallback", "letter": "fallback"}
    assert len(clients.claude.calls) == n_calls


def test_spend_cap_only_counts_this_month(store, clients, clock):
    store.events.append("spend", {"usd": 100.0}, ts=clock().replace(month=2))
    assert Pipeline(store, clients, clock).job_story() == "generated"


def test_cap_hit_mid_run(store, clients, clock):
    store.settings.spend_cap_usd = 0.05  # a few calls' worth
    res = Pipeline(store, clients, clock).run_nightly()
    assert "capped" in res.values()
    assert len(events(store, "spend_cap_reached")) == 1
    spend = sum(e.payload["usd"] for e in events(store, "spend"))
    assert spend < 0.05 + 0.05  # at most one call past the line


# ------------------------------------------------------------ bootstrap ----


def test_bootstrap_phrases_and_evergreen(store, clients, clock, media):
    p = Pipeline(store, clients, clock)
    out = p.bootstrap()
    assert set(out["phrases"]) == set(PHRASES)
    assert set(out["phrases"].values()) == {"generated"}
    # SPEC 7.1: first month -> phrases wait for parent approval like every item
    assert all(store.content.phrase(name) is None for name in PHRASES)
    st = p.status()
    assert set(st["phrases_missing"]) == set(PHRASES)
    assert st["phrases_pending"] == sorted(PHRASES)
    approve_all(store)
    book = StorePhraseBook(store)
    for name in PHRASES:
        path = book.path_for(name)
        assert path is not None and path.is_file()
        item = store.content.phrase(name)
        assert item.approval is Approval.APPROVED and item.meta["text"] == eg.PHRASE_TEXTS[name]
    assert set(out["evergreen"].values()) == {"ready"}
    st = p.status()
    assert st["phrases_missing"] == [] and st["phrases_pending"] == []
    assert st["evergreen_ready"] == {"story": len(eg.STORIES), "coloring": len(eg.COLORING),
                                     "letter": 26, "weather": 32}
    n = len(media.tts_texts)
    again = p.bootstrap()
    assert set(again["phrases"].values()) == {"skipped"}
    assert len(media.tts_texts) == n


def test_bootstrap_rerun_skips_pending_phrases(store, clients, clock, media):
    p = Pipeline(store, clients, clock)
    p.bootstrap_phrases()
    n = len(media.tts_texts)
    assert set(p.bootstrap_phrases().values()) == {"skipped"}
    assert len(media.tts_texts) == n
    assert len(store.content.list("phrase")) == len(PHRASES)


def test_phrases_after_first_month_are_auto(store, clients, clock):
    store.settings.approval_required_until = date(2026, 3, 1)
    Pipeline(store, clients, clock).bootstrap_phrases()
    assert store.content.phrase("sleepy").approval is Approval.AUTO


def test_bootstrap_force_supersedes_phrases(store, clients, clock):
    p = Pipeline(store, clients, clock)
    p.bootstrap_phrases()
    approve_all(store)
    old = store.content.phrase("sleepy")
    p.bootstrap_phrases(force=True)
    # the new take is pending approval: the old one stays in use meanwhile
    assert store.content.phrase("sleepy").id == old.id
    pending = [i for i in store.content.pending_approval() if i.meta["key"] == "sleepy"]
    assert len(pending) == 1
    # forcing again replaces the unapproved take instead of piling them up
    p.bootstrap_phrases(force=True)
    pending2 = [i for i in store.content.pending_approval() if i.meta["key"] == "sleepy"]
    assert len(pending2) == 1 and pending2[0].id != pending[0].id
    assert store.content.get(pending[0].id).superseded
    store.content.approve(pending2[0].id)
    assert store.content.phrase("sleepy").id == pending2[0].id


def test_bootstrap_force_supersedes_phrases_after_first_month(store, clients, clock):
    store.settings.approval_required_until = date(2026, 3, 1)
    p = Pipeline(store, clients, clock)
    p.bootstrap_phrases()
    old = store.content.phrase("sleepy")
    p.bootstrap_phrases(force=True)
    new = store.content.phrase("sleepy")
    assert new.id != old.id and store.content.get(old.id).superseded


def test_phrase_texts_cover_engine_phrases():
    assert set(eg.PHRASE_TEXTS) == set(PHRASES)
    assert all(0 < len(t.split()) <= 15 for t in eg.PHRASE_TEXTS.values())


def test_evergreen_image_rejected_is_not_ready(store, clients, clock):
    clients.claude = FakeClaude(responder({"evergreen_coloring_image_review": [FAIL, FAIL]}))
    p = Pipeline(store, clients, clock)
    out = p.bootstrap_evergreen()
    failed = [k for k, v in out.items() if v == "failed"]
    assert len(failed) == 1 and failed[0].startswith("coloring:")
    assert p.status()["evergreen_ready"]["coloring"] == len(eg.COLORING) - 1


# ------------------------------------------------------------ helpers ----


def test_choose_letter_cycles_and_avoids_recent(store, pipeline):
    for L in "XYZ":
        store.content.add("letter", meta={"letter": L}, safety="passed")
    assert pipeline.choose_letter() == "A"
    store.content.add("letter", meta={"letter": "A"}, safety="passed")
    assert pipeline.choose_letter() == "B"


def test_letter_x_accepts_two_words(store, clients, clock):
    for L in "ABCDEFGHIJKLMNOPQRSTUVW":
        it = store.content.add("letter", meta={"letter": L, "words": ["w"]}, safety="passed")
        store.content.mark_served(it.id)
        clock.advance(minutes=1)
    clients.claude = FakeClaude(responder({"letter_words": [{"words": ["xylophone", "x-ray"]}]}))
    p = Pipeline(store, clients, clock)
    assert p.choose_letter() == "X"
    assert p.job_letter() == "generated"
    assert [c.purpose for c in clients.claude.calls].count("letter_words") == 1
    it = store.content.list("letter", limit=1)[0]
    assert it.meta["letter"] == "X" and it.meta["words"] == ["xylophone", "x-ray"]
    assert "Pick 2 to 4 words" in clients.claude.calls[0].text()
    assert events(store, CONTENT_ERROR) == []


def test_min_words_per_letter():
    assert Pipeline.min_words("x") == 2 and Pipeline.min_words("B") == 3
    assert Pipeline.parse_words("x", {"words": ["xylophone", "x-ray"]}) == ["xylophone", "x-ray"]
    with pytest.raises(GenerationError):
        Pipeline.parse_words("x", {"words": ["xylophone"]})
    with pytest.raises(GenerationError):
        Pipeline.parse_words("b", {"words": ["ball", "bear"]})
    # every bundled evergreen letter meets its own minimum
    assert all(len(w) >= Pipeline.min_words(L) for L, w in eg.LETTER_WORDS.items())


def test_parse_words():
    assert Pipeline.parse_words("B", {"words": ["Ball", "bear!", "apple", "b@d", "ball",
                                                "bus", "bee", "bike", "bird"]}) == \
        ["ball", "bear", "bus", "bee", "bike"]
    with pytest.raises(GenerationError):
        Pipeline.parse_words("B", {"words": ["ball", "apple"]})
    with pytest.raises(GenerationError):
        Pipeline.parse_words("B", {"words": ["ball", "bear", "bus"]}, avoid=["bus"])


def test_parse_story_checks_length(pipeline):
    with pytest.raises(GenerationError):
        pipeline.parse_story({"title": "t", "chunks": ["Zozo ran."] * 4})
    with pytest.raises(GenerationError):
        pipeline.parse_story({"title": "t", "chunks": ["word " * 40] * 4})  # no Zozo
    with pytest.raises(GenerationError):
        pipeline.parse_story({"title": "t", "chunks": "nope"})


def test_rechunk():
    assert rechunk(["a.", "b.", "c."]) == ["a.", "b.", "c."]
    one = " ".join(f"Zozo hops {i} times." for i in range(12))
    out = rechunk([one])
    assert len(out) == 4 and " ".join(out) == one
    many = [f"Part {i}." for i in range(9)]
    out = rechunk(many)
    assert 3 <= len(out) <= 5 and " ".join(out) == " ".join(many)
    assert rechunk(["Only one."]) == ["Only one."]
    assert rechunk([]) == []


def test_status_without_clients(store):
    from zozobox.content import status

    st = status(store)
    assert st["needs_generation"] == {"weather": True, "story": True, "letter": True,
                                      "coloring": True}
    assert st["month_to_date_usd"] == 0 and st["spend_cap_usd"] == 30.0
    assert len(st["phrases_missing"]) == len(PHRASES)


def test_content_queued_for_archive(store, pipeline):
    pipeline.job_story()
    it = store.content.list("story")[0]
    keys = {o.remote_key for o in store.outbox.pending(limit=100)}
    assert set(it.media.values()) <= keys
    assert f"content/index/{it.id}.json" in keys


def test_content_run_event(store, pipeline):
    pipeline.run_nightly(jobs=("story",))
    ev = events(store, "content_run")
    assert ev[-1].payload["results"] == {"story": "generated"} and ev[-1].payload["usd"] > 0


def test_evergreen_image_rejection_is_flagged_as_coloring(store, clients, clock):
    clients.claude = FakeClaude(responder({"evergreen_coloring_image_review": [FAIL]}))
    Pipeline(store, clients, clock).bootstrap_evergreen()
    failed = store.content.list("coloring", safety="failed")
    assert len(failed) == 1 and failed[0].meta["source"] == "evergreen_bootstrap"
    assert events(store, SAFETY_FAILED)[0].payload["item_id"] == failed[0].id


def test_story_item_plays_in_engine_builder(store, pipeline):
    """The engine's story builder accepts what the pipeline stores."""
    from zozobox.engine import builders

    store.settings.approval_required_until = date(2026, 3, 1)
    pipeline.job_story()
    item = servable(store, "story")
    keys = builders.story_chunk_keys(item)
    assert keys == [f"chunk_{i}" for i in range(4)]
    assert all(store.media_path(item.media[k]).is_file() for k in keys)


def test_real_ffmpeg_normalizer(store, clients, clock):
    import shutil as sh

    if sh.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    from zozobox.content.pipeline import _default_normalizer

    clients.normalizer = _default_normalizer
    p = Pipeline(store, clients, clock)
    rel = p._tts("content/test/x.wav", "hello", "test_tts")
    import wave

    with wave.open(str(store.media_path(rel))) as w:
        assert w.getframerate() == 48000 and w.getnchannels() == 1
