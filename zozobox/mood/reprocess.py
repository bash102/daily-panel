"""Offline mood recognition (SPEC 2.1 / 7.4, docs/ENGINE.md M7 contract).

Finds `mood_pending` events with no `mood` event for the same media, runs the
classifier and records the result with source "offline" at the original
timestamp, so it lands on the right box day and in order. Idempotent: a take
that already has a mood event is never processed again. Stops early when the
spend cap is reached or the classifier is offline (no point hammering).

A failure that belongs to one take (OpenAI rejected its audio, the file can't
be read, Claude's answer was cut off; see errors.PER_TAKE_KINDS) doesn't stop
the run: an event `mood_reprocess_failed` {pending_id, media, provider, kind} is
logged for the take and the run moves on to the next one. After MAX_ATTEMPTS
such failures the take is recorded as "unknown" (reason e.g. "stt_rejected")
so it never blocks the queue. MAX_CONSECUTIVE_FAILURES per-take failures in a
row in one run look like a global problem and stop the run as "offline".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..engine.sources import CLASSIFIER_LABELS, MoodClassifier, MoodResult
from ..store.events import EventType
from .spend import SpendLedger

log = logging.getLogger(__name__)

PURPOSE = "mood_offline"
MOOD_REPROCESS_FAILED = "mood_reprocess_failed"
MAX_ATTEMPTS = 3
MAX_CONSECUTIVE_FAILURES = 3

GIVE_UP_REASONS = {
    ("openai", "bad_request"): "stt_rejected",
    ("openai", "unreadable"): "audio_unreadable",
    ("anthropic", "truncated"): "claude_truncated",
}


@dataclass
class ReprocessReport:
    pending: int = 0          # unmatched pending takes found
    recognized: int = 0       # recorded with a happy/mad/sad/neutral label
    unknown: int = 0          # recorded as unknown (incl. missing audio, gave up)
    failed: int = 0           # per-take failures this run (take skipped)
    gave_up: int = 0          # of `unknown`: takes given up after MAX_ATTEMPTS
    left_pending: int = 0     # not processed this run (incl. failed)
    stopped: str | None = None  # "spend_cap", "offline" or None
    recorded_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending": self.pending, "recognized": self.recognized,
            "unknown": self.unknown, "failed": self.failed, "gave_up": self.gave_up,
            "left_pending": self.left_pending,
            "stopped": self.stopped, "recorded_ids": list(self.recorded_ids),
        }


def unmatched_pending(store) -> list:
    """Oldest-first mood_pending events whose media has no mood event (one
    per media; a pending event without media can't be matched and is skipped)."""
    done = {e.media for e in store.events.query(EventType.MOOD, limit=None) if e.media}
    seen: set[str] = set()
    out = []
    for ev in store.events.query(EventType.MOOD_PENDING, limit=None):
        if not ev.media:
            log.warning("mood: pending event %s has no media; skipped", ev.id)
            continue
        if ev.media in done or ev.media in seen:
            continue
        seen.add(ev.media)
        out.append(ev)
    return out


def _has_mood(store, media: str) -> bool:
    return any(e.media == media for e in store.events.query(EventType.MOOD, limit=None))


def reprocess_pending(
    store,
    classifier: MoodClassifier,
    *,
    ledger: SpendLedger | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> ReprocessReport:
    """Recognize pending takes. `ledger` (default: SpendLedger(store)) gates
    each take on the spend cap. Never raises for a single bad take."""
    ledger = ledger if ledger is not None else SpendLedger(store)
    todo = unmatched_pending(store)
    rep = ReprocessReport(pending=len(todo))
    if dry_run:
        rep.left_pending = len(todo)
        return rep
    consecutive = 0
    for i, ev in enumerate(todo):
        if limit is not None and i >= limit:
            rep.left_pending += len(todo) - i
            break
        if ledger.cap_reached(PURPOSE):
            rep.stopped = "spend_cap"
            rep.left_pending += len(todo) - i
            break
        result = _classify_one(store, classifier, ev)
        if result is None:
            rep.stopped = "offline"
            rep.left_pending += len(todo) - i
            break
        if isinstance(result, _PerTakeFailure):
            label_reason = _note_failure(store, ev, result)
            if label_reason is None:  # will be retried on a later run
                rep.failed += 1
                rep.left_pending += 1
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES and i + 1 < len(todo):
                    log.warning("mood: %d takes failed in a row; stopping this run",
                                consecutive)
                    rep.stopped = "offline"
                    rep.left_pending += len(todo) - i - 1
                    break
                continue
            result = ("unknown", None, {"pending_id": ev.id, "reason": label_reason,
                                        **_attempt(ev)})
            rep.gave_up += 1
        else:
            consecutive = 0
        if _has_mood(store, ev.media):  # recorded meanwhile by someone else
            continue
        label, transcript, extra = result
        try:
            rec = store.events.record_mood(
                label,
                transcript=transcript,
                source="offline",
                audio=ev.media,
                ts=ev.ts,
                extra=extra,
            )
        except Exception:
            log.exception("mood: could not record offline mood for event %s", ev.id)
            rep.left_pending += 1
            continue
        rep.recorded_ids.append(rec.id)
        if label == "unknown":
            rep.unknown += 1
        else:
            rep.recognized += 1
    return rep


@dataclass(frozen=True)
class _PerTakeFailure:
    provider: str
    kind: str


def _attempt(ev) -> dict[str, Any]:
    return {"attempt": ev.payload["attempt"]} if "attempt" in ev.payload else {}


def _note_failure(store, ev, f: _PerTakeFailure) -> str | None:
    """Log one failed attempt for this take; return the give-up reason when
    it has now failed MAX_ATTEMPTS times, else None."""
    try:
        prior = sum(1 for e in store.events.query(MOOD_REPROCESS_FAILED, limit=None)
                    if e.payload.get("media") == ev.media)
    except Exception:
        log.exception("mood: could not count failures for event %s", ev.id)
        prior = 0
    attempts = prior + 1
    log.warning("mood: pending take %s failed (%s %s), attempt %d/%d",
                ev.id, f.provider, f.kind, attempts, MAX_ATTEMPTS)
    try:
        store.events.append(MOOD_REPROCESS_FAILED,
                            {"pending_id": ev.id, "media": ev.media,
                             "provider": f.provider, "kind": f.kind})
    except Exception:
        log.exception("mood: could not log failure for event %s", ev.id)
    if attempts < MAX_ATTEMPTS:
        return None
    return GIVE_UP_REASONS.get((f.provider, f.kind), f"{f.provider}_{f.kind}")


def _classify_one(store, classifier, ev):
    """(label, transcript, extra) to record, a _PerTakeFailure, or None when
    offline (global failure: stop the run)."""
    extra: dict[str, Any] = {"pending_id": ev.id, **_attempt(ev)}
    try:
        path = store.media_path(ev.media)
    except Exception:
        log.warning("mood: pending event %s has a bad media path", ev.id)
        return "unknown", None, {**extra, "reason": "audio_missing"}

    prior = (ev.payload.get("transcript") or "").strip()
    try:
        if prior and hasattr(classifier, "classify_transcript"):
            res: MoodResult = classifier.classify_transcript(prior)
        elif not path.is_file():
            log.warning("mood: audio for pending event %s is missing", ev.id)
            return "unknown", None, {**extra, "reason": "audio_missing"}
        else:
            res = classifier.classify(path)
    except Exception:
        log.exception("mood: classifier raised for event %s; treating as offline", ev.id)
        return None
    if res.offline:
        if getattr(res, "per_take", False):
            return _PerTakeFailure(getattr(res, "provider", ""), getattr(res, "kind", ""))
        return None
    label = res.label if res.label in CLASSIFIER_LABELS else "unknown"
    return label, res.transcript, extra
