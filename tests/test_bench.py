from zozobox import bench


def test_bench_leds_fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOZOBOX_CONFIG", str(tmp_path / "none.toml"))
    assert bench.main(["leds", "--fake", "--hold", "0"]) == 0


def test_bench_print_every_card_fake(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZOZOBOX_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("ZOZOBOX_DATA_DIR", str(tmp_path / "d"))
    assert bench.main(["print", "all", "--fake"]) == 0
    out = capsys.readouterr().out
    assert all(f"printed {k}" in out for k in bench.CARDS)
    assert len(list((tmp_path / "d" / "bench-prints").glob("*.png"))) == 4


def test_bench_print_reports_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZOZOBOX_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("ZOZOBOX_DATA_DIR", str(tmp_path / "d"))
    import zozobox.printer.locked as locked
    monkeypatch.setattr(locked, "usb_probe", lambda *a: (_ for _ in ()).throw(
        locked.PrinterError("no USB device")))
    assert bench.main(["print", "mood"]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_bench_audio_fake(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZOZOBOX_CONFIG", str(tmp_path / "none.toml"))
    assert bench.main(["audio", "--fake", "--seconds", "1"]) == 0
    out = capsys.readouterr().out
    assert "silent=False" in out and "speech_dbfs=-40.0" in out


def test_bench_bad_config(tmp_path, capsys):
    bad = tmp_path / "box.toml"
    bad.write_text("nope = 1\n")
    assert bench.main(["leds", "--fake", "--config", str(bad)]) == 2
