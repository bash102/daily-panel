"""Button and LED backends that retry their hardware init (PLAN M10).

The engine must not run for hours with inert buttons because the GPIO chip
or the I2C bus was not ready at boot. These wrappers start with no hardware
(buttons read as released, LED writes go nowhere), and `ensure()` retries
the real backend at most every `retry_s`. The engine's main loop calls
`App.maintain()` once a second, which calls `ensure()`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable

from ..config import Btn

log = logging.getLogger(__name__)


class _Retrying:
    def __init__(self, factory: Callable[[], Any], *, retry_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic, name: str = "hardware"):
        self.factory = factory
        self.retry_s = retry_s
        self.clock = clock
        self.name = name
        self.inner: Any = None
        self.last_error: str | None = None
        self.attempts = 0
        self._next_try = -float("inf")
        self._lock = threading.Lock()
        self._closed = False

    @property
    def available(self) -> bool:
        return self.inner is not None

    def ensure(self) -> bool:
        """Try to bring the hardware up (rate-limited). True when it is up."""
        with self._lock:
            if self.inner is not None or self._closed:
                return self.inner is not None
            now = self.clock()
            if now < self._next_try:
                return False
            self._next_try = now + self.retry_s
            self.attempts += 1
            try:
                inner = self.factory()
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                if msg != self.last_error:
                    log.error("%s: init failed (%s); retrying every %.0f s", self.name, msg,
                              self.retry_s)
                self.last_error = msg
                return False
            try:
                self._attach(inner)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.error("%s: start failed (%s)", self.name, self.last_error)
                try:
                    inner.close()
                except Exception:
                    pass
                return False
            self.inner = inner
            if self.attempts > 1:
                log.warning("%s: up after %d attempts", self.name, self.attempts)
            return True

    def _attach(self, inner: Any) -> None:
        pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            inner, self.inner = self.inner, None
        if inner is not None:
            inner.close()


class RetryingButtonBackend(_Retrying):
    """ButtonBackend: every button reads released until the GPIO backend is up;
    then edges flow from it to the callback given to start()."""

    def __init__(self, factory: Callable[[], Any], buttons: Iterable[Btn], **kw):
        kw.setdefault("name", "buttons")
        super().__init__(factory, **kw)
        self.buttons = list(buttons)
        self._on_edge: Callable[[Btn, bool], None] | None = None

    def _attach(self, inner: Any) -> None:
        if self._on_edge is not None:
            inner.start(self._on_edge)

    def start(self, on_edge: Callable[[Btn, bool], None]) -> None:
        with self._lock:
            self._on_edge = on_edge
            inner = self.inner
        if inner is not None:
            inner.start(on_edge)

    def read(self, button: Btn) -> bool:
        inner = self.inner
        return bool(inner.read(button)) if inner is not None else False


class RetryingLedBackend(_Retrying):
    """LedBackend: writes are dropped until the PCA9685 is up. After it comes
    up, call LedController.invalidate() so every channel is rewritten."""

    def __init__(self, factory: Callable[[], Any], **kw):
        kw.setdefault("name", "leds")
        super().__init__(factory, **kw)

    def set_duty(self, channel: int, duty: int) -> None:
        inner = self.inner
        if inner is not None:
            inner.set_duty(channel, duty)


__all__ = ["RetryingButtonBackend", "RetryingLedBackend"]
