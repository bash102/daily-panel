"""Cross-process printer lock and a self-healing printer wrapper (PLAN M10).

The engine and the dashboard both print on the one USB QL-800. Two processes
writing raster data at once garble both cards, so every print goes through
`LockedPrinter`, which holds an exclusive `fcntl.flock` on a lock file in the
data dir for the whole job:

    printer = LockedPrinter(BrotherQLPrinter(cfg), lock_path(data_dir))

`SupervisedPrinter` builds the real printer lazily and keeps retrying when
that fails (library missing, printer unplugged at boot), so a missing printer
never stops audio or buttons: prints just fail with PrinterError until it is
back.

`make_locked_brother(data_dir, cfg)` is the production wiring both processes
use.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .backend import BROTHER_VENDOR_ID, QL800_PRODUCT_ID, BrotherQLPrinter, PrinterError, QLConfig

log = logging.getLogger(__name__)

LOCK_NAME = ".printer.lock"


def lock_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / LOCK_NAME


@contextlib.contextmanager
def file_lock(path: str | Path, timeout_s: float, *, poll_s: float = 0.05,
              clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    """Exclusive flock on `path`, waiting up to `timeout_s`. Raises
    TimeoutError when another holder keeps it. Each call opens its own file
    description, so this also serializes threads of one process."""
    import fcntl

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
    try:
        deadline = clock() + max(timeout_s, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    raise TimeoutError(f"lock {path} busy for {timeout_s:.0f}s") from None
                sleep(poll_s)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class LockedPrinter:
    """Wraps a Printer; each print_image holds the cross-process lock.

    A lock that stays busy longer than `timeout_s` raises PrinterError
    ("printer busy"), so the caller's own timeout (engine print_timeout_s=60)
    is not exceeded and no card prints long after the toddler moved on."""

    def __init__(self, inner: Any, path: str | Path, *, timeout_s: float = 30.0):
        self.inner = inner
        self.path = Path(path)
        self.timeout_s = timeout_s

    def print_image(self, img) -> None:
        lock = file_lock(self.path, self.timeout_s)
        try:
            lock.__enter__()
        except TimeoutError as e:
            raise PrinterError(f"printer busy: {e}") from None
        except OSError as e:  # lock file unusable (read-only dir, permissions)
            raise PrinterError(f"printer lock unavailable: {e}") from None
        try:
            self.inner.print_image(img)
        finally:
            lock.__exit__(None, None, None)

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if close is not None:
            close()


class SupervisedPrinter:
    """Builds its printer with `factory()`; retries every `retry_s` until it
    works. While unavailable, print_image raises PrinterError (the engine then
    plays `printer_sleepy` and the item prints on a later turn).

    `ensure()` attempts a build when one is due; the app calls it from a
    background timer, and print_image calls it too."""

    def __init__(self, factory: Callable[[], Any], *, retry_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic, name: str = "printer"):
        self.factory = factory
        self.retry_s = retry_s
        self.clock = clock
        self.name = name
        self._printer: Any = None
        self._next_try = -float("inf")
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.attempts = 0

    @property
    def available(self) -> bool:
        return self._printer is not None

    def ensure(self, *, force: bool = False) -> bool:
        with self._lock:
            if self._printer is not None:
                return True
            now = self.clock()
            if not force and now < self._next_try:
                return False
            self.attempts += 1
            try:
                self._printer = self.factory()
            except Exception as e:
                self._next_try = now + self.retry_s
                msg = f"{type(e).__name__}: {e}"
                if msg != self.last_error:
                    log.error("%s unavailable (%s); retrying every %.0fs", self.name, msg, self.retry_s)
                else:
                    log.debug("%s still unavailable: %s", self.name, msg)
                self.last_error = msg
                return False
            if self.last_error is not None:
                log.info("%s is available again", self.name)
            self.last_error = None
            return True

    def print_image(self, img) -> None:
        if not self.ensure():
            raise PrinterError(f"{self.name} not available: {self.last_error or 'not set up'}")
        try:
            self._printer.print_image(img)
        except PrinterError as e:
            if _looks_gone(e):
                self._drop(str(e))
            raise

    def _drop(self, why: str) -> None:
        with self._lock:
            log.warning("%s dropped (%s); will set it up again", self.name, why)
            self._printer = None
            self._next_try = -float("inf")  # next print or timer tick retries at once
            self.last_error = why

    def close(self) -> None:
        p = self._printer
        if p is not None and hasattr(p, "close"):
            with contextlib.suppress(Exception):
                p.close()


def _looks_gone(e: Exception) -> bool:
    s = str(e).lower()
    return "not installed" in s or "unusable brother_ql backend" in s


def usb_probe(vendor: int = BROTHER_VENDOR_ID, product: int = QL800_PRODUCT_ID) -> None:
    """Raise PrinterError when pyusb is present and sees no such device.
    Without pyusb (off-Pi) it does nothing: the print itself will say."""
    try:
        import usb.core  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        dev = usb.core.find(idVendor=vendor, idProduct=product)
    except Exception as e:  # no libusb backend, permissions...
        raise PrinterError(f"cannot scan USB: {type(e).__name__}: {e}") from None
    if dev is None:
        raise PrinterError(f"no USB device {vendor:04x}:{product:04x} (QL-800 off or unplugged?)")


def brother_factory(cfg: QLConfig, *, probe: Callable[[], None] | None = None) -> Callable[[], Any]:
    """factory() for SupervisedPrinter: checks brother_ql imports and (for a
    usb:// identifier) that the printer is on the bus, then builds it."""

    def make() -> BrotherQLPrinter:
        try:
            import brother_ql  # noqa: F401
        except ImportError:
            raise PrinterError("brother_ql is not installed (pip install -e .[pi])") from None
        if probe is not None:
            probe()
        elif cfg.backend == "pyusb" and cfg.identifier.startswith("usb://"):
            usb_probe(*_ids(cfg.identifier))
        return BrotherQLPrinter(cfg)

    return make


def _ids(identifier: str) -> tuple[int, int]:
    try:
        vid, _, pid = identifier[len("usb://"):].split("/")[0].partition(":")
        return int(vid, 16), int(pid, 16)
    except ValueError:
        return BROTHER_VENDOR_ID, QL800_PRODUCT_ID


def make_locked_brother(data_dir: str | Path, cfg: QLConfig | None = None, *,
                        retry_s: float = 60.0, lock_timeout_s: float = 30.0) -> LockedPrinter:
    """The production printer for both the engine and the dashboard."""
    sup = SupervisedPrinter(brother_factory(cfg or QLConfig()), retry_s=retry_s,
                            name="QL-800 printer")
    return LockedPrinter(sup, lock_path(data_dir), timeout_s=lock_timeout_s)


__all__ = [
    "LOCK_NAME", "LockedPrinter", "SupervisedPrinter", "brother_factory", "file_lock",
    "lock_path", "make_locked_brother", "usb_probe",
]
