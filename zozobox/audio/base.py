"""Audio types shared by the ALSA backend and the fakes (PLAN.md M2 contract)."""

from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ContextManager, Iterable, Protocol

log = logging.getLogger(__name__)


class Output(str, Enum):
    BOTH = "both"  # handset earpiece + box speaker (speech)
    SPEAKER = "speaker"  # box speaker only (chimes, clicks)


class Proc(Protocol):
    """The subset of subprocess.Popen that Playback needs."""

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


def reap(proc: Proc, grace_s: float = 0.5) -> None:
    """Terminate, then kill, a process and always wait for it (no zombies)."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=grace_s)
                return
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        log.error("process did not exit after kill: %r", proc)
    except Exception:  # never let cleanup crash the box
        log.exception("failed to reap process %r", proc)


class Playback:
    """Handle for one sound, possibly playing on several outputs at once.

    Done when every underlying process has exited. `stop()` is idempotent and
    thread-safe. A Playback with no processes (missing file, muted output,
    spawn failure) is done immediately.

    - `failed`: the sound did not play properly: it could not start, or an
      output's player exited non-zero without being stopped by us (e.g. aplay
      "No such device" after a USB card dropped out).
    - `suppressed`: nothing was played on purpose because the box speaker is
      muted (a recording is running). Callers that must be heard (the button
      click) can replay it once recording has ended.
    """

    def __init__(self, procs: Iterable[Proc] = (), *, failed: bool = False,
                 suppressed: bool = False, poll_s: float = 0.02):
        self._procs = list(procs)
        self._lock = threading.Lock()
        self._stopped = False
        self._released: set[int] = set()  # ids of procs we stopped on purpose
        self._poll_s = poll_s
        self._failed = failed
        self.suppressed = suppressed

    @property
    def done(self) -> bool:
        with self._lock:
            return all(p.poll() is not None for p in self._procs)

    @property
    def failed(self) -> bool:
        if self._failed:
            return True
        with self._lock:
            if self._stopped:
                return False
            procs = [p for p in self._procs if id(p) not in self._released]
        return any(p.poll() not in (None, 0) for p in procs)

    @property
    def stopped(self) -> bool:
        return self._stopped

    def wait(self, timeout: float | None = None) -> bool:
        """Block until done; True if done, False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.done:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(self._poll_s)
        return True

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            procs = list(self._procs)
        for p in procs:
            reap(p)

    def stop_procs(self, procs: Iterable[Proc]) -> None:
        """Stop some outputs on purpose (e.g. the speaker on mute); not a failure."""
        procs = list(procs)
        with self._lock:
            self._released.update(id(p) for p in procs)
        for p in procs:
            reap(p)

    def released(self, proc: Proc) -> bool:
        """True if `proc` was stopped on purpose (stop() or stop_procs())."""
        with self._lock:
            return self._stopped or id(proc) in self._released

    @property
    def procs(self) -> list[Proc]:
        return list(self._procs)


class SpeakerMuter(Protocol):
    def speaker_muted(self) -> ContextManager[None]: ...


class AudioOut(Protocol):
    def play(self, path: Path, output: Output = Output.BOTH) -> Playback: ...  # non-blocking
    def stop_all(self) -> None: ...
    def set_volume(self, level: float) -> None: ...  # 0..1, always under the ceiling


@dataclass(frozen=True)
class Recording:
    path: Path
    duration_s: float
    silent: bool


class AudioIn(Protocol):
    def record(self, path: Path, max_s: float, stop: threading.Event) -> Recording: ...  # blocking


def muted(muter: SpeakerMuter | None) -> ContextManager[None]:
    return muter.speaker_muted() if muter is not None else contextlib.nullcontext()
