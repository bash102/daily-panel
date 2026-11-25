"""Slap-proof button input.

`ButtonArbiter` is pure logic (no threads, no hardware) fed with raw edges and
timestamps, so it is fully unit-testable. `ButtonInput` wraps it with a
hardware backend and a polling thread, and publishes events on a queue.

Rules (SPEC section 3):
- 20 ms debounce per button. Edges inside the window are ignored, but the pin
  is re-read when the window closes, so a very short tap is never "lost" and a
  button is never left stuck in the wrong state.
- First press wins. After an accepted press, every press on any button is
  dropped (not queued) for `lockout_s`.
- Holding the chord buttons together for `chord_hold_s` emits one ParentChord.

Interpreting presses (skip / switch / "Print ignored mid-item") is the
interaction engine's job, not this module's.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol, Union

from .config import Btn, ButtonConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Press:
    button: Btn
    t: float


@dataclass(frozen=True)
class ParentChord:
    t: float


Event = Union[Press, ParentChord]
Reader = Callable[[Btn], bool]


class ButtonArbiter:
    """Debounce, lockout and chord detection. Not thread-safe; callers lock."""

    def __init__(self, cfg: ButtonConfig, read: Reader):
        self.cfg = cfg
        self._read = read
        self._buttons = tuple(cfg.pins)
        self._down = {b: False for b in self._buttons}
        self._last_edge = {b: -math.inf for b in self._buttons}
        self._pending = {b: False for b in self._buttons}  # an edge was ignored
        self._last_accepted = -math.inf
        self._chord_since: float | None = None
        self._chord_fired = False

    def is_down(self, button: Btn) -> bool:
        return self._down[button]

    def edge(self, button: Btn, pressed: bool, t: float) -> list[Event]:
        """Raw edge from hardware. Returns zero or more events."""
        if t - self._last_edge[button] < self.cfg.debounce_s:
            self._pending[button] = True  # re-read once the window closes
            return []
        return self._apply(button, pressed, t)

    def tick(self, t: float) -> list[Event]:
        """Call periodically: settles pending debounces and checks the chord."""
        events: list[Event] = []
        for b in self._buttons:
            if self._pending[b] and t - self._last_edge[b] >= self.cfg.debounce_s:
                self._pending[b] = False
                level = bool(self._read(b))
                if level != self._down[b]:
                    events += self._apply(b, level, t)
        if (
            self._chord_since is not None
            and not self._chord_fired
            and t - self._chord_since >= self.cfg.chord_hold_s
        ):
            self._chord_fired = True
            events.append(ParentChord(t))
        return events

    def _apply(self, button: Btn, pressed: bool, t: float) -> list[Event]:
        if pressed == self._down[button]:
            return []  # duplicate edge, nothing changed
        self._last_edge[button] = t
        self._down[button] = pressed
        events: list[Event] = []
        if pressed and t - self._last_accepted >= self.cfg.lockout_s:
            self._last_accepted = t
            events.append(Press(button, t))
        both = all(self._down[b] for b in self.cfg.chord)
        if both and self._chord_since is None:
            self._chord_since, self._chord_fired = t, False
        elif not both:
            self._chord_since, self._chord_fired = None, False
        return events


class ButtonBackend(Protocol):
    def start(self, on_edge: Callable[[Btn, bool], None]) -> None: ...
    def read(self, button: Btn) -> bool: ...
    def close(self) -> None: ...


class FakeButtonBackend:
    """In-memory backend for tests and desktop runs."""

    def __init__(self, buttons):
        self.levels = {b: False for b in buttons}
        self._on_edge: Callable[[Btn, bool], None] | None = None

    def start(self, on_edge):
        self._on_edge = on_edge

    def read(self, button):
        return self.levels[button]

    def set(self, button: Btn, pressed: bool, *, notify: bool = True):
        self.levels[button] = pressed
        if notify and self._on_edge:
            self._on_edge(button, pressed)

    def close(self):
        self._on_edge = None


class ButtonInput:
    """Runs the arbiter against a backend; events appear on `self.events`."""

    def __init__(
        self,
        cfg: ButtonConfig,
        backend: ButtonBackend,
        clock: Callable[[], float] = time.monotonic,
        *,
        start_thread: bool = True,
    ):
        self.cfg = cfg
        self.backend = backend
        self.clock = clock
        self.events: "queue.Queue[Event]" = queue.Queue()
        self._lock = threading.Lock()
        self._arbiter = ButtonArbiter(cfg, backend.read)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        backend.start(self._on_edge)
        if start_thread:
            self._thread = threading.Thread(target=self._run, name="buttons", daemon=True)
            self._thread.start()

    def _publish(self, events: list[Event]) -> None:
        for e in events:
            log.debug("button event %s", e)
            self.events.put(e)

    def _on_edge(self, button: Btn, pressed: bool) -> None:
        with self._lock:
            events = self._arbiter.edge(button, pressed, self.clock())
        self._publish(events)

    def poll(self) -> None:
        """One reconcile/chord check. The thread calls this; tests may too."""
        with self._lock:
            events = self._arbiter.tick(self.clock())
        self._publish(events)

    def _run(self) -> None:
        while not self._stop.wait(self.cfg.poll_s):
            try:
                self.poll()
            except Exception:  # never let input die silently
                log.exception("button poll failed")

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self.backend.close()
