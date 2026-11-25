import wave

from zozobox import assets


def test_chimes_are_deterministic_and_valid(tmp_path):
    a = assets.ensure_chimes(tmp_path / "a")
    b = assets.ensure_chimes(tmp_path / "b")
    for name in (assets.CLICK, assets.BEEP):
        assert a[name].read_bytes() == b[name].read_bytes()
        with wave.open(str(a[name])) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (48000, 1, 2)
            assert w.getnframes() > 0
    with wave.open(str(a[assets.CLICK])) as w:
        assert w.getnframes() / 48000 < 0.05
    with wave.open(str(a[assets.BEEP])) as w:
        assert 0.2 < w.getnframes() / 48000 < 0.5


def test_ensure_repairs_a_changed_file(tmp_path):
    p = assets.ensure_chimes(tmp_path)[assets.CLICK]
    p.write_bytes(b"junk")
    mtime_beep = (tmp_path / assets.BEEP).stat().st_mtime_ns
    assets.ensure_chimes(tmp_path)
    assert p.read_bytes() == assets.click_wav()
    assert (tmp_path / assets.BEEP).stat().st_mtime_ns == mtime_beep  # untouched


def test_cli(tmp_path, capsys):
    assert assets.main([str(tmp_path)]) == 0
    assert (tmp_path / "click.wav").exists() and (tmp_path / "beep.wav").exists()
    assert assets.main([]) == 2
