"""Fake audio backends for tests and off-Pi runs (no sound, no subprocesses)."""

from __future__ import annotations

import contextlib
import math
import struct
import subprocess
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .base import Output, Playback, Recording, SpeakerMuter, muted


class FakeProc:
    """A Proc that 'plays' until finish() or terminate()/kill()."""

    def __init__(self, argv=None, *, hang_on_terminate: bool = False):
        self.argv = argv
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.waited = False
        self.hang_on_terminate = hang_on_terminate
        self._exited = threading.Event()

    def finish(self, code: int = 0) -> None:
        if self.returncode is None:
            self.returncode = code
        self._exited.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.waited = True
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.hang_on_terminate:
            self.finish(-15)

    def kill(self):
        self.killed = True
        self.finish(-9)


@dataclass
class Played:
    path: Path
    output: Output
    playback: Playback
    proc: FakeProc


class FakeAudioOut:
    """Records every play; playbacks run until finish()/stop().

    Mirrors AlsaAudioOut:
    - speaker-only sounds requested while muted are not played; they are
      logged in `muted_plays` and return a done Playback with `suppressed=True`;
    - entering speaker_muted() stops SPEAKER playbacks already running;
    - with `check_files=True`, a path that does not exist returns a done
      Playback with `failed=True` (logged in `failed_plays`). Off by default
      so tests can play made-up paths.
    `auto_finish=True` makes every playback done immediately.
    """

    def __init__(self, *, auto_finish: bool = False, volume: float = 0.7, check_files: bool = False):
        self.auto_finish = auto_finish
        self.check_files = check_files
        self.volume = volume
        self.plays: list[Played] = []
        self.muted_plays: list[tuple[Path, Output]] = []
        self.failed_plays: list[tuple[Path, Output]] = []
        self.volume_calls: list[float] = []
        self.stop_all_calls = 0
        self._mute_depth = 0
        self._lock = threading.Lock()

    def play(self, path: Path, output: Output = Output.BOTH) -> Playback:
        path = Path(path)
        with self._lock:
            if self.check_files and not path.is_file():
                self.failed_plays.append((path, output))
                return Playback(failed=True)
            if self._mute_depth and output is Output.SPEAKER:
                self.muted_plays.append((path, output))
                return Playback(suppressed=True)
            proc = FakeProc([str(path)])
            if self.auto_finish:
                proc.finish()
            pb = Playback([proc], poll_s=0.001)
            self.plays.append(Played(path, output, pb, proc))
            return pb

    def stop_all(self) -> None:
        with self._lock:
            self.stop_all_calls += 1
            plays = list(self.plays)
        for p in plays:
            p.playback.stop()

    def set_volume(self, level: float) -> None:
        self.volume_calls.append(level)
        self.volume = min(max(float(level), 0.0), 1.0)

    # -- test helpers -----------------------------------------------------
    def finish_all(self) -> None:
        for p in list(self.plays):
            p.proc.finish()

    @property
    def last(self) -> Played | None:
        return self.plays[-1] if self.plays else None

    @property
    def playing(self) -> list[Played]:
        return [p for p in self.plays if not p.playback.done]

    @contextlib.contextmanager
    def speaker_muted(self) -> Iterator[None]:
        with self._lock:
            self._mute_depth += 1
            first = self._mute_depth == 1
            to_stop = [p for p in self.plays if p.output is Output.SPEAKER] if first else []
        for p in to_stop:
            p.playback.stop()
        try:
            yield
        finally:
            with self._lock:
                self._mute_depth -= 1

    @property
    def speaker_is_muted(self) -> bool:
        return self._mute_depth > 0


@dataclass(frozen=True)
class FakeTake:
    duration_s: float = 2.0
    silent: bool = False
    wait_for_stop: bool = False  # block until the stop event (or max_s)


class FakeAudioIn:
    """Scripted recorder. Each record() pops the next FakeTake (or repeats a default).

    Writes a real, small WAV (tone or silence) so downstream code can read it.
    Mutes `muter` (e.g. a FakeAudioOut) for the duration, like the real one.
    Like the real recorder: a stop event already set on entry gives an empty
    silent take, and a take shorter than `min_speech_s` is silent.
    """

    RATE = 8000

    def __init__(self, takes: list[FakeTake] | None = None, *, muter: SpeakerMuter | None = None,
                 default: FakeTake = FakeTake(), min_speech_s: float = 0.3):
        self.takes = list(takes or [])
        self.default = default
        self.muter = muter
        self.min_speech_s = min_speech_s
        self.calls: list[tuple[Path, float]] = []
        self.muted_during: list[bool] = []

    def record(self, path: Path, max_s: float, stop: threading.Event) -> Recording:
        path = Path(path)
        self.calls.append((path, max_s))
        take = self.takes.pop(0) if self.takes else self.default
        with muted(self.muter):
            self.muted_during.append(bool(getattr(self.muter, "speaker_is_muted", False)))
            if stop.is_set():
                duration = 0.0
            else:
                if take.wait_for_stop:
                    stop.wait(max(max_s, 0.0))
                duration = min(take.duration_s, max(max_s, 0.0))
            _write_wav(path, duration, take.silent, self.RATE)
        silent = take.silent or duration <= 0.0 or duration < self.min_speech_s
        return Recording(path=path, duration_s=duration, silent=silent)


def _write_wav(path: Path, duration_s: float, silent: bool, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(duration_s * rate)
    if silent:
        frames = b"\x00\x00" * n
    else:
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n)
        )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
