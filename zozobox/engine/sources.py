"""Small collaborator protocols the engine needs from later milestones, with
simple real implementations and Fake twins for tests.

- `PhraseBook`: fixed TTS phrases by name (M6 generates and caches them).
- `MoodClassifier`: speech -> mood label (M7 implements the cloud version).
- `Soundboard`: the bundled clip pack.

Phrase names the engine uses are listed in `PHRASES` (see docs/ENGINE.md).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, Sequence

log = logging.getLogger(__name__)

AUDIO_EXTS: tuple[str, ...] = (".wav", ".opus", ".ogg", ".mp3", ".flac")

RECOGNIZED_MOODS: tuple[str, ...] = ("happy", "mad", "sad", "neutral")
CLASSIFIER_LABELS: tuple[str, ...] = RECOGNIZED_MOODS + ("unknown",)

# Every fixed phrase name the engine may ask for. Missing ones are skipped.
PHRASES: tuple[str, ...] = (
    "say_hi_grandma",
    "sleepy",
    "printer_sleepy",
    "no_story_yet",
    "print_not_ready",
    "ask_mood",
    "ask_mood_again",
    "you_feel_happy",
    "you_feel_mad",
    "you_feel_sad",
    "you_feel_neutral",
    "all_printed_today",
    "here_is_your_card",
    "praise_great_job",
    "praise_well_done",
    "praise_you_did_it",
)


# --------------------------------------------------------------- phrases ----


class PhraseBook(Protocol):
    def path_for(self, name: str) -> Path | None: ...


class DirPhraseBook:
    """Phrases as files named `<name>.<ext>` in one directory."""

    def __init__(self, directory: str | Path, exts: Sequence[str] = AUDIO_EXTS):
        self.directory = Path(directory)
        self.exts = tuple(exts)

    def path_for(self, name: str) -> Path | None:
        for ext in self.exts:
            p = self.directory / f"{name}{ext}"
            if p.is_file():
                return p
        return None


class StorePhraseBook:
    """Phrases from the content cache (kind='phrase', meta['key']=name,
    media['audio']), falling back to another PhraseBook (e.g. bundled files)."""

    def __init__(self, store, fallback: PhraseBook | None = None):
        self.store = store
        self.fallback = fallback

    def path_for(self, name: str) -> Path | None:
        try:
            item = self.store.content.phrase(name)
            if item is not None and "audio" in item.media:
                p = self.store.media_path(item.media["audio"])
                if p.is_file():
                    return p
        except Exception:
            log.exception("phrase lookup failed for %s", name)
        return self.fallback.path_for(name) if self.fallback is not None else None


class FakePhraseBook:
    """Creates a tiny placeholder file per phrase under `directory`.
    Names in `missing` (or not in `names`, when given) return None."""

    def __init__(self, directory: str | Path, *, names: Iterable[str] | None = None,
                 missing: Iterable[str] = ()):
        self.directory = Path(directory)
        self.names = set(names) if names is not None else None
        self.missing = set(missing)
        self.asked: list[str] = []

    def path_for(self, name: str) -> Path | None:
        self.asked.append(name)
        if name in self.missing or (self.names is not None and name not in self.names):
            return None
        p = self.directory / f"{name}.wav"
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"RIFF")
        return p


# ------------------------------------------------------------------ mood ----


@dataclass(frozen=True)
class MoodResult:
    """label: one of CLASSIFIER_LABELS. offline=True: no answer could be had
    now (no network, API error, timeout); the engine treats the answer as
    unrecognized and leaves the audio for M7 to recognize later."""

    label: str = "unknown"
    transcript: str = ""
    offline: bool = False


class MoodClassifier(Protocol):
    def classify(self, path: Path) -> MoodResult: ...  # blocking; may raise


class FakeMoodClassifier:
    """Returns scripted results in order (then `default`). An Exception in the
    script is raised instead. `gate`, when given, blocks classify() until set."""

    def __init__(self, results: Iterable[MoodResult | Exception] = (), *,
                 default: MoodResult = MoodResult("unknown"),
                 gate: threading.Event | None = None):
        self.results = list(results)
        self.default = default
        self.gate = gate
        self.calls: list[Path] = []

    def classify(self, path: Path) -> MoodResult:
        self.calls.append(Path(path))
        if self.gate is not None:
            self.gate.wait(10)
        r = self.results.pop(0) if self.results else self.default
        if isinstance(r, Exception):
            raise r
        return r


# ------------------------------------------------------------ soundboard ----


class Soundboard(Protocol):
    def clips(self) -> list[Path]: ...


class DirSoundboard:
    """Every audio file under a directory (recursively), sorted."""

    def __init__(self, directory: str | Path, exts: Sequence[str] = AUDIO_EXTS):
        self.directory = Path(directory)
        self.exts = tuple(exts)

    def clips(self) -> list[Path]:
        try:
            return sorted(
                p for p in self.directory.rglob("*")
                if p.is_file() and p.suffix.lower() in self.exts and not p.name.startswith(".")
            )
        except OSError:
            log.exception("soundboard: cannot list %s", self.directory)
            return []


class StoreSoundboard:
    """The soundboard the engine process uses: the TTS starter clips from the
    content store that are servable (approved in the first month, SPEC 7.1),
    plus every audio file the owner imported into `directory` (no approval:
    the parent chose them). The legacy `starter/` subdirectory is ignored, so
    ungated TTS clips from an older bootstrap are never played."""

    IGNORED_SUBDIRS = ("starter",)

    def __init__(self, store, directory: str | Path | None = None):
        self.store = store
        self.dir = DirSoundboard(directory) if directory is not None else None

    def clips(self) -> list[Path]:
        out: list[Path] = []
        try:
            seen: set[str] = set()
            for it in self.store.content.list("soundboard", include_withheld=False,
                                              include_superseded=False, limit=1000):
                key = it.meta.get("key") or it.id
                if not it.servable or key in seen or "audio" not in it.media:
                    continue
                p = self.store.media_path(it.media["audio"])
                if p.is_file():
                    seen.add(key)  # newest servable take per clip
                    out.append(p)
        except Exception:
            log.exception("soundboard: store lookup failed")
        if self.dir is not None:
            for p in self.dir.clips():
                try:
                    first = p.relative_to(self.dir.directory).parts[0]
                except ValueError:
                    first = ""
                if first not in self.IGNORED_SUBDIRS:
                    out.append(p)
        return sorted(out)


class FakeSoundboard:
    def __init__(self, clips: Iterable[str | Path] = ()):
        self._clips = [Path(c) for c in clips]

    def clips(self) -> list[Path]:
        return list(self._clips)
