import shutil
import struct
import wave

import pytest

from zozobox import import_clip as ic
from zozobox.engine import DirSoundboard


def make_wav(path, seconds=1.0, rate=8000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 1000) * int(seconds * rate))
    return path


def copy_norm(src, dst):
    shutil.copyfile(src, dst)


def test_import_normalizes_and_credits(tmp_path):
    src = make_wav(tmp_path / "Happy Cow!.wav")
    sb = tmp_path / "sb"
    dest = ic.import_clip(src, sb, source="https://freesound.org/s/1/", license="CC0",
                          normalize=copy_norm)
    assert dest == sb / "imported" / "happy_cow.wav" and dest.is_file()
    lines = (sb / "CREDITS.tsv").read_text().splitlines()
    assert lines[0].startswith("file\tsource\tlicense")
    assert lines[1].startswith("imported/happy_cow.wav\thttps://freesound.org/s/1/\tCC0")
    assert DirSoundboard(sb).clips() == [dest]


def test_refuses_long_existing_and_missing(tmp_path):
    sb = tmp_path / "sb"
    long = make_wav(tmp_path / "long.wav", seconds=3)
    with pytest.raises(ValueError, match="longer"):
        ic.import_clip(long, sb, max_s=2, normalize=copy_norm)
    assert not list((sb / "imported").iterdir())  # no temp left behind
    ok = make_wav(tmp_path / "ok.wav")
    ic.import_clip(ok, sb, normalize=copy_norm)
    with pytest.raises(ValueError, match="exists"):
        ic.import_clip(ok, sb, normalize=copy_norm)
    ic.import_clip(ok, sb, normalize=copy_norm, overwrite=True)
    with pytest.raises(ValueError, match="no such file"):
        ic.import_clip(tmp_path / "nope.wav", sb, normalize=copy_norm)


def test_cli(tmp_path, capsys):
    a, b = make_wav(tmp_path / "a.wav"), make_wav(tmp_path / "b.wav", seconds=30)
    rc = ic.main([str(a), str(b), "--dir", str(tmp_path / "sb")], normalize=copy_norm)
    assert rc == 1
    out = capsys.readouterr()
    assert "added" in out.out and "skipped" in out.err


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_real_normalize(tmp_path):
    import math
    src = tmp_path / "tone.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"".join(struct.pack("<hh", v, v) for v in
                               (int(3000 * math.sin(i / 5)) for i in range(22050))))
    dest = ic.import_clip(src, tmp_path / "sb")
    with wave.open(str(dest)) as w:
        assert (w.getframerate(), w.getnchannels()) == (48000, 1)


def test_cli_default_dir_is_the_engines_soundboard_dir(tmp_path, monkeypatch, capsys):
    """box.toml [app].soundboard_dir is where the engine looks, so import there."""
    cfg = tmp_path / "box.toml"
    sb = tmp_path / "custom-sb"
    cfg.write_text(f'[app]\nsoundboard_dir = "{sb}"\n')
    monkeypatch.setenv("ZOZOBOX_CONFIG", str(cfg))
    a = make_wav(tmp_path / "a.wav")
    assert ic.main([str(a)], normalize=copy_norm) == 0
    assert (sb / "imported" / "a.wav").is_file()
    from zozobox.boxconfig import load_app_config
    assert load_app_config().soundboard_dir == sb  # the same dir the app wires
    # no soundboard_dir in box.toml -> <data dir>/soundboard, like the engine
    cfg.write_text("")
    monkeypatch.setenv("ZOZOBOX_DATA_DIR", str(tmp_path / "data"))
    assert ic.main([str(a)], normalize=copy_norm) == 0
    assert (tmp_path / "data" / "soundboard" / "imported" / "a.wav").is_file()
    cfg.write_text("[app]\nsoundboard = 1\n")
    assert ic.main([str(a)], normalize=copy_norm) == 2
    assert "config error" in capsys.readouterr().err


def test_unreadable_source_says_so(tmp_path, monkeypatch):
    src = make_wav(tmp_path / "cow.wav")
    real = ic.os.access
    monkeypatch.setattr(ic.os, "access", lambda p, m: False if str(p) == str(src) else real(p, m))
    with pytest.raises(ValueError, match="cannot read"):
        ic.import_clip(src, tmp_path / "sb", normalize=copy_norm)
