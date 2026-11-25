from zozobox.dashboard import __main__ as cli
from zozobox.printer import LockedPrinter, SupervisedPrinter


def test_brother_printer_is_behind_the_shared_lock(tmp_path):
    for spec in ("brother", "locked"):
        p = cli.make_printer(spec, str(tmp_path))
        assert isinstance(p, LockedPrinter) and isinstance(p.inner, SupervisedPrinter)
        assert p.path == tmp_path / ".printer.lock"  # same file the engine locks


def test_main_passes_data_dir_to_the_locked_printer(tmp_path, monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.setdefault("app", app))
    assert cli.main(["--data-dir", str(tmp_path / "d"), "--printer", "brother"]) == 0
    printer = seen["app"].state.printer
    assert isinstance(printer, LockedPrinter) and printer.path == tmp_path / "d" / ".printer.lock"


def test_brother_printer_uses_box_toml_printer_table(tmp_path, monkeypatch):
    import zozobox.printer as zp

    cfg = tmp_path / "box.toml"
    cfg.write_text('[printer]\nidentifier = "usb://0x04f9:0x2042"\nlabel = "29"\n'
                   '[app]\nprinter_lock_timeout_s = 12.0\n')
    seen = {}

    def fake(data_dir, qlcfg=None, **kw):
        seen.update(data_dir=data_dir, cfg=qlcfg, **kw)
        return "printer"

    monkeypatch.setattr(zp, "make_locked_brother", fake)
    monkeypatch.setenv("ZOZOBOX_CONFIG", str(cfg))
    assert cli.make_printer("brother", str(tmp_path)) == "printer"
    assert seen["cfg"].identifier == "usb://0x04f9:0x2042" and seen["cfg"].label == "29"
    assert seen["lock_timeout_s"] == 12.0
    # an explicit --config wins over the env
    other = tmp_path / "other.toml"
    other.write_text('[printer]\nlabel = "62red"\n')
    cli.make_printer("brother", str(tmp_path), str(other))
    assert seen["cfg"].label == "62red"


def test_bad_box_toml_stops_the_dashboard_with_exit_2(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "box.toml"
    cfg.write_text("[printer]\nidentifer = 'typo'\n")
    monkeypatch.setenv("ZOZOBOX_CONFIG", str(cfg))
    assert cli.main(["--data-dir", str(tmp_path / "d"), "--printer", "brother"]) == 2
    assert "identifer" in capsys.readouterr().err


def test_setup_token_lifecycle(tmp_path):
    from zozobox.dashboard import AUTH_FILE_NAME

    tok = cli.setup_token(tmp_path)
    f = tmp_path / cli.SETUP_TOKEN_FILE
    assert tok and len(tok) >= 16 and f.read_text().strip() == tok
    assert (f.stat().st_mode & 0o777) == 0o600
    assert cli.setup_token(tmp_path) == tok  # stable across restarts
    (tmp_path / AUTH_FILE_NAME).write_text("{}")
    assert cli.setup_token(tmp_path) is None and not f.exists()


def test_main_gates_setup_with_a_token(tmp_path, monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.setdefault("app", app))
    assert cli.main(["--data-dir", str(tmp_path), "--printer", "none"]) == 0
    assert (tmp_path / cli.SETUP_TOKEN_FILE).exists()
    seen.clear()
    assert cli.main(["--data-dir", str(tmp_path / "dev"), "--open-setup"]) == 0
    assert not (tmp_path / "dev" / cli.SETUP_TOKEN_FILE).exists()
