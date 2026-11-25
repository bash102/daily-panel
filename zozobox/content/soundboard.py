"""Soundboard starter pack (SPEC 2.3), made by the bootstrap step.

We can't ship downloaded CC0 clips in the repo, so `bootstrap` TTS-generates a
small pack of silly clips in the box voice. The texts below are fixed and were
reviewed by hand against SPEC 2.4 (age 2-3, gentle, never scary), so they
skip the AI safety review (safety=passed, like the fixed phrases).

They are still TTS output, so they follow the same first-month approval rule
as every other generated item (SPEC 7.1, PLAN "Notes from M6-M9"): each clip
is a content item of kind `soundboard` (media `soundboard/<slug>-<id>.wav`
in the store's media dir, meta {"key": slug, "text": ...}), pending until a
parent approves it on the dashboard. The engine's StoreSoundboard plays only
servable ones (approved, or auto after the first month).

Owner-added recordings (`zozobox-import-clip`, docs/DEPLOY.md "Soundboard
clips") go to the soundboard directory instead and need no approval: the
parent picked and listened to them.

This is a stand-in for SPEC 2.3's bundled CC0 starter pack, which can't be
redistributed from this repo; CC0 clips imported by the owner complete it.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from typing import Any

from ..store import Kind
from .spend import SpendCapReached

log = logging.getLogger(__name__)

PURPOSE = "soundboard_tts"
MEDIA_PREFIX = "soundboard"
# Legacy location of TTS starter clips (before they were gated); the engine
# ignores this subdirectory of the soundboard dir.
STARTER_SUBDIR = "starter"

# slug -> text. Short (2-6 s spoken). Edit here, then re-run bootstrap with
# --force-soundboard to regenerate them (each new take waits for approval in
# the first month; the approved take stays in use until then).
STARTER_CLIPS: dict[str, str] = {
    "cow": "Moo! said the cow. Moooooo!",
    "duck": "Quack, quack! said the little yellow duck.",
    "sheep": "Baa, baa! said the fluffy white sheep.",
    "puppy": "Woof, woof! said the happy puppy. Wag, wag, wag!",
    "kitty": "Meow! said the kitty. Meeeow! Purr, purr.",
    "pig": "Oink, oink! said the pink pig. Snort, snort!",
    "horse": "Neigh! said the horse. Clip clop, clip clop, clip clop!",
    "owl": "Hoo, hoo! said the sleepy owl.",
    "frog": "Ribbit, ribbit! said the jumpy green frog. Boing!",
    "lion": "Roar! said the friendly lion. That was a teeny, tiny roar!",
    "robot": "Beep boop! I am a silly robot. Beep, beep, boop!",
    "giggle": "Hee hee hee! Ha ha ha! Oh, that tickles!",
    "wibble": "Wibble wobble, jiggle jelly, wiggle wobble, wibbly jelly!",
    "twinkle": "Twinkle, twinkle, little star, how I wonder what you are!",
    "row_boat": "Row, row, row your boat, gently down the stream. "
                "Merrily, merrily, merrily, merrily, life is but a dream!",
    "hickory": "Hickory, dickory, dock! The mouse ran up the clock!",
}



def _items(store, key: str) -> list:
    return [it for it in store.content.list(Kind.SOUNDBOARD, include_withheld=False,
                                            include_superseded=False, limit=1000)
            if it.meta.get("key") == key]


def bootstrap_soundboard(pipeline: Any, *, force: bool = False) -> dict[str, str]:
    """TTS every starter clip that has no usable take yet (all of them with
    `force`). A usable take is a servable item, or one still waiting for
    approval, whose audio file exists. Returns slug -> generated | skipped |
    capped | failed. Spend is logged like any other TTS call (purpose
    `soundboard_tts`)."""
    store = pipeline.store
    out: dict[str, str] = {}
    for slug, text in STARTER_CLIPS.items():
        try:
            items = _items(store, slug)
            usable = [it for it in items if it.approval.value in ("pending", "approved", "auto")
                      and "audio" in it.media and store.media_exists(it.media["audio"])]
            if usable and not force:
                out[slug] = "skipped"
                continue
            rel = f"{MEDIA_PREFIX}/{slug}-{uuid.uuid4().hex[:8]}.wav"
            try:
                pipeline._tts(rel, text, PURPOSE)
                # Fixed, hand-written text: passed; approval per store default
                # (pending in the first month, auto afterwards).
                item = store.content.add(Kind.SOUNDBOARD, media={"audio": rel},
                                         meta={"key": slug, "text": text, "title": slug},
                                         safety="passed")
            except BaseException:
                with contextlib.suppress(Exception):
                    store.media_path(rel).unlink(missing_ok=True)
                raise
            for old in items:
                # An unapproved older take is replaced outright; an approved one
                # stays in use until the new take is servable itself.
                if old.approval.value == "pending" or item.servable:
                    with contextlib.suppress(Exception):
                        store.content.supersede(old.id, by=item.id)
            out[slug] = "generated"
        except SpendCapReached:
            out[slug] = "capped"
        except Exception as e:
            log.warning("soundboard: %s failed: %s", slug, e)
            pipeline._error("soundboard", e, clip=slug)
            out[slug] = "failed"
    return out


__all__ = ["MEDIA_PREFIX", "PURPOSE", "STARTER_CLIPS", "STARTER_SUBDIR", "bootstrap_soundboard"]
