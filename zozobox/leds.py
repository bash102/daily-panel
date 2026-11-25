"""Button LED control (SPEC section 4).

States: OFF, DIM (available), BRIGHT (something new), PULSE_SLOW (playing),
PULSE_FAST (recording). `LedController` renders states to 16-bit duty cycles
with gamma correction and writes only channels whose duty changed. I2C errors
are logged and retried on the next frame rather than crashing the box.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from enum import Enum
from typing import Callable, Protocol

from .config import Btn, LedConfig

log = logging.getLogger(__name__)

DUTY_MAX = 0xFFFF


class LedState(str, Enum):
    OFF = "off"
    DIM = "dim"
    BRIGHT = "bright"
    PULSE_SLOW = "pulse_slow"
    PULSE_FAST = "pulse_fast"


def level_for(state: LedState, t: float, since: float, cfg: LedConfig) -> float:
    """Perceived brightness 0..1 for a state at time t (state set at `since`)."""
    if state is LedState.OFF:
        return 0.0
    if state is LedState.DIM:
        return cfg.dim_level
    if state is LedState.BRIGHT:
        return 1.0
    period = cfg.slow_pulse_s if state is LedState.PULSE_SLOW else cfg.fast_pulse_s
    # Starts at dim and rises, so a state change never jumps abruptly to full.
    wave = 0.5 - 0.5 * math.cos(2 * math.pi * (t - since) / period)
    return cfg.dim_level + (1.0 - cfg.dim_level) * wave


def to_duty(level: float, cfg: LedConfig, trim: float = 1.0) -> int:
    level = min(max(level, 0.0), 1.0) * min(max(cfg.max_brightness, 0.0), 1.0)
    level = min(level * max(trim, 0.0), 1.0)
    return round((level ** cfg.gamma) * DUTY_MAX)


class LedBackend(Protocol):
    def set_duty(self, channel: int, duty: int) -> None: ...
    def close(self) -> None: ...


class FakeLedBackend:
    def __init__(self):
        self.duty: dict[int, int] = {}
        self.writes = 0
        self.fail_next = 0  # simulate I2C errors
        self.closed = False

    def set_duty(self, channel, duty):
        if self.fail_next:
            self.fail_next -= 1
            raise OSError(121, "Remote I/O error")
        self.duty[channel] = duty
        self.writes += 1

    def close(self):
        self.closed = True


class LedController:
    def __init__(
        self,
        cfg: LedConfig,
        backend: LedBackend,
        clock: Callable[[], float] = time.monotonic,
        *,
        start_thread: bool = True,
    ):
        self.cfg = cfg
        self.backend = backend
        self.clock = clock
        self._lock = threading.Lock()
        now = clock()
        self._state = {b: (LedState.OFF, now) for b in cfg.channels}
        self._written: dict[Btn, int | None] = {b: None for b in cfg.channels}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.render()
        if start_thread:
            self._thread = threading.Thread(target=self._run, name="leds", daemon=True)
            self._thread.start()

    def set(self, button: Btn, state: LedState) -> None:
        with self._lock:
            if self._state[button][0] is not state:
                self._state[button] = (state, self.clock())

    def set_all(self, state: LedState) -> None:
        for b in self.cfg.channels:
            self.set(b, state)

    def get(self, button: Btn) -> LedState:
        with self._lock:
            return self._state[button][0]

    def invalidate(self) -> None:
        """Forget what was written, so the next render rewrites every channel
        (after the LED driver came back)."""
        with self._lock:
            self._written = {b: None for b in self.cfg.channels}

    def render(self) -> None:
        t = self.clock()
        with self._lock:
            targets = {
                b: to_duty(level_for(s, t, since, self.cfg), self.cfg, self.cfg.trim.get(b, 1.0))
                for b, (s, since) in self._state.items()
            }
        for b, duty in targets.items():
            if self._written[b] == duty:
                continue
            try:
                self.backend.set_duty(self.cfg.channels[b], duty)
                self._written[b] = duty
            except OSError as e:
                self._written[b] = None  # unknown; force a rewrite next frame
                log.warning("LED write failed for %s: %s", b.value, e)

    def _run(self) -> None:
        interval = 1.0 / self.cfg.frame_hz
        while not self._stop.wait(interval):
            try:
                self.render()
            except Exception:
                log.exception("LED render failed")

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self.set_all(LedState.OFF)
        self.render()
        self.backend.close()
