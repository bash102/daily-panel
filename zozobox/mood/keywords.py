"""Cheap local pre-match for toddler mood answers (no network, no cost).

Only an *unambiguous* short answer is matched here: exactly one mood family
is named, nothing is negated, and the answer is short. Anything else
("not happy", "happy and sad", a long ramble) goes to Claude.
"""

from __future__ import annotations

import re

# Word -> mood. Kept deliberately strict: words a toddler uses for exactly
# one of the four moods. Ambiguous ones ("good", "tired", "hungry") are left
# to the model.
KEYWORDS: dict[str, str] = {
    # happy
    "happy": "happy", "happi": "happy", "happee": "happy", "hap": "happy",
    "happier": "happy", "glad": "happy", "yay": "happy", "yippee": "happy",
    # mad
    "mad": "mad", "madder": "mad", "angry": "mad", "angy": "mad",
    "grumpy": "mad", "cross": "mad", "grr": "mad", "grrr": "mad",
    # sad
    "sad": "sad", "saddy": "sad", "sadder": "sad", "sadded": "sad",
    "crying": "sad", "cry": "sad", "boohoo": "sad",
    # neutral
    "okay": "neutral", "ok": "neutral", "okey": "neutral", "kay": "neutral",
    "fine": "neutral", "alright": "neutral", "meh": "neutral", "soso": "neutral",
}

NEGATIONS = frozenset({
    "not", "no", "nah", "nope", "dont", "don't", "isnt", "isn't", "aint",
    "ain't", "never", "nobody", "notta", "un",
})

# Longer answers are more likely to contain a story ("I was sad but now I'm
# happy"): send them to the model.
MAX_WORDS = 6

_WORD = re.compile(r"[a-z']+")


def words(text: str) -> list[str]:
    t = text.lower().replace("so-so", "soso").replace("so so", "soso")
    t = t.replace("’", "'")
    return [w.strip("'") for w in _WORD.findall(t) if w.strip("'")]


def has_speech(text: str | None) -> bool:
    """True when the transcript has at least one word (not just punctuation,
    '...' or whitespace)."""
    return bool(text) and bool(words(text))


def keyword_match(text: str | None) -> str | None:
    """The mood when the answer is unambiguous, else None."""
    if not text:
        return None
    ws = words(text)
    if not ws or len(ws) > MAX_WORDS:
        return None
    if any(w in NEGATIONS for w in ws):
        return None
    found = {KEYWORDS[w] for w in ws if w in KEYWORDS}
    if len(found) != 1:
        return None
    return found.pop()
