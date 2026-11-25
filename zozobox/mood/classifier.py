"""M7 mood classifier (SPEC 2.1, docs/ENGINE.md "Mood classifier contract").

    clf = OpenAIClaudeMoodClassifier.from_store(store)
    clf.classify(path) -> MoodResult      # engine's MoodClassifier protocol

Pipeline: spend-cap check -> OpenAI speech-to-text -> empty transcript =>
unknown -> local keyword pre-match (no Claude call when unambiguous) ->
deadline check -> spend-cap check -> Claude label. Network/timeout/auth/API
errors and a reached spend cap => MoodResult(offline=True); when the cause is
a cloud error the result is a `FailedResult` (a MoodResult subclass) carrying
provider/kind/per_take so the reprocessor can tell a poisoned take from an
outage. Config/auth errors (missing package or key, rejected key) are also
logged as an `error` event {kind: "mood_config_error", provider, error_kind}
for the dashboard, at most once per provider+kind per hour. Never raises.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..engine.sources import CLASSIFIER_LABELS, MoodResult
from ..store.events import EventType
from .errors import ServiceUnavailable
from .keywords import has_speech, keyword_match
from .labeler import ClaudeMoodLabeler, MoodLabeler
from .spend import SpendLedger
from .stt import OpenAITranscriber, Transcriber

log = logging.getLogger(__name__)

MOOD_CONFIG_ERROR = "mood_config_error"
CONFIG_ERROR_EVERY_S = 3600.0
# Below this much remaining budget after speech-to-text, Claude isn't called
# (the engine would give up before the answer arrives).
MIN_CLAUDE_BUDGET_S = 3.0


@dataclass(frozen=True)
class FailedResult(MoodResult):
    """offline=True result caused by a cloud error (see errors.PER_TAKE_KINDS)."""

    offline: bool = True
    provider: str = ""
    kind: str = ""
    per_take: bool = False

PURPOSE_STT = "mood_stt"
PURPOSE_LABEL = "mood_classify"


class OpenAIClaudeMoodClassifier:
    def __init__(
        self,
        transcriber: Transcriber,
        labeler: MoodLabeler,
        *,
        ledger: SpendLedger | None = None,
        use_keywords: bool = True,
        purpose_suffix: str = "",
        store=None,
        deadline_s: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        """ledger=None disables spend logging and the cap (tests/bench only).
        store: where config errors are logged (default: ledger.store).
        deadline_s: overall budget for classify(); after speech-to-text the
        remaining budget becomes Claude's request timeout, and Claude is
        skipped (offline, transcript kept) when < MIN_CLAUDE_BUDGET_S is left.
        None = no overall deadline (offline reprocessing)."""
        self.transcriber = transcriber
        self.labeler = labeler
        self.ledger = ledger
        self.use_keywords = use_keywords
        self.purpose_suffix = purpose_suffix
        self.store = store if store is not None else getattr(ledger, "store", None)
        self.deadline_s = deadline_s
        self._monotonic = monotonic
        self._config_logged: dict[tuple[str, str], float] = {}

    @classmethod
    def from_store(
        cls,
        store,
        *,
        stt_timeout_s: float = 8.0,
        claude_timeout_s: float = 10.0,
        purpose_suffix: str = "",
        deadline_s: float | None = 18.0,
    ) -> "OpenAIClaudeMoodClassifier":
        """Production wiring. Keys come from OPENAI_API_KEY / ANTHROPIC_API_KEY;
        models from ZOZOBOX_STT_MODEL / ZOZOBOX_CLAUDE_MODEL (optional).

        No SDK retries (max_retries=0). The SDK timeouts apply per phase
        (connect/read/write), not to the whole call, so they alone can't bound
        a call; deadline_s (default 18 s, under the engine's
        classify_timeout_s=20) bounds the Claude step by the time left after
        speech-to-text. A speech-to-text call that itself overruns can still
        outlast the engine: the engine then logs mood_pending without a
        transcript and the reprocessor transcribes that take again (speech-to-
        text paid twice for it; a fraction of a cent per take). Pass
        deadline_s=None for offline reprocessing."""
        return cls(
            OpenAITranscriber(timeout_s=stt_timeout_s, max_retries=0),
            ClaudeMoodLabeler(timeout_s=claude_timeout_s, max_retries=0),
            ledger=SpendLedger(store),
            purpose_suffix=purpose_suffix,
            store=store,
            deadline_s=deadline_s,
        )

    def _purpose(self, base: str) -> str:
        return base + self.purpose_suffix

    def _cap_reached(self, purpose: str) -> bool:
        return self.ledger is not None and self.ledger.cap_reached(self._purpose(purpose))

    def _record(self, spend, purpose: str) -> None:
        if self.ledger is not None and spend is not None:
            self.ledger.record(spend, self._purpose(purpose))

    def _failed(self, e: ServiceUnavailable, transcript: str = "") -> FailedResult:
        """Log, record billed spend of a failed call, surface config errors."""
        if e.spend is not None:
            self._record(e.spend, PURPOSE_LABEL if e.provider == "anthropic" else PURPOSE_STT)
        if e.config_error:
            self._report_config_error(e)
        return FailedResult("unknown", transcript=transcript, provider=e.provider,
                            kind=e.kind, per_take=e.per_take)

    def _report_config_error(self, e: ServiceUnavailable) -> None:
        if self.store is None:
            return
        key = (e.provider, e.kind)
        now = self._monotonic()
        last = self._config_logged.get(key)
        if last is not None and now - last < CONFIG_ERROR_EVERY_S:
            return
        self._config_logged[key] = now
        try:
            # e.detail is an exception class name / fixed text: never a key.
            self.store.events.append(EventType.ERROR, {
                "kind": MOOD_CONFIG_ERROR, "provider": e.provider,
                "error_kind": e.kind, "detail": e.detail[:200],
            })
        except Exception:
            log.exception("mood: could not log config error event")

    # -- MoodClassifier -------------------------------------------------------
    def classify(self, path: Path) -> MoodResult:
        try:
            start = self._monotonic()
            return self._classify(Path(path), start)
        except Exception:
            log.exception("mood: classify failed; treating as offline")
            return MoodResult("unknown", offline=True)

    def classify_transcript(self, transcript: str) -> MoodResult:
        """Label a transcript we already have (offline reprocessing reuses
        the transcript of a take whose labelling failed)."""
        try:
            return self._label(transcript or "", None)
        except Exception:
            log.exception("mood: classify_transcript failed; treating as offline")
            return MoodResult("unknown", transcript=transcript or "", offline=True)

    def _classify(self, path: Path, start: float) -> MoodResult:
        if not path.is_file():
            log.warning("mood: audio %s missing; unknown", path)
            return MoodResult("unknown")
        if self._cap_reached(PURPOSE_STT):
            return MoodResult("unknown", offline=True)
        try:
            tr = self.transcriber.transcribe(path)
        except ServiceUnavailable as e:
            log.warning("mood: speech-to-text unavailable (%s); offline", e)
            return self._failed(e)
        self._record(tr.spend, PURPOSE_STT)
        return self._label(tr.text or "", start)

    def _remaining(self, start: float | None) -> float | None:
        if self.deadline_s is None or start is None:
            return None
        return self.deadline_s - (self._monotonic() - start)

    def _label(self, transcript: str, start: float | None) -> MoodResult:
        text = transcript.strip()
        if not has_speech(text):
            return MoodResult("unknown", transcript=text)
        if self.use_keywords:
            hit = keyword_match(text)
            if hit is not None:
                return MoodResult(hit, transcript=text)
        remaining = self._remaining(start)
        if remaining is not None and remaining < MIN_CLAUDE_BUDGET_S:
            log.warning("mood: %.1f s left after speech-to-text; Claude skipped, pending",
                        remaining)
            return MoodResult("unknown", transcript=text, offline=True)
        if self._cap_reached(PURPOSE_LABEL):
            return MoodResult("unknown", transcript=text, offline=True)
        try:
            if remaining is None:
                res = self.labeler.label(text)
            else:
                res = self.labeler.label(text, timeout_s=remaining)
        except ServiceUnavailable as e:
            log.warning("mood: Claude unavailable (%s); offline", e)
            return self._failed(e, text)
        self._record(res.spend, PURPOSE_LABEL)
        label = res.label if res.label in CLASSIFIER_LABELS else "unknown"
        return MoodResult(label, transcript=text)
