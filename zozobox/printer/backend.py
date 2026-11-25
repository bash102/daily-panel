"""Printer backends: the `Printer` protocol, `FakePrinter`, and the Brother
QL-800 backend built on the `brother_ql` library (imported lazily so the rest
of the box runs without it).
"""

from __future__ import annotations

import errno
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from PIL import Image

from .render import WIDTH_PX, to_1bit

log = logging.getLogger(__name__)

BROTHER_VENDOR_ID = 0x04F9
QL800_PRODUCT_ID = 0x209B  # not listed by brother_ql 0.9.4; from Brother's USB IDs
DEFAULT_IDENTIFIER = f"usb://0x{BROTHER_VENDOR_ID:04x}:0x{QL800_PRODUCT_ID:04x}"


class PrinterError(Exception):
    """Printing failed: printer missing, out of paper, cover open, bad image..."""


class Printer(Protocol):
    def print_image(self, img: Image.Image) -> None: ...  # raises PrinterError


def prepare(img: Image.Image) -> Image.Image:
    """Validate a card for printing and make sure it is 1-bit."""
    if not isinstance(img, Image.Image):
        raise PrinterError(f"expected a PIL image, got {type(img).__name__}")
    if img.width != WIDTH_PX:
        raise PrinterError(f"image is {img.width}px wide; the 62mm roll needs exactly {WIDTH_PX}px")
    if img.height < 1:
        raise PrinterError("image has no height")
    return img if img.mode == "1" else to_1bit(img)


class FakePrinter:
    """Saves every print as a PNG (when given a directory) and keeps it in memory.

    Set `fail_next` to make the next N prints raise PrinterError.
    """

    def __init__(self, out_dir: Path | str | None = None, clock: Callable[[], float] = time.time):
        self.out_dir = Path(out_dir) if out_dir is not None else None
        self.clock = clock
        self.images: list[Image.Image] = []
        self.paths: list[Path] = []
        self.fail_next = 0
        self._lock = threading.Lock()

    def print_image(self, img: Image.Image) -> None:
        img = prepare(img)
        with self._lock:
            if self.fail_next:
                self.fail_next -= 1
                raise PrinterError("simulated printer failure")
            path = None
            if self.out_dir is not None:
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
                path = self.out_dir / f"print-{len(self.images) + 1:04d}-{stamp}.png"
                try:
                    self.out_dir.mkdir(parents=True, exist_ok=True)
                    img.save(path)
                except OSError as e:
                    raise PrinterError(f"could not save fake print to {path}: {e}") from None
            # Only count the print once it has fully "happened".
            self.images.append(img.copy())
            if path is not None:
                self.paths.append(path)
                log.info("fake print saved to %s", path)

    @property
    def count(self) -> int:
        return len(self.images)


@dataclass(frozen=True)
class QLConfig:
    model: str = "QL-800"
    label: str = "62"  # DK-22205, 62mm continuous, black on white
    backend: str = "pyusb"
    identifier: str = DEFAULT_IDENTIFIER
    cut: bool = True
    hq: bool = True
    threshold: float = 70.0  # brother_ql's own threshold; our images are already 0/255
    blocking: bool = True  # wait for the printer's "printing completed" status


def _is_timeout(e: BaseException) -> bool:
    try:
        import usb.core  # type: ignore[import-not-found]

        if isinstance(e, getattr(usb.core, "USBTimeoutError", ())):
            return True
    except ImportError:
        pass
    return getattr(e, "errno", None) == errno.ETIMEDOUT


def send_instructions(
    instructions: bytes,
    *,
    printer_identifier: str,
    backend_identifier: str = "pyusb",
    blocking: bool = True,
    backend_class: Callable[[str], Any] | None = None,
    timeout: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Send raster bytes to a QL printer and (optionally) read back its status.

    A replacement for `brother_ql.backends.helpers.send` that:
    - raises PrinterError only when the job could not be opened/written;
    - never raises once the bytes are out: read-back failures (the QL status
      read is flaky over USB; pyusb raises USBTimeoutError) are logged and the
      status is returned with outcome 'sent' / did_print False;
    - always disposes the backend (releases the USB interface, reattaches the
      kernel driver), so a failed job cannot keep the device claimed.

    Returns a dict shaped like brother_ql's: instructions_sent, outcome
    ('sent' | 'printed' | 'error'), printer_state, did_print, ready_for_next_job.
    """
    try:
        if backend_class is None:
            from brother_ql.backends import backend_factory

            backend_class = backend_factory(backend_identifier)["backend_class"]
        from brother_ql.reader import interpret_response
    except ImportError:
        raise PrinterError("brother_ql is not installed (pip install brother_ql)") from None
    except Exception as e:  # NotImplementedError for an unknown backend name
        raise PrinterError(f"unusable brother_ql backend {backend_identifier!r}: {e}") from None

    status: dict = {
        "instructions_sent": False,
        "outcome": "unknown",
        "printer_state": None,
        "did_print": False,
        "ready_for_next_job": False,
    }
    printer = None
    try:
        try:
            printer = backend_class(printer_identifier)
            printer.write(instructions)
        except Exception as e:  # ValueError('Device not found'), usb.core.USBError, ...
            # str-only, no __cause__: the traceback would pin the device handle.
            raise PrinterError(f"could not send to {printer_identifier}: {type(e).__name__}: {e}") from None
        status["instructions_sent"] = True
        status["outcome"] = "sent"
        if not blocking or backend_identifier == "network":
            return status

        start = clock()
        while clock() - start < timeout:
            try:
                data = printer.read()
            except Exception as e:
                if _is_timeout(e):
                    sleep(0.005)
                    continue
                log.warning("printer status read failed after sending (%s: %s); print unconfirmed", type(e).__name__, e)
                break
            if not data:
                sleep(0.005)
                continue
            try:
                result = interpret_response(data)
            except Exception:
                log.debug("could not understand printer response %r", data)
                continue
            status["printer_state"] = result
            if result.get("errors"):
                status["outcome"] = "error"
                break
            if result.get("status_type") == "Printing completed":
                status["did_print"] = True
                status["outcome"] = "printed"
            if result.get("status_type") == "Phase change" and result.get("phase_type") == "Waiting to receive":
                status["ready_for_next_job"] = True
            if status["did_print"] and status["ready_for_next_job"]:
                break
        return status
    finally:
        if printer is not None:
            try:
                printer.dispose()
            except Exception as e:  # pragma: no cover - brother_ql's dispose already swallows
                log.debug("printer dispose failed: %s", e)


class BrotherQLPrinter:
    """Brother QL-800 over USB via `brother_ql` (pyusb backend).

    One print at a time (a lock serialises callers). Failures before or while
    writing the job (library missing, printer unplugged, write error) and an
    error status read back raise PrinterError. Once the bytes are written, a
    missing/failed status read only logs a warning (the card most likely
    printed; re-raising could cause a duplicate on retry).

    `send` (a callable with `send_instructions`' keyword signature returning a
    status dict) or `backend_class` (a brother_ql backend class) can be
    injected for tests.
    """

    def __init__(
        self,
        cfg: QLConfig = QLConfig(),
        *,
        send: Callable[..., Any] | None = None,
        backend_class: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self._send = send
        self._backend_class = backend_class
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self.last_status: dict | None = None

    def instructions(self, img: Image.Image) -> bytes:
        """Raster instruction bytes for one card (no I/O)."""
        img = prepare(img)
        try:
            from brother_ql.conversion import convert
            from brother_ql.raster import BrotherQLRaster
        except ImportError as e:
            raise PrinterError("brother_ql is not installed (pip install brother_ql)") from e
        try:
            qlr = BrotherQLRaster(self.cfg.model)
            qlr.exception_on_warning = True
            return convert(
                qlr,
                [img.convert("L")],  # 'L' 0/255 so the library's threshold is exact
                self.cfg.label,
                cut=self.cfg.cut,
                dither=False,
                compress=False,
                red=False,
                rotate="0",
                dpi_600=False,
                hq=self.cfg.hq,
                threshold=self.cfg.threshold,
            )
        except Exception as e:
            raise PrinterError(f"could not build raster data: {e}") from e

    def _do_send(self, data: bytes) -> Any:
        kw = dict(
            instructions=data,
            printer_identifier=self.cfg.identifier,
            backend_identifier=self.cfg.backend,
            blocking=self.cfg.blocking,
        )
        if self._send is not None:
            try:
                return self._send(**kw)
            except PrinterError:
                raise
            except Exception as e:
                raise PrinterError(
                    f"could not send to {self.cfg.model} at {self.cfg.identifier}: {type(e).__name__}: {e}"
                ) from None
        return send_instructions(
            data,
            printer_identifier=self.cfg.identifier,
            backend_identifier=self.cfg.backend,
            blocking=self.cfg.blocking,
            backend_class=self._backend_class,
            clock=self._clock,
            sleep=self._sleep,
        )

    def print_image(self, img: Image.Image) -> None:
        data = self.instructions(img)
        with self._lock:
            status = self._do_send(data)
        self.last_status = status if isinstance(status, dict) else None
        if not isinstance(status, dict):
            return
        if status.get("outcome") == "error":
            state = status.get("printer_state") or {}
            errors = state.get("errors") if isinstance(state, dict) else None
            raise PrinterError(f"printer reported an error: {', '.join(errors or []) or 'unknown'}")
        if self.cfg.blocking and not status.get("did_print"):
            # Read-back over USB is not always reliable; the job was sent, so log only.
            log.warning("printer did not confirm the print (outcome=%s)", status.get("outcome"))

    def close(self) -> None:
        pass
