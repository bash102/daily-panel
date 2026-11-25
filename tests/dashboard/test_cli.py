import sys
import types

import pytest

from zozobox.dashboard import __main__ as cli
from zozobox.printer import FakePrinter, PrinterError


def test_make_printer_variants(tmp_path):
    assert isinstance(cli.make_printer("fake"), FakePrinter)
    fp = cli.make_printer(f"fake:{tmp_path}")
    assert fp.out_dir == tmp_path
    with pytest.raises(PrinterError):
        cli.make_printer("none").print_image(None)
    mod = types.ModuleType("dash_test_printer_mod")
    mod.make = lambda: "custom"
    sys.modules["dash_test_printer_mod"] = mod
    try:
        assert cli.make_printer("dash_test_printer_mod:make") == "custom"
    finally:
        del sys.modules["dash_test_printer_mod"]
    with pytest.raises(ValueError):
        cli.make_printer("bogus")


def test_main_runs_uvicorn_with_args(tmp_path, monkeypatch):
    import uvicorn

    seen = {}

    def fake_run(app, **kw):
        seen["app"] = app
        seen.update(kw)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    rc = cli.main(["--data-dir", str(tmp_path / "d"), "--host", "127.0.0.1", "--port", "8099",
                   "--printer", "fake"])
    assert rc == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8099
    assert seen["app"].state.store.data_dir == tmp_path / "d"
    assert (tmp_path / "d" / "zozobox.db").exists()


def test_main_bad_printer(tmp_path, capsys):
    assert cli.main(["--data-dir", str(tmp_path), "--printer", "bogus"]) == 2


def test_default_printer_is_none_not_unlocked_brother(tmp_path, monkeypatch):
    """The engine drives the same USB printer; the dashboard must not open it
    directly unless asked (M10's locked printer plugs in via pkg.mod:func)."""
    import uvicorn

    seen = {}
    monkeypatch.delenv("ZOZOBOX_DASHBOARD_PRINTER", raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.setdefault("app", app))
    assert cli.main(["--data-dir", str(tmp_path / "d")]) == 0
    assert isinstance(seen["app"].state.printer, cli.NoPrinter)
