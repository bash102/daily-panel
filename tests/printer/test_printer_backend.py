import builtins
import errno
import sys
import threading

import pytest
from PIL import Image

from zozobox.printer import (
    DEFAULT_IDENTIFIER,
    WIDTH_PX,
    BrotherQLPrinter,
    FakePrinter,
    Printer,
    PrinterError,
    QLConfig,
    prepare,
    render_mood_card,
)


def card():
    return render_mood_card("happy")


# ------------------------------------------------------------------ fake ----


def test_fake_printer_saves_pngs(tmp_path):
    p = FakePrinter(tmp_path / "prints", clock=lambda: 0)
    p.print_image(card())
    p.print_image(card())
    assert p.count == 2
    assert len(p.paths) == 2 and all(x.exists() for x in p.paths)
    assert p.paths[0] != p.paths[1]
    saved = Image.open(p.paths[0])
    assert saved.width == WIDTH_PX and saved.mode == "1"


def test_fake_printer_in_memory_only():
    p = FakePrinter()
    p.print_image(card())
    assert p.count == 1 and p.paths == []


def test_fake_printer_simulated_failure():
    p = FakePrinter()
    p.fail_next = 1
    with pytest.raises(PrinterError):
        p.print_image(card())
    p.print_image(card())
    assert p.count == 1


def test_wrong_width_rejected():
    p = FakePrinter()
    with pytest.raises(PrinterError):
        p.print_image(Image.new("1", (500, 300), 1))
    with pytest.raises(PrinterError):
        p.print_image("not an image")
    assert p.count == 0


def test_prepare_converts_to_1bit():
    img = Image.new("L", (WIDTH_PX, 100), 200)
    out = prepare(img)
    assert out.mode == "1"
    assert set(out.convert("L").getdata()) == {255}


def test_protocol_conformance():
    def use(p: Printer):
        p.print_image(card())

    use(FakePrinter())
    use(BrotherQLPrinter(send=lambda **kw: {"outcome": "printed", "did_print": True}))


# ------------------------------------------------------------ brother_ql ----


class FakeRaster:
    def __init__(self, model):
        self.model = model
        self.exception_on_warning = False


@pytest.fixture
def fake_lib(monkeypatch):
    """Stand-in brother_ql modules, so the backend's wiring is tested even
    where the real library is not installed."""
    import types

    calls = {}

    def convert(qlr, images, label, **kw):
        calls["convert"] = (qlr, images, label, kw)
        return b"RASTER"

    mods = {
        "brother_ql": types.ModuleType("brother_ql"),
        "brother_ql.raster": types.ModuleType("brother_ql.raster"),
        "brother_ql.conversion": types.ModuleType("brother_ql.conversion"),
    }
    mods["brother_ql.raster"].BrotherQLRaster = FakeRaster
    mods["brother_ql.conversion"].convert = convert
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    return calls


def test_ql_defaults():
    cfg = QLConfig()
    assert cfg.model == "QL-800" and cfg.label == "62" and cfg.backend == "pyusb"
    assert cfg.identifier == DEFAULT_IDENTIFIER == "usb://0x04f9:0x209b"
    with pytest.raises(Exception):
        cfg.model = "x"  # frozen


def test_ql_print_sends_instructions(fake_lib):
    sent = []

    def send(**kw):
        sent.append(kw)
        return {"outcome": "printed", "did_print": True, "ready_for_next_job": True, "printer_state": None}

    p = BrotherQLPrinter(QLConfig(identifier="usb://0x04f9:0x209b/SER1"), send=send)
    p.print_image(card())
    assert sent == [
        {
            "instructions": b"RASTER",
            "printer_identifier": "usb://0x04f9:0x209b/SER1",
            "backend_identifier": "pyusb",
            "blocking": True,
        }
    ]
    qlr, images, label, kw = fake_lib["convert"]
    assert qlr.model == "QL-800" and qlr.exception_on_warning
    assert label == "62"
    assert kw["dither"] is False and kw["red"] is False and kw["cut"] is True
    assert images[0].width == WIDTH_PX and images[0].mode == "L"
    assert p.last_status["outcome"] == "printed"


def test_ql_send_exception_wrapped(fake_lib):
    def send(**kw):
        raise ValueError("Device not found")

    p = BrotherQLPrinter(send=send)
    with pytest.raises(PrinterError, match="Device not found"):
        p.print_image(card())


def test_ql_error_status_raises(fake_lib):
    def send(**kw):
        return {"outcome": "error", "did_print": False, "printer_state": {"errors": ["No media when printing"]}}

    with pytest.raises(PrinterError, match="No media"):
        BrotherQLPrinter(send=send).print_image(card())


def test_ql_unconfirmed_print_only_warns(fake_lib, caplog):
    p = BrotherQLPrinter(send=lambda **kw: {"outcome": "sent", "did_print": False})
    p.print_image(card())
    assert "did not confirm" in caplog.text


def test_ql_convert_failure_wrapped(fake_lib, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("bad raster")

    monkeypatch.setattr(sys.modules["brother_ql.conversion"], "convert", boom)
    sent = []
    with pytest.raises(PrinterError, match="bad raster"):
        BrotherQLPrinter(send=lambda **kw: sent.append(kw)).print_image(card())
    assert sent == []


def test_ql_wrong_width_rejected_before_send(fake_lib):
    sent = []
    with pytest.raises(PrinterError):
        BrotherQLPrinter(send=lambda **kw: sent.append(kw)).print_image(Image.new("1", (100, 100)))
    assert sent == []


def test_ql_missing_library(monkeypatch):
    for name in list(sys.modules):
        if name == "brother_ql" or name.startswith("brother_ql."):
            monkeypatch.delitem(sys.modules, name)
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("brother_ql"):
            raise ImportError("No module named 'brother_ql'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(PrinterError, match="pip install brother_ql"):
        BrotherQLPrinter(send=lambda **kw: None).print_image(card())


def test_ql_prints_are_serialised(fake_lib):
    active = []
    overlap = []

    def send(**kw):
        active.append(1)
        if len(active) > 1:
            overlap.append(True)
        threading.Event().wait(0.01)
        active.pop()
        return {"outcome": "printed", "did_print": True}

    p = BrotherQLPrinter(send=send)
    threads = [threading.Thread(target=p.print_image, args=(card(),)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlap


# ---------------------------------------------- real library, if installed ---


def test_real_convert_produces_raster_bytes():
    pytest.importorskip("brother_ql.conversion")
    from zozobox.printer import render_coloring_page, render_letter_card, render_weather_card

    p = BrotherQLPrinter()
    for img in (
        card(),
        render_mood_card(None),
        render_weather_card("Rain", "rain", 10, 5),
        render_letter_card("b", ["ball"], None),
        render_coloring_page(Image.new("RGB", (400, 300), "white")),
    ):
        data = p.instructions(img)
        assert isinstance(data, bytes)
        # Raster lines: one 'g' command (0x67) per pixel row, 90 bytes each on the QL-800.
        assert data.count(b"\x67\x00\x5a") >= img.height
        assert data.endswith(b"\x1a")  # print with feed


def test_real_library_knows_ql800_and_62mm():
    pytest.importorskip("brother_ql")
    from brother_ql.labels import ALL_LABELS
    from brother_ql.models import ALL_MODELS

    assert "QL-800" in [m.identifier for m in ALL_MODELS]
    label = next(lab for lab in ALL_LABELS if lab.identifier == "62")
    assert label.dots_printable[0] == WIDTH_PX


# ------------------------------------------- FakePrinter save failures ---


def test_fake_printer_save_failure_raises_printer_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    p = FakePrinter(blocker / "sub")  # mkdir fails: parent is a file
    with pytest.raises(PrinterError, match="could not save"):
        p.print_image(card())
    assert p.count == 0 and p.paths == []
    p.out_dir = tmp_path / "ok"
    p.print_image(card())
    assert p.count == 1 and p.paths[0].name.startswith("print-0001-")


# ------------------------------- send_instructions with a fake USB backend ---


class FakeUSBTimeout(OSError):
    """Shaped like usb.core.USBTimeoutError (errno 110)."""

    def __init__(self):
        super().__init__(errno.ETIMEDOUT, "Operation timed out")


class FakeUSBError(OSError):
    pass


class FakeBackend:
    """Stands in for brother_ql's BrotherQLBackendPyUSB."""

    instances: list = []

    def __init__(self, identifier, *, open_exc=None, write_exc=None, reads=()):
        if open_exc:
            raise open_exc
        self.identifier = identifier
        self.write_exc = write_exc
        self.reads = list(reads)
        self.written = b""
        self.disposed = 0
        FakeBackend.instances.append(self)

    def write(self, data):
        if self.write_exc:
            raise self.write_exc
        self.written += data

    def read(self, length=32):
        item = self.reads.pop(0) if self.reads else FakeUSBTimeout()
        if isinstance(item, BaseException):
            raise item
        return item

    def dispose(self):
        self.disposed += 1


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.5)  # make the 10 s poll loop finish fast


def make_backend(**kw):
    FakeBackend.instances = []
    return lambda ident: FakeBackend(ident, **kw)


def status_bytes(status_type, phase_type=0x00, errors=(0, 0)):
    pytest.importorskip("brother_ql.reader")
    st = {"Printing completed": 0x01, "Error occurred": 0x02, "Phase change": 0x06}[status_type]
    b = bytearray(32)
    b[0:4] = b"\x80\x20\x42\x30"
    b[4] = 0x38  # QL-800 series code is not validated by interpret_response
    b[8], b[9] = errors
    b[10] = 62
    b[11] = 0x0A  # continuous
    b[18] = st
    b[19] = phase_type
    return bytes(b)


def ql(backend, clock=None, **cfg):
    clock = clock or FakeClock()
    return BrotherQLPrinter(QLConfig(**cfg), backend_class=backend, clock=clock, sleep=clock.sleep)


@pytest.fixture
def real_reader():
    pytest.importorskip("brother_ql.reader")


def test_read_timeout_after_successful_write_only_warns(real_reader, caplog):
    # Regression: brother_ql's helpers.send let USBTimeoutError escape from read()
    # after the bytes were out, so a card that printed was reported as failed.
    p = ql(make_backend(reads=[FakeUSBTimeout()] * 100))
    p.print_image(card())  # must not raise
    (be,) = FakeBackend.instances
    assert be.written == p.instructions(card()) and len(be.written) > 10_000
    assert be.disposed == 1
    assert p.last_status["instructions_sent"] and p.last_status["outcome"] == "sent"
    assert "did not confirm" in caplog.text


def test_real_usb_timeout_error_is_treated_as_no_data(real_reader, caplog):
    usb_core = pytest.importorskip("usb.core")
    p = ql(make_backend(reads=[usb_core.USBTimeoutError("timeout", 110, 110)] * 3))
    p.print_image(card())
    assert FakeBackend.instances[0].disposed == 1


def test_other_read_error_after_write_only_warns(real_reader, caplog):
    p = ql(make_backend(reads=[FakeUSBError(errno.EPIPE, "Pipe error")]))
    p.print_image(card())
    assert FakeBackend.instances[0].disposed == 1
    assert "status read failed" in caplog.text


def test_write_failure_raises_and_disposes(real_reader):
    p = ql(make_backend(write_exc=FakeUSBError(errno.EIO, "I/O error")))
    with pytest.raises(PrinterError, match="I/O error") as ei:
        p.print_image(card())
    assert FakeBackend.instances[0].disposed == 1
    # No chained cause/context: the original traceback would pin the device handle.
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_device_not_found_raises(real_reader):
    p = ql(make_backend(open_exc=ValueError("Device not found")))
    with pytest.raises(PrinterError, match="Device not found"):
        p.print_image(card())


def test_printed_status_read_back(real_reader, caplog):
    reads = [b"", status_bytes("Printing completed"), status_bytes("Phase change", 0x00)]
    p = ql(make_backend(reads=reads))
    p.print_image(card())
    assert p.last_status["did_print"] and p.last_status["ready_for_next_job"]
    assert p.last_status["outcome"] == "printed"
    assert "did not confirm" not in caplog.text
    assert FakeBackend.instances[0].disposed == 1


def test_error_status_read_back_raises_and_disposes(real_reader):
    p = ql(make_backend(reads=[status_bytes("Error occurred", errors=(0x01, 0))]))
    with pytest.raises(PrinterError, match="printer reported an error"):
        p.print_image(card())
    assert FakeBackend.instances[0].disposed == 1


def test_non_blocking_skips_read(real_reader):
    p = ql(make_backend(reads=[FakeUSBError(errno.EPIPE, "never read")]), blocking=False)
    p.print_image(card())
    be = FakeBackend.instances[0]
    assert be.reads and be.disposed == 1


def test_default_path_uses_brother_ql_backend_factory(real_reader, monkeypatch):
    # The un-injected path resolves the class via brother_ql.backends.backend_factory.
    import brother_ql.backends as bqb

    backend = make_backend(reads=[FakeUSBTimeout()])
    seen = []

    def factory(name):
        seen.append(name)
        return {"backend_class": backend, "list_available_devices": lambda: []}

    monkeypatch.setattr(bqb, "backend_factory", factory)
    clock = FakeClock()
    p = BrotherQLPrinter(clock=clock, sleep=clock.sleep)
    p.print_image(card())
    assert seen == ["pyusb"]
    assert FakeBackend.instances[0].identifier == DEFAULT_IDENTIFIER
    assert FakeBackend.instances[0].disposed == 1
