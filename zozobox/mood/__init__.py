"""M7 mood recognition (SPEC 2.1): OpenAI speech-to-text + Claude label,
a local keyword pre-match, spend logging/cap, and offline reprocessing.

    from zozobox.mood import OpenAIClaudeMoodClassifier, reprocess_pending
    clf = OpenAIClaudeMoodClassifier.from_store(store)   # engine mood_classifier
    reprocess_pending(store, clf)                        # or: python -m zozobox.mood reprocess
"""

from .classifier import MOOD_CONFIG_ERROR, FailedResult, OpenAIClaudeMoodClassifier
from .errors import ServiceUnavailable
from .keywords import has_speech, keyword_match
from .labeler import ClaudeMoodLabeler, FakeLabeler, Label, MoodLabeler
from .reprocess import (
    MOOD_REPROCESS_FAILED, ReprocessReport, reprocess_pending, unmatched_pending,
)
from .spend import (
    SPEND, SPEND_CAP_REACHED, SpendItem, SpendLedger, anthropic_cost, openai_stt_cost,
)
from .stt import FakeTranscriber, OpenAITranscriber, Transcriber, Transcript

__all__ = [
    "OpenAIClaudeMoodClassifier", "FailedResult", "MOOD_CONFIG_ERROR",
    "MOOD_REPROCESS_FAILED", "ServiceUnavailable", "has_speech", "keyword_match",
    "ClaudeMoodLabeler", "FakeLabeler", "Label", "MoodLabeler",
    "ReprocessReport", "reprocess_pending", "unmatched_pending",
    "SPEND", "SPEND_CAP_REACHED", "SpendItem", "SpendLedger", "anthropic_cost",
    "openai_stt_cost", "FakeTranscriber", "OpenAITranscriber", "Transcriber", "Transcript",
]
