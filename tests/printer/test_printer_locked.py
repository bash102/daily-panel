import multiprocessing
import threading
import time

import pytest
from PIL import Image

from zozobox.printer import (FakePrinter, LockedPrinter, PrinterError, SupervisedPrinter,
                             brother_factory, file_lock, lock_path, make_locked_brother)
from zozobox.printer.backend import QLConfig


class SlowPrinter:
    def __init__(self, hold=0.2):
        self.hold = hold
        self.active = 0
        self.max_active = 0
        self.count = 0
        self._lock = threading.Lock()

    def print_image(self, img):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.hold)
        with self._lock:
            self.active -= 1
            self.count += 1


def img():
    return Image.new("1", (696, 100), 1)


def test_lock_path(tmp_path):
    assert lock_path(tmp_path) == tmp_path / ".printer.lock"


def test_locked_printer_serializes_threads(tmp_path):
    inner = SlowPrinter(0.05)
    a = LockedPrinter(inner, lock_path(tmp_path))
    b = LockedPrinter(inner, lock_path(tmp_path))
    ts = [threading.Thread(target=p.print_image, args=(img(),)) for p in (a, b, a, b)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert inner.count == 4 and inner.max_active == 1


def _hold_lock(path, ready, release):
    with file_lock(path, 5):
        ready.set()
        release.wait(5)


def test_lock_held_by_another_process_times_out(tmp_path):
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(lock_path(tmp_path), ready, release))
    proc.start()
    try:
        assert ready.wait(5)
        fp = FakePrinter()
        p = LockedPrinter(fp, lock_path(tmp_path), timeout_s=0.2)
        with pytest.raises(PrinterError, match="busy"):
            p.print_image(img())
        assert fp.count == 0
        release.set()
        proc.join(5)
        p.print_image(img())
        assert fp.count == 1
    finally:
        release.set()
        proc.join(5)


def test_inner_errors_pass_through_and_release(tmp_path):
    class Bad:
        def print_image(self, img):
            raise PrinterError("jam")

    p = LockedPrinter(Bad(), lock_path(tmp_path), timeout_s=0.1)
    with pytest.raises(PrinterError, match="jam"):
        p.print_image(img())
    with file_lock(lock_path(tmp_path), 0.1):  # released
        pass


def test_unusable_lock_dir_is_a_printer_error(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    p = LockedPrinter(FakePrinter(), f / "sub" / ".printer.lock")
    with pytest.raises(PrinterError, match="lock unavailable"):
        p.print_image(img())


def test_supervised_printer_retries(tmp_path):
    t = [0.0]
    attempts = []
    fp = FakePrinter()

    def factory():
        attempts.append(t[0])
        if len(attempts) < 3:
            raise PrinterError("not on USB")
        return fp

    sup = SupervisedPrinter(factory, retry_s=60, clock=lambda: t[0])
    assert not sup.ensure() and "not on USB" in sup.last_error
    with pytest.raises(PrinterError, match="not available"):
        sup.print_image(img())
    assert len(attempts) == 1  # not due yet
    t[0] = 61
    with pytest.raises(PrinterError):
        sup.print_image(img())
    assert len(attempts) == 2
    t[0] = 200
    sup.print_image(img())
    assert sup.available and fp.count == 1 and sup.last_error is None
    sup.print_image(img())
    assert len(attempts) == 3


def test_supervised_printer_drops_on_library_error():
    class Gone:
        def print_image(self, img):
            raise PrinterError("brother_ql is not installed")

    made = []
    sup = SupervisedPrinter(lambda: made.append(1) or Gone(), retry_s=60, clock=lambda: 0.0)
    with pytest.raises(PrinterError):
        sup.print_image(img())
    assert not sup.available
    with pytest.raises(PrinterError):
        sup.print_image(img())
    assert len(made) == 2


def test_brother_factory_uses_probe():
    def probe():
        raise PrinterError("no USB device")

    with pytest.raises(PrinterError, match="no USB device|not installed"):
        brother_factory(QLConfig(), probe=probe)()


def test_make_locked_brother_builds_lazily(tmp_path):
    p = make_locked_brother(tmp_path, retry_s=1)
    assert isinstance(p, LockedPrinter) and isinstance(p.inner, SupervisedPrinter)
    assert p.path == tmp_path / ".printer.lock"
