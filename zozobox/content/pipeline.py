"""Nightly content pipeline (SPEC 7.0/7.1, PLAN M6).

    from zozobox.content import Clients, run_nightly
    results = run_nightly(store, Clients.from_env(), clock)

Jobs (each isolated: one failing or timing out never stops the others):
- weather: refreshed every day for `store.weather_day()`.
- story, letter, coloring: only when `content.needs_generation(kind)` (the
  previous item was served), so unserved items carry over.

Every generated text and image goes through an independent Claude review
(the safety gate). A rejection or a generation error is retried once; then
the job falls back to bundled evergreen content (pre-vetted texts whose audio
and images `bootstrap` made; weather falls back to template wording whose
audio bootstrap pre-rendered for every icon/feel combination). Rejections are
stored as items with safety `failed` and logged as `safety_failed` events for
the dashboard. A reviewer that cannot be reached (outage, bad key) is not a
rejection: the content is withheld and a `content_error` is logged.

Every stored item, evergreen copies and phrases included, goes through the
store's first-month approval gate (SPEC 7.1): approval is left to the store
default, so it is `pending` while settings.approval_required(day) holds.

A job that exceeds its timeout is cancelled cooperatively: it stops at its
next checkpoint (before any paid call or store write) and removes its partial
media. The run holds its lock until such stragglers exit (up to
`cancel_grace_s`); a job whose straggler is still alive is reported `busy` by
later runs in the same process. Each run first sweeps `.tts-*` temp dirs and
generated media dirs that no item references (left by a killed process).

Every paid call logs a `spend` event; at the monthly cap (settings
spend_cap_usd) paid calls are refused and `spend_cap_reached` is logged.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import random
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from . import evergreen as eg
from . import prompts as P
from .claude import AnthropicClaude, ClaudeAPI, ClaudeError, DEFAULT_CLAUDE_MODEL, image_block
from .openai_media import (
    DEFAULT_IMAGE_MODEL, DEFAULT_STT_MODEL, DEFAULT_TTS_MODEL, DEFAULT_VOICE,
    DEFAULT_VOICE_INSTRUCTIONS, MediaAPI, MediaError, OpenAIMedia,
)
from .spend import SpendCapReached, SpendMeter
from .weather import (FEEL_BANDS, TEMPLATE_ICONS, OpenMeteo, WeatherAPI, feel_slug,
                      template_text, template_weather)

log = logging.getLogger(__name__)

SAFETY_FAILED = "safety_failed"
CONTENT_ERROR = "content_error"
CONTENT_RUN = "content_run"

GENERATED_KINDS = ("story", "letter", "coloring")
NIGHTLY_JOBS = ("weather", "story", "letter", "coloring")


# --------------------------------------------------------------- config ----


@dataclass(frozen=True)
class ContentConfig:
    claude_model: str = DEFAULT_CLAUDE_MODEL
    image_model: str = DEFAULT_IMAGE_MODEL
    tts_model: str = DEFAULT_TTS_MODEL
    stt_model: str = DEFAULT_STT_MODEL
    voice: str = DEFAULT_VOICE
    voice_instructions: str = DEFAULT_VOICE_INSTRUCTIONS
    image_quality: str = "medium"
    gen_effort: str = "medium"
    review_effort: str = "low"  # pass/fail classification
    job_timeout_s: float = 900.0
    # Per HTTP attempt; the SDKs retry max_retries times, so one call is
    # bounded by about api_timeout_s * (api_max_retries + 1).
    api_timeout_s: float = 120.0
    api_max_retries: int = 1
    cancel_grace_s: float = 30.0  # wait for a timed-out job to stop
    story_words: tuple[int, int] = (90, 220)  # accepted range (target 130-160)
    recent_n: int = 14  # recent titles/themes to avoid
    recent_letters: int = 10
    letters: str = eg.LETTERS
    review_image_px: int = 768

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ContentConfig":
        """Non-secret overrides: ZOZOBOX_CLAUDE_MODEL, ZOZOBOX_IMAGE_MODEL,
        ZOZOBOX_TTS_MODEL, ZOZOBOX_STT_MODEL, ZOZOBOX_TTS_VOICE,
        ZOZOBOX_IMAGE_QUALITY, ZOZOBOX_CONTENT_JOB_TIMEOUT_S."""
        env = os.environ if env is None else env
        c = cls()
        m = {
            "ZOZOBOX_CLAUDE_MODEL": "claude_model",
            "ZOZOBOX_IMAGE_MODEL": "image_model",
            "ZOZOBOX_TTS_MODEL": "tts_model",
            "ZOZOBOX_STT_MODEL": "stt_model",
            "ZOZOBOX_TTS_VOICE": "voice",
            "ZOZOBOX_IMAGE_QUALITY": "image_quality",
        }
        changes: dict[str, Any] = {f: env[k] for k, f in m.items() if env.get(k)}
        t = env.get("ZOZOBOX_CONTENT_JOB_TIMEOUT_S")
        if t:
            try:
                changes["job_timeout_s"] = float(t)
            except ValueError:
                log.warning("content: bad ZOZOBOX_CONTENT_JOB_TIMEOUT_S=%r", t)
        return replace(c, **changes)


def _default_normalizer(src: Path, dst: Path) -> Any:
    from zozobox.audio import normalize

    return normalize(src, dst)


@dataclass
class Clients:
    claude: ClaudeAPI
    media: MediaAPI
    weather: WeatherAPI
    # (src any-format, dst WAV) -> loudness-normalised to -16 LUFS
    normalizer: Callable[[Path, Path], Any] = _default_normalizer

    @classmethod
    def from_env(cls, config: ContentConfig | None = None) -> "Clients":
        """Real clients. Keys come from ANTHROPIC_API_KEY / OPENAI_API_KEY,
        read by the SDKs themselves on first use."""
        c = config or ContentConfig.from_env()
        return cls(
            claude=AnthropicClaude(c.claude_model, timeout_s=c.api_timeout_s,
                                   max_retries=c.api_max_retries),
            media=OpenAIMedia(
                image_model=c.image_model, tts_model=c.tts_model, stt_model=c.stt_model,
                voice=c.voice, voice_instructions=c.voice_instructions,
                image_quality=c.image_quality, timeout_s=c.api_timeout_s,
                max_retries=c.api_max_retries,
            ),
            weather=OpenMeteo(),
        )


# ------------------------------------------------------------ errors etc ----


class GenerationError(RuntimeError):
    """The model's output was unusable (wrong shape, too long...)."""


class SafetyRejected(RuntimeError):
    def __init__(self, stage: str, reasons: list[str]):
        super().__init__(f"{stage} rejected: {'; '.join(reasons) or 'no reason given'}")
        self.stage = stage
        self.reasons = reasons


class ReviewUnavailable(ClaudeError):
    """The safety reviewer could not give a verdict (network, 5xx, auth,
    refusal, bad output). The content is withheld, but this is an error, not
    a safety failure. Keeps the cause's `retryable`."""


class JobCancelled(BaseException):
    """Raised at a checkpoint in a job that exceeded its timeout. A
    BaseException so the jobs' `except Exception` handlers cannot swallow it;
    the `except BaseException: cleanup; raise` blocks still run."""


# Letters with few toddler-familiar words: accept fewer words for them.
MIN_WORDS: dict[str, int] = {"X": 2}
DEFAULT_MIN_WORDS = 3

# (data_dir, job) -> the thread running it; survives Pipeline instances so a
# later run in the same process never starts a job whose straggler still runs.
_LIVE: dict[tuple[str, str], threading.Thread] = {}
_LIVE_LOCK = threading.Lock()


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Story:
    title: str
    theme: str
    value: str
    chunks: list[str]

    @property
    def text(self) -> str:
        return " ".join(self.chunks)


_SENT = re.compile(r"(?<=[.!?])\s+")
_WORD_OK = re.compile(r"^[a-z][a-z' -]{0,18}[a-z]$|^[a-z]$")


def _words(text: str) -> int:
    return len(text.split())


def rechunk(chunks: list[str], lo: int = 3, hi: int = 5, target: int = 4) -> list[str]:
    """Keep 3-5 chunks as given; otherwise regroup whole sentences into ~4
    chunks of roughly equal word count."""
    chunks = [c.strip() for c in chunks if isinstance(c, str) and c.strip()]
    if lo <= len(chunks) <= hi:
        return chunks
    sentences = [s for c in chunks for s in _SENT.split(c) if s.strip()]
    if not sentences:
        return []
    k = max(1, min(target, len(sentences)))
    total = sum(_words(s) for s in sentences)
    out: list[str] = []
    cur: list[str] = []
    acc = 0
    for i, s in enumerate(sentences):
        cur.append(s)
        acc += _words(s)
        left_sent = len(sentences) - i - 1
        left_groups = k - len(out) - 1
        if left_groups > 0 and (acc >= total * (len(out) + 1) / k or left_sent == left_groups):
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _clean(s: Any, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


# -------------------------------------------------------------- pipeline ----


class Pipeline:
    def __init__(
        self,
        store,
        clients: Clients,
        clock: Callable[[], datetime] | None = None,
        config: ContentConfig | None = None,
        rng: random.Random | None = None,
    ):
        self.store = store
        self.clients = clients
        self.clock = clock or store.now
        self.config = config or ContentConfig()
        self.rng = rng or random.Random()
        self.meter = SpendMeter(store, self.clock)
        self._cancel: dict[threading.Thread, threading.Event] = {}
        self._stragglers: list[threading.Thread] = []

    # -- cancellation ---------------------------------------------------------
    def _checkpoint(self) -> None:
        """Raise JobCancelled if the current job thread timed out."""
        ev = self._cancel.get(threading.current_thread())
        if ev is not None and ev.is_set():
            raise JobCancelled()

    # -- logging helpers ------------------------------------------------------
    def _event(self, type_: str, payload: dict[str, Any]) -> None:
        try:
            self.store.events.append(type_, payload)
        except Exception:
            log.exception("content: could not log %s event", type_)

    def _error(self, job: str, err: BaseException, **extra: Any) -> None:
        log.warning("content: %s: %s", job, err)
        self._event(CONTENT_ERROR, {"job": job, "error": f"{type(err).__name__}: {err}"[:500],
                                    **extra})

    # -- paid calls -----------------------------------------------------------
    def _claude(self, purpose: str, system: str, content, schema: dict, *, effort: str,
                max_tokens: int = 16000) -> dict[str, Any]:
        self._checkpoint()
        self.meter.ensure_budget(purpose)
        try:
            res = self.clients.claude.generate_json(
                purpose=purpose, system=system, content=content, schema=schema,
                effort=effort, max_tokens=max_tokens,
            )
        except ClaudeError as e:
            self.meter.record(e.usage, purpose)
            raise
        self.meter.record(res.usage, purpose)
        self._checkpoint()
        return res.data

    def _tts(self, rel: str, text: str, purpose: str) -> str:
        """TTS `text`, loudness-normalise into media/<rel> (WAV). Returns rel."""
        self._checkpoint()
        self.meter.ensure_budget(purpose)
        try:
            res = self.clients.media.tts(text)
        except MediaError as e:
            self.meter.record(e.usage, purpose)
            raise
        self.meter.record(res.usage, purpose)
        self._checkpoint()
        dest = self.store.media_path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.store.data_dir, prefix=".tts-") as td:
            raw = Path(td) / f"raw.{res.ext or 'wav'}"
            raw.write_bytes(res.data)
            self.clients.normalizer(raw, dest)
        if not dest.is_file():
            raise GenerationError(f"normalised audio missing for {rel}")
        return rel

    def _image(self, prompt: str, purpose: str) -> bytes:
        self._checkpoint()
        self.meter.ensure_budget(purpose)
        try:
            res = self.clients.media.image(prompt)
        except MediaError as e:
            self.meter.record(e.usage, purpose)
            raise
        self.meter.record(res.usage, purpose)
        self._checkpoint()
        return res.data

    # -- safety gate ----------------------------------------------------------
    def _verdict(self, data: dict[str, Any]) -> Verdict:
        reasons = [str(r)[:300] for r in (data.get("reasons") or []) if str(r).strip()]
        if data.get("verdict") == "pass":
            return Verdict(True, [])
        return Verdict(False, reasons or ["reviewer did not pass it"])

    @staticmethod
    def _unavailable(e: Exception) -> ReviewUnavailable:
        return ReviewUnavailable(f"review unavailable: {type(e).__name__}: {e}"[:400],
                                 retryable=getattr(e, "retryable", True) is not False)

    def review_text(self, kind: str, text: str) -> Verdict:
        """Independent second Claude call. Raises ReviewUnavailable when no
        verdict could be had: unreviewed content is never served, but an
        outage is not reported as a safety failure."""
        try:
            data = self._claude(f"{kind}_review", P.REVIEW_SYSTEM,
                                P.review_text_prompt(kind, text), P.REVIEW_SCHEMA,
                                effort=self.config.review_effort, max_tokens=4000)
        except SpendCapReached:
            raise
        except Exception as e:
            raise self._unavailable(e) from None
        return self._verdict(data)

    def review_image(self, kind: str, data: bytes, meant: str) -> Verdict:
        try:
            png, mt = self._review_copy(data)
            out = self._claude(
                f"{kind}_image_review", P.REVIEW_SYSTEM,
                [image_block(png, mt), {"type": "text", "text": P.review_image_prompt(kind, meant)}],
                P.REVIEW_SCHEMA, effort=self.config.review_effort, max_tokens=4000,
            )
        except SpendCapReached:
            raise
        except Exception as e:
            raise self._unavailable(e) from None
        return self._verdict(out)

    def _review_copy(self, data: bytes) -> tuple[bytes, str]:
        """A smaller copy for the vision review (fewer tokens). Falls back to
        the original bytes when Pillow is unavailable or the image unreadable."""
        mt = "image/png"
        if data[:3] == b"\xff\xd8\xff":
            mt = "image/jpeg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mt = "image/webp"
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                im.load()
                px = self.config.review_image_px
                if max(im.size) <= px and mt == "image/png":
                    return data, mt
                im = im.convert("RGB")
                im.thumbnail((px, px))
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                return buf.getvalue(), "image/png"
        except Exception:
            return data, mt

    def _reject(self, kind: str, attempt: int, stage: str, reasons: list[str],
                meta: dict[str, Any], media: dict[str, str] | None = None,
                for_date: date | None = None) -> None:
        """Store the rejected candidate (safety failed, for the dashboard) and
        log a safety_failed event."""
        self._checkpoint()
        item_id = None
        try:
            item = self.store.content.add(
                kind, media=media or {}, meta={**meta, "rejected_stage": stage},
                for_date=for_date, safety="failed",
            )
            item = self.store.content.set_safety(item.id, "failed", "; ".join(reasons)[:1000])
            item_id = item.id
        except Exception:
            log.exception("content: could not store rejected %s", kind)
        self._event(SAFETY_FAILED, {"kind": kind, "stage": stage, "attempt": attempt,
                                    "reasons": reasons, "item_id": item_id})

    # -- media layout ---------------------------------------------------------
    def _item_dir(self, kind: str) -> str:
        local = self.store.settings.local(self.clock())
        return f"content/{kind}/{local:%Y/%m/%d}/{uuid.uuid4().hex[:12]}"

    def _remove_dir(self, rel_dir: str) -> None:
        try:
            shutil.rmtree(self.store.media_path(rel_dir), ignore_errors=True)
        except Exception:
            log.exception("content: could not clean %s", rel_dir)

    def _add_item(self, kind: str, media: dict[str, str], meta: dict[str, Any], *,
                  for_date: date | None = None, approval: str | None = None):
        self._checkpoint()
        item = self.store.content.add(kind, media=media, meta=meta, for_date=for_date,
                                      safety="passed", approval=approval)
        self._archive(item)
        return item

    def _archive(self, item) -> None:
        """Queue media and a metadata record for the R2 archive (SPEC 7.3)."""
        try:
            for rel in item.media.values():
                ct = "audio/wav" if rel.endswith(".wav") else "image/png"
                self.store.outbox.enqueue(rel, path=self.store.media_path(rel), content_type=ct)
            self.store.outbox.enqueue(
                f"content/index/{item.id}.json",
                record={"id": item.id, "kind": item.kind.value, "created_at": str(item.created_at),
                        "for_date": item.for_date.isoformat() if item.for_date else None,
                        "media": item.media, "meta": item.meta, "safety": item.safety.value,
                        "approval": item.approval.value},
                content_type="application/json",
            )
        except Exception:
            log.exception("content: could not queue %s for upload", item.id)

    # -- gate driver ----------------------------------------------------------
    def _gated(self, job: str, attempt: Callable[[int], Any],
               fallback: Callable[[], Any] | None) -> str:
        """Run `attempt(1)`, retry once, then the fallback. Returns a status."""
        capped = False
        for n in (1, 2):
            try:
                attempt(n)
                return "generated"
            except SpendCapReached:
                capped = True
                break
            except SafetyRejected:
                continue  # already stored and logged
            except Exception as e:
                extra = {"reason": "review_unavailable"} if isinstance(e, ReviewUnavailable) else {}
                self._error(job, e, attempt=n, **extra)
                if getattr(e, "retryable", True) is False:
                    break  # e.g. bad key or model name: retrying cannot help
        if fallback is None:
            return "capped" if capped else "failed"
        try:
            ok = fallback()
        except SpendCapReached:
            return "capped"
        except Exception as e:
            self._error(job, e, stage="fallback")
            return "failed"
        if not ok:
            return "capped" if capped else "failed"
        return "fallback"

    # =============================================================== jobs ====

    # -- story ---------------------------------------------------------------
    def _recent_meta(self, kind: str, key: str, n: int) -> list[str]:
        out: list[str] = []
        for it in self.store.content.list(kind, limit=n * 2):
            v = it.meta.get(key)
            if isinstance(v, str) and v and v not in out:
                out.append(v)
            if len(out) >= n:
                break
        return out

    def parse_story(self, data: dict[str, Any]) -> Story:
        raw = data.get("chunks")
        if not isinstance(raw, list):
            raise GenerationError("story: no chunks")
        chunks = rechunk([str(c) for c in raw])
        if not chunks:
            raise GenerationError("story: empty")
        story = Story(_clean(data.get("title"), 80) or "Zozo's Story",
                      _clean(data.get("theme"), 120), _clean(data.get("value"), 60), chunks)
        lo, hi = self.config.story_words
        n = _words(story.text)
        if not lo <= n <= hi:
            raise GenerationError(f"story: {n} words, wanted {lo}-{hi}")
        if "zozo" not in story.text.lower():
            raise GenerationError("story: Zozo is not in it")
        return story

    def job_story(self) -> str:
        if not self.store.content.needs_generation("story"):
            return "skipped"
        recent = self._recent_meta("story", "title", self.config.recent_n)
        recent_values = self._recent_meta("story", "value", 4)
        values = [v for v in P.VALUES if v not in recent_values] or list(P.VALUES)
        value = self.rng.choice(values)

        def attempt(n: int) -> None:
            data = self._claude("story", P.GENERATION_SYSTEM, P.story_prompt(recent, value),
                                P.STORY_SCHEMA, effort=self.config.gen_effort)
            story = self.parse_story(data)
            meta = {"title": story.title, "theme": story.theme, "value": story.value,
                    "text": story.text, "source": "generated", "attempt": n}
            v = self.review_text("story", f"Title: {story.title}\n\n" + "\n\n".join(story.chunks))
            if not v.passed:
                self._reject("story", n, "text", v.reasons, {**meta, "chunks_text": story.chunks})
                recent.append(story.title)
                raise SafetyRejected("text", v.reasons)
            d = self._item_dir("story")
            try:
                media = {f"chunk_{i}": self._tts(f"{d}/chunk_{i}.wav", c, "story_tts")
                         for i, c in enumerate(story.chunks)}
                meta["chunks"] = list(media)
                meta["chunks_text"] = story.chunks
                self._add_item("story", media, meta)
            except BaseException:
                self._remove_dir(d)
                raise

        return self._gated("story", attempt, self._fallback_story)

    # -- letter --------------------------------------------------------------
    def choose_letter(self) -> str:
        letters = [c for c in self.config.letters.upper() if c.isalpha()] or list(eg.LETTERS)
        recent = [L.upper() for L in self._recent_meta("letter", "letter", self.config.recent_letters)]
        start = 0
        if recent and recent[0] in letters:
            start = letters.index(recent[0]) + 1
        for i in range(len(letters)):
            L = letters[(start + i) % len(letters)]
            if L not in recent:
                return L
        return letters[start % len(letters)]

    @staticmethod
    def min_words(letter: str) -> int:
        return MIN_WORDS.get(letter.upper(), DEFAULT_MIN_WORDS)

    @staticmethod
    def parse_words(letter: str, data: dict[str, Any], avoid: list[str] = (),
                    min_words: int | None = None) -> list[str]:
        need = Pipeline.min_words(letter) if min_words is None else min_words
        out: list[str] = []
        for w in data.get("words") or []:
            w = _clean(w, 40).lower().strip(".!,")
            if not w or not w.startswith(letter.lower()) or not _WORD_OK.match(w):
                continue
            if w in out or w in avoid:
                continue
            out.append(w)
        if len(out) < need:
            raise GenerationError(f"letter {letter}: only {len(out)} usable words")
        return out[:5]

    def _letter_audio(self, d: str, letter: str, words: list[str]) -> dict[str, str]:
        media = {"intro": self._tts(f"{d}/intro.wav", eg.letter_intro(letter), "letter_tts")}
        for i, w in enumerate(words):
            media[f"word_{i}"] = self._tts(f"{d}/word_{i}.wav", eg.word_prompt_text(letter, w),
                                           "letter_tts")
        return media

    def _drawing(self, kind: str, subject: str, rel: str, n: int, meta: dict,
                 item_kind: str | None = None) -> tuple[str | None, bool]:
        """Generate + review one line drawing (up to 2 tries). Returns
        (rel or None, whether any image was rejected by the reviewer).
        `kind` names the spend purpose; rejections are stored as `item_kind`.
        A non-retryable ReviewUnavailable propagates."""
        rejected = False
        for t in (1, 2):
            try:
                data = self._image(P.image_prompt(subject), f"{kind}_image")
            except SpendCapReached:
                raise
            except Exception as e:
                self._error(kind, e, stage="image", attempt=n)
                if getattr(e, "retryable", True) is False:
                    break
                continue
            try:
                v = self.review_image(kind, data, subject)
            except ReviewUnavailable as e:
                if not e.retryable:
                    raise
                self._error(kind, e, stage="image_review", attempt=n,
                            reason="review_unavailable")
                continue
            if v.passed:
                self._checkpoint()
                self.store.write_media(rel, data)
                return rel, rejected
            rejected = True
            self._reject(item_kind or kind, n, "image", v.reasons,
                         {**meta, "image_subject": subject, "image_try": t})
        return None, rejected

    def job_letter(self) -> str:
        if not self.store.content.needs_generation("letter"):
            return "skipped"
        letter = self.choose_letter()
        need = self.min_words(letter)
        avoid: list[str] = []

        def attempt(n: int) -> None:
            data = self._claude("letter_words", P.GENERATION_SYSTEM,
                                P.words_prompt(letter, avoid, need), P.WORDS_SCHEMA,
                                effort=self.config.gen_effort, max_tokens=8000)
            words = self.parse_words(letter, data, avoid, need)
            meta = {"letter": letter, "words": words, "source": "generated", "attempt": n}
            v = self.review_text("letter", f"Letter {letter}. Words: {', '.join(words)}")
            if not v.passed:
                self._reject("letter", n, "text", v.reasons, meta)
                avoid.extend(words)
                raise SafetyRejected("text", v.reasons)
            d = self._item_dir("letter")
            try:
                media = self._letter_audio(d, letter, words)
                # The drawing is optional: a rejected or failed image only drops it.
                drawing, _ = self._drawing("letter", f"a {words[0]}", f"{d}/drawing.png", n,
                                           meta)
                if drawing:
                    media["drawing"] = drawing
                self._add_item("letter", media, meta)
            except BaseException:
                self._remove_dir(d)
                raise

        return self._gated("letter", attempt, lambda: self._fallback_letter(letter))

    # -- weather -------------------------------------------------------------
    def job_weather(self, *, force: bool = False) -> str:
        day = self.store.weather_day(self.clock())
        if not force and not self.store.content.needs_generation("weather", day):
            return "skipped"
        loc = self.store.settings.location
        if loc is None:
            self._error("weather", RuntimeError("no location set"), reason="no_location")
            return "failed"
        try:
            # Only coordinates go to Open-Meteo; Claude never sees the location.
            f = self.clients.weather.forecast(loc.lat, loc.lon, day)
        except Exception as e:
            self._error("weather", e, stage="forecast")
            return "failed"
        base = {"icon": f.icon, "hi_c": round(f.hi_c, 1), "lo_c": round(f.lo_c, 1),
                "code": f.code}

        def attempt(n: int) -> None:
            data = self._claude(
                "weather", P.GENERATION_SYSTEM,
                P.weather_prompt(f.condition, f.hi_c, f.lo_c, f.precip_prob, f.icon == "wind"),
                P.WEATHER_SCHEMA, effort=self.config.gen_effort, max_tokens=4000)
            summary, sentence = _clean(data.get("summary"), 60), _clean(data.get("sentence"), 200)
            if not summary or not sentence or _words(sentence) > 30 or _words(summary) > 6:
                raise GenerationError("weather: unusable wording")
            meta = {**base, "summary": summary, "sentence": sentence, "source": "generated",
                    "attempt": n}
            v = self.review_text("weather sentence", f"Card: {summary}\nSpoken: {sentence}")
            if not v.passed:
                self._reject("weather", n, "text", v.reasons, meta, for_date=day)
                raise SafetyRejected("text", v.reasons)
            self._store_weather(day, meta)

        def fallback() -> bool:
            # Template wording; its audio was pre-rendered by bootstrap, so
            # this needs no paid call (works at the spend cap or with TTS down).
            summary, sentence = template_weather(f)
            meta = {**base, "summary": summary, "sentence": sentence, "source": "template"}
            cached = {"audio": self.eg_weather_rel(f.icon, f.hi_c)}
            if self._ready(cached):
                self._copy_evergreen("weather", cached, {**meta, "evergreen_audio": True},
                                     for_date=day, source="template")
                return True
            self._store_weather(day, meta)
            return True

        return self._gated("weather", attempt, fallback)

    def _store_weather(self, day: date, meta: dict[str, Any]) -> None:
        d = self._item_dir("weather")
        try:
            media = {"audio": self._tts(f"{d}/audio.wav", meta["sentence"], "weather_tts")}
            self._add_item("weather", media, meta, for_date=day)
        except BaseException:
            self._remove_dir(d)
            raise

    # -- coloring ------------------------------------------------------------
    def job_coloring(self) -> str:
        if not self.store.content.needs_generation("coloring"):
            return "skipped"
        recent = self._recent_meta("coloring", "title", self.config.recent_n)

        def attempt(n: int) -> None:
            data = self._claude("coloring_theme", P.GENERATION_SYSTEM, P.coloring_prompt(recent),
                                P.COLORING_SCHEMA, effort=self.config.gen_effort, max_tokens=8000)
            title, subject = _clean(data.get("title"), 40), _clean(data.get("subject"), 200)
            intro = _clean(data.get("intro"), 200)
            if not title or not subject or not intro or _words(intro) > 25:
                raise GenerationError("coloring: unusable theme")
            meta = {"title": title, "subject": subject, "intro_text": intro,
                    "source": "generated", "attempt": n}
            v = self.review_text("coloring page theme",
                                 f"Title: {title}\nPicture: {subject}\nSpoken: {intro}")
            if not v.passed:
                self._reject("coloring", n, "text", v.reasons, meta)
                recent.append(title)
                raise SafetyRejected("text", v.reasons)
            d = self._item_dir("coloring")
            try:
                art, rejected = self._drawing("coloring", subject, f"{d}/art.png", n, meta)
                if art is None:
                    if rejected:  # already stored and logged as safety_failed
                        raise SafetyRejected("image", ["no image passed the review"])
                    raise GenerationError("coloring: no image could be made and reviewed")
                media = {"art": art,
                         "intro": self._tts(f"{d}/intro.wav", intro, "coloring_tts")}
                self._add_item("coloring", media, meta)
            except BaseException:
                self._remove_dir(d)
                raise

        return self._gated("coloring", attempt, self._fallback_coloring)

    # ========================================================= evergreen ====

    @staticmethod
    def eg_story_media(s: eg.EvergreenStory) -> dict[str, str]:
        return {f"chunk_{i}": f"evergreen/story/{s.slug}/chunk_{i}.wav"
                for i in range(len(s.chunks))}

    @staticmethod
    def eg_coloring_media(c: eg.EvergreenColoring) -> dict[str, str]:
        return {"art": f"evergreen/coloring/{c.slug}/art.png",
                "intro": f"evergreen/coloring/{c.slug}/intro.wav"}

    @staticmethod
    def eg_weather_rel(icon: str, hi_c: float) -> str:
        icon = icon if icon in TEMPLATE_ICONS else "cloudy"
        return f"evergreen/weather/{icon}/{feel_slug(hi_c)}.wav"

    @staticmethod
    def eg_letter_media(letter: str) -> dict[str, str]:
        words = eg.LETTER_WORDS.get(letter.upper(), ())
        m = {"intro": f"evergreen/letter/{letter.upper()}/intro.wav"}
        for i in range(len(words)):
            m[f"word_{i}"] = f"evergreen/letter/{letter.upper()}/word_{i}.wav"
        return m

    def _ready(self, media: dict[str, str]) -> bool:
        return bool(media) and all(self.store.media_exists(r) for r in media.values())

    def _copy_evergreen(self, kind: str, media: dict[str, str], meta: dict[str, Any], *,
                        for_date: date | None = None, source: str = "evergreen"):
        """Serve a copy (so purging the served item never deletes the master).
        Approval follows the store default: pending during the first month
        (SPEC 7.1: every item needs parent approval then)."""
        self._checkpoint()
        d = self._item_dir(kind)
        try:
            out = {}
            for k, rel in media.items():
                ext = Path(rel).suffix
                out[k] = self.store.import_media(self.store.media_path(rel), f"{d}/{k}{ext}")
            return self._add_item(kind, out, {**meta, "source": source}, for_date=for_date)
        except BaseException:
            self._remove_dir(d)
            raise

    def _pick_evergreen(self, kind: str, slugs: list[str]) -> list[str]:
        """Least recently served evergreen slugs first."""
        recent = self._recent_meta(kind, "evergreen_slug", 50)
        never = [s for s in slugs if s not in recent]
        self.rng.shuffle(never)
        used = sorted((s for s in slugs if s in recent), key=lambda s: -recent.index(s))
        return never + used

    def _fallback_story(self) -> bool:
        by_slug = {s.slug: s for s in eg.STORIES}
        for slug in self._pick_evergreen("story", list(by_slug)):
            s = by_slug[slug]
            media = self.eg_story_media(s)
            if self._ready(media):
                self._copy_evergreen("story", media, {
                    "title": s.title, "value": s.value, "text": s.text,
                    "chunks": list(media), "chunks_text": list(s.chunks), "evergreen_slug": slug})
                return True
        self._error("story", RuntimeError("no evergreen story ready (run bootstrap)"),
                    stage="fallback")
        return False

    def _fallback_coloring(self) -> bool:
        by_slug = {c.slug: c for c in eg.COLORING}
        for slug in self._pick_evergreen("coloring", list(by_slug)):
            c = by_slug[slug]
            media = self.eg_coloring_media(c)
            if self._ready(media):
                self._copy_evergreen("coloring", media, {
                    "title": c.title, "subject": c.subject, "intro_text": c.intro,
                    "evergreen_slug": slug})
                return True
        self._error("coloring", RuntimeError("no evergreen coloring page ready (run bootstrap)"),
                    stage="fallback")
        return False

    def _fallback_letter(self, letter: str) -> bool:
        order = [letter] + [L for L in eg.LETTERS if L != letter]
        for L in order:
            media = self.eg_letter_media(L)
            if self._ready(media):
                self._copy_evergreen("letter", media, {
                    "letter": L, "words": list(eg.LETTER_WORDS[L]), "evergreen_slug": L})
                return True
        self._error("letter", RuntimeError("no evergreen letter ready (run bootstrap)"),
                    stage="fallback")
        return False

    # ========================================================= bootstrap ====

    def _pending_phrases(self, key: str) -> list:
        return [it for it in self.store.content.list(
                    "phrase", approval="pending", include_withheld=False,
                    include_superseded=False, limit=1000)
                if it.meta.get("key") == key]

    def bootstrap_phrases(self, *, force: bool = False) -> dict[str, str]:
        """TTS the fixed phrases. Like every item they follow the first-month
        approval gate: a new phrase is pending until a parent approves it, and
        a replaced phrase stays in use until its replacement is servable."""
        out: dict[str, str] = {}
        for key, text in eg.PHRASE_TEXTS.items():
            try:
                cur = self.store.content.phrase(key)
                pending = self._pending_phrases(key)
                have = (cur is not None and "audio" in cur.media
                        and self.store.media_exists(cur.media["audio"])) or any(
                    "audio" in p.media and self.store.media_exists(p.media["audio"])
                    for p in pending)
                if not force and have:
                    out[key] = "skipped"
                    continue
                rel = f"phrases/{key}-{uuid.uuid4().hex[:8]}.wav"
                try:
                    self._tts(rel, text, "phrase_tts")
                    self._checkpoint()
                    # Fixed, hand-written text: passed; approval per store default.
                    item = self.store.content.add("phrase", media={"audio": rel},
                                                  meta={"key": key, "text": text},
                                                  safety="passed")
                except BaseException:
                    with contextlib.suppress(Exception):
                        self.store.media_path(rel).unlink(missing_ok=True)
                    raise
                for old in pending:  # unapproved older takes are replaced outright
                    with contextlib.suppress(Exception):
                        self.store.content.supersede(old.id, by=item.id)
                if cur is not None and item.servable:
                    with contextlib.suppress(Exception):
                        self.store.content.supersede(cur.id, by=item.id)
                out[key] = "generated"
            except SpendCapReached:
                out[key] = "capped"
            except Exception as e:
                self._error("phrases", e, key=key)
                out[key] = "failed"
        return out

    def _ensure_tts(self, rel: str, text: str, purpose: str) -> None:
        if not self.store.media_exists(rel):
            self._tts(rel, text, purpose)

    def bootstrap_evergreen(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for s in eg.STORIES:
            try:
                for (k, rel), text in zip(self.eg_story_media(s).items(), s.chunks):
                    self._ensure_tts(rel, text, "evergreen_tts")
                out[f"story:{s.slug}"] = "ready"
            except SpendCapReached:
                out[f"story:{s.slug}"] = "capped"
            except Exception as e:
                self._error("evergreen", e, item=f"story:{s.slug}")
                out[f"story:{s.slug}"] = "failed"
        for c in eg.COLORING:
            key = f"coloring:{c.slug}"
            try:
                media = self.eg_coloring_media(c)
                self._ensure_tts(media["intro"], c.intro, "evergreen_tts")
                if not self.store.media_exists(media["art"]):
                    if self._drawing("evergreen_coloring", c.subject, media["art"], 1,
                                     {"title": c.title, "evergreen_slug": c.slug,
                                      "source": "evergreen_bootstrap"},
                                     item_kind="coloring")[0] is None:
                        out[key] = "failed"
                        continue
                out[key] = "ready"
            except SpendCapReached:
                out[key] = "capped"
            except Exception as e:
                self._error("evergreen", e, item=key)
                out[key] = "failed"
        for L, words in eg.LETTER_WORDS.items():
            key = f"letter:{L}"
            try:
                media = self.eg_letter_media(L)
                self._ensure_tts(media["intro"], eg.letter_intro(L), "evergreen_tts")
                for i, w in enumerate(words):
                    self._ensure_tts(media[f"word_{i}"], eg.word_prompt_text(L, w), "evergreen_tts")
                out[key] = "ready"
            except SpendCapReached:
                out[key] = "capped"
            except Exception as e:
                self._error("evergreen", e, item=key)
                out[key] = "failed"
        for icon in TEMPLATE_ICONS:
            for slug, hi in FEEL_BANDS.items():
                key = f"weather:{icon}:{slug}"
                try:
                    self._ensure_tts(self.eg_weather_rel(icon, hi), template_text(icon, hi)[1],
                                     "evergreen_tts")
                    out[key] = "ready"
                except SpendCapReached:
                    out[key] = "capped"
                except Exception as e:
                    self._error("evergreen", e, item=key)
                    out[key] = "failed"
        return out

    def bootstrap(self, *, force_phrases: bool = False) -> dict[str, Any]:
        return {"phrases": self.bootstrap_phrases(force=force_phrases),
                "evergreen": self.bootstrap_evergreen()}

    # ============================================================ status ====

    def status(self) -> dict[str, Any]:
        c = self.store.content
        st: dict[str, Any] = {
            "month_to_date_usd": round(self.meter.month_to_date(), 4),
            "spend_cap_usd": self.meter.cap(),
            "needs_generation": {k: c.needs_generation(k) for k in NIGHTLY_JOBS},
            "current": {},
            "pending_approval": len(c.pending_approval()),
            "safety_failed": len(c.list(safety="failed", limit=1000)),
            "phrases_missing": [k for k in eg.PHRASE_TEXTS if c.phrase(k) is None],
            "evergreen_ready": {
                "story": sum(self._ready(self.eg_story_media(s)) for s in eg.STORIES),
                "coloring": sum(self._ready(self.eg_coloring_media(x)) for x in eg.COLORING),
                "letter": sum(self._ready(self.eg_letter_media(L)) for L in eg.LETTER_WORDS),
                "weather": sum(self.store.media_exists(self.eg_weather_rel(i, h))
                               for i in TEMPLATE_ICONS for h in FEEL_BANDS.values()),
            },
            "phrases_pending": sorted({it.meta.get("key") for it in c.list(
                "phrase", approval="pending", include_withheld=False,
                include_superseded=False, limit=1000)} - {None}),
        }
        for k in NIGHTLY_JOBS:
            it = c.current_for_today(k)
            st["current"][k] = None if it is None else {
                "id": it.id, "title": it.meta.get("title") or it.meta.get("letter")
                or it.meta.get("summary"), "source": it.meta.get("source")}
        return st

    # =========================================================== running ====

    def _live_key(self, name: str) -> tuple[str, str]:
        try:
            d = str(Path(self.store.data_dir).resolve())
        except Exception:
            d = str(self.store.data_dir)
        return d, name

    def _any_live(self) -> bool:
        d = self._live_key("")[0]
        with _LIVE_LOCK:
            return any(k[0] == d and t.is_alive() for k, t in _LIVE.items())

    def run_job(self, name: str, fn: Callable[[], Any], timeout_s: float | None = None) -> Any:
        """Run one job in a helper thread with a timeout; never raises.

        On timeout the job is told to cancel: it stops at its next checkpoint
        (before a paid call or a store write) and cleans up its partial media.
        Python threads cannot be killed, so one blocked inside an API call
        keeps running until that call returns; until it exits, this job is
        reported `busy` to later runs (in this process) instead of starting
        twice. `run_nightly` waits for such stragglers before releasing the
        run lock."""
        timeout_s = self.config.job_timeout_s if timeout_s is None else timeout_s
        key = self._live_key(name)
        with _LIVE_LOCK:
            prev = _LIVE.get(key)
            if prev is not None and prev.is_alive():
                busy = True
            else:
                busy = False
                _LIVE.pop(key, None)
        if busy:
            self._error(name, RuntimeError("a timed-out run of this job is still stopping"),
                        reason="busy")
            return "busy"
        box: dict[str, Any] = {}
        cancel = threading.Event()

        def target() -> None:
            try:
                box["result"] = fn()
            except SpendCapReached:
                box["result"] = "capped"
            except JobCancelled:
                box["result"] = "cancelled"
                log.info("content: %s stopped after its timeout", name)
            except BaseException as e:  # noqa: BLE001 - never crash the run
                box["error"] = e
            finally:
                self._cancel.pop(threading.current_thread(), None)

        t = threading.Thread(target=target, name=f"content-{name}", daemon=True)
        self._cancel[t] = cancel
        with _LIVE_LOCK:
            _LIVE[key] = t
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            cancel.set()
            self._stragglers.append(t)
            self._error(name, TimeoutError(f"job exceeded {timeout_s:.0f}s"), reason="timeout")
            return "timeout"
        with _LIVE_LOCK:
            if _LIVE.get(key) is t:
                del _LIVE[key]
        if "error" in box:
            self._error(name, box["error"])
            return "failed"
        return box.get("result")

    def wait_stragglers(self, grace_s: float | None = None) -> bool:
        """Join timed-out job threads for up to `grace_s` in total. True when
        all of them have exited."""
        grace_s = self.config.cancel_grace_s if grace_s is None else grace_s
        deadline = time.monotonic() + max(0.0, grace_s)
        for t in list(self._stragglers):
            t.join(max(0.0, deadline - time.monotonic()))
            if not t.is_alive():
                self._stragglers.remove(t)
        if self._stragglers:
            log.warning("content: %d timed-out job(s) still running: %s", len(self._stragglers),
                        ", ".join(t.name for t in self._stragglers))
            return False
        return True

    def sweep_orphans(self) -> dict[str, int]:
        """Remove `.tts-*` temp dirs and generated media dirs
        (media/content/<kind>/YYYY/MM/DD/<id>/) that no item references: what
        a killed or timed-out job left behind. Call only under the run lock.
        Skipped while a job thread of this store is still alive."""
        out = {"tts_tmp": 0, "media_dirs": 0}
        if self._any_live():
            return out
        try:
            for d in Path(self.store.data_dir).glob(".tts-*"):
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                    out["tts_tmp"] += 1
            root = Path(self.store.media_dir) / "content"
            if not root.is_dir():
                return out
            used: set[str] = set()
            for kind in ("weather", *GENERATED_KINDS):
                off = 0
                while True:
                    page = self.store.content.list(kind, limit=1000, offset=off)
                    for it in page:
                        for rel in it.media.values():
                            used.add(Path(rel).parent.as_posix())
                    if len(page) < 1000:
                        break
                    off += 1000
            for kind in ("weather", *GENERATED_KINDS):
                for d in (root / kind).glob("*/*/*/*"):
                    rel = d.relative_to(self.store.media_dir).as_posix()
                    if d.is_dir() and rel not in used:
                        shutil.rmtree(d, ignore_errors=True)
                        out["media_dirs"] += 1
        except Exception:
            log.exception("content: orphan sweep failed")
        if any(out.values()):
            log.info("content: swept orphans %s", out)
        return out

    def run_nightly(self, *, jobs: tuple[str, ...] = NIGHTLY_JOBS,
                    force_weather: bool = False) -> dict[str, Any]:
        table: dict[str, Callable[[], Any]] = {
            "weather": lambda: self.job_weather(force=force_weather),
            "story": self.job_story,
            "letter": self.job_letter,
            "coloring": self.job_coloring,
        }
        results: dict[str, Any] = {}
        with _run_lock(self.store.data_dir) as got:
            if not got:
                log.warning("content: another content run is in progress")
                return {"locked": True}
            self.sweep_orphans()
            try:
                for name in jobs:
                    results[name] = self.run_job(name, table[name])
            finally:
                # Hold the lock while timed-out jobs unwind and clean up.
                self.wait_stragglers()
        self._event(CONTENT_RUN, {"results": results, "usd": round(self.meter.run_usd, 4)})
        return results


@contextlib.contextmanager
def _run_lock(data_dir: Path) -> Iterator[bool]:
    """One content run at a time (cron overlap, manual run). No-op without fcntl."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield True
        return
    path = Path(data_dir) / ".content.lock"
    f = open(path, "a+")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


# --------------------------------------------------------- module API ----


def run_nightly(store, clients: Clients, clock: Callable[[], datetime] | None = None, *,
                config: ContentConfig | None = None, **kw: Any) -> dict[str, Any]:
    return Pipeline(store, clients, clock, config).run_nightly(**kw)


def run_bootstrap(store, clients: Clients, clock: Callable[[], datetime] | None = None, *,
                  config: ContentConfig | None = None, force_phrases: bool = False):
    p = Pipeline(store, clients, clock, config)
    with _run_lock(store.data_dir) as got:
        if not got:
            return {"locked": True}
        p.sweep_orphans()
        return p.bootstrap(force_phrases=force_phrases)


def run_weather(store, clients: Clients, clock: Callable[[], datetime] | None = None, *,
                config: ContentConfig | None = None, force: bool = False):
    return Pipeline(store, clients, clock, config).run_nightly(jobs=("weather",),
                                                               force_weather=force)


def status(store, clients: Clients | None = None, clock=None) -> dict[str, Any]:
    dummy = clients or Clients(claude=None, media=None, weather=None)  # type: ignore[arg-type]
    return Pipeline(store, dummy, clock).status()


__all__ = [
    "Clients", "ContentConfig", "GenerationError", "JobCancelled", "Pipeline",
    "ReviewUnavailable", "SafetyRejected", "Story",
    "Verdict", "rechunk", "run_bootstrap", "run_nightly", "run_weather", "status",
    "SAFETY_FAILED", "CONTENT_ERROR", "CONTENT_RUN",
]
