import json
import math
import shutil
import struct
import subprocess
import threading
import wave

import pytest

from zozobox.audio import AudioIn, AudioOut, FakeAudioIn, FakeAudioOut, FakeTake, NormalizeError, Output, normalize
from zozobox.audio.loudness import normalize_commands, parse_loudnorm_json


def test_fake_out_records_and_controls_playbacks(tmp_path):
    out: AudioOut = FakeAudioOut()
    a = out.play(tmp_path / "a.wav")
    b = out.play(tmp_path / "b.wav", Output.SPEAKER)
    assert [(p.path.name, p.output) for p in out.plays] == [("a.wav", Output.BOTH), ("b.wav", Output.SPEAKER)]
    assert len(out.playing) == 2
    out.plays[0].proc.finish()
    assert a.done and not b.done
    out.stop_all()
    assert b.done and out.stop_all_calls == 1
    out.set_volume(3)
    assert out.volume == 1.0 and out.volume_calls == [3]


def test_fake_out_auto_finish_and_finish_all(tmp_path):
    out = FakeAudioOut(auto_finish=True)
    assert out.play(tmp_path / "a.wav").wait(0)
    out2 = FakeAudioOut()
    pb = out2.play(tmp_path / "a.wav")
    out2.finish_all()
    assert pb.wait(1) and out2.last.path.name == "a.wav"


def test_fake_out_muted_drops_speaker_only(tmp_path):
    out = FakeAudioOut()
    with out.speaker_muted():
        pb = out.play(tmp_path / "click.wav", Output.SPEAKER)
        out.play(tmp_path / "speech.wav", Output.BOTH)
    assert pb.done
    assert [p.path.name for p in out.plays] == ["speech.wav"]
    assert out.muted_plays == [(tmp_path / "click.wav", Output.SPEAKER)]


def test_fake_in_scripted_takes(tmp_path):
    out = FakeAudioOut()
    ain: AudioIn = FakeAudioIn([FakeTake(5, False), FakeTake(3, True)], muter=out)
    r1 = ain.record(tmp_path / "1.wav", 2, threading.Event())
    r2 = ain.record(tmp_path / "2.wav", 30, threading.Event())
    r3 = ain.record(tmp_path / "3.wav", 30, threading.Event())  # default take
    assert (r1.duration_s, r1.silent) == (2, False)
    assert (r2.duration_s, r2.silent) == (3, True)
    assert r3.duration_s == 2.0
    assert ain.muted_during == [True, True, True] and not out.speaker_is_muted
    with wave.open(str(r1.path)) as w:
        assert w.getnframes() == 2 * FakeAudioIn.RATE
    assert ain.calls[0] == (tmp_path / "1.wav", 2)


def test_fake_in_wait_for_stop(tmp_path):
    ain = FakeAudioIn([FakeTake(1, wait_for_stop=True)])
    stop = threading.Event()
    done = []
    t = threading.Thread(target=lambda: done.append(ain.record(tmp_path / "x.wav", 60, stop)))
    t.start()
    t.join(0.1)
    assert t.is_alive()
    stop.set()
    t.join(2)
    assert done and done[0].duration_s == 1


def test_fake_in_zero_duration_is_silent(tmp_path):
    r = FakeAudioIn([FakeTake(0, silent=False)]).record(tmp_path / "x.wav", 30, threading.Event())
    assert r.silent


# --- loudness ------------------------------------------------------------

MEASURED = {"input_i": "-27.5", "input_tp": "-10.1", "input_lra": "3.2",
            "input_thresh": "-38.0", "target_offset": "0.4"}


def test_normalize_commands():
    p1 = normalize_commands("in.mp3", "out.wav")
    assert p1[-3:] == ["-f", "null", "-"]
    assert "aformat=channel_layouts=mono,loudnorm=I=-16.0:TP=-1.5:LRA=11.0:print_format=json" in p1
    p2 = normalize_commands("in.mp3", "out.wav", measured=MEASURED)
    af = p2[p2.index("-af") + 1]
    assert "measured_I=-27.5" in af and "linear=true" in af and "offset=0.4" in af
    assert af.startswith("aformat=channel_layouts=mono,") and af.endswith(",aresample=48000")
    assert p2[p2.index("-ar") + 1] == "48000" and p2[p2.index("-c:a") + 1] == "pcm_s16le"


def test_parse_loudnorm_json():
    err = "junk\n[Parsed_loudnorm_0 @ 0x1]\n" + json.dumps(MEASURED, indent=1) + "\nmore"
    assert parse_loudnorm_json(err)["input_i"] == "-27.5"
    with pytest.raises(NormalizeError):
        parse_loudnorm_json("nothing here")


def test_normalize_with_fake_runner(tmp_path):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[-1] != "-":
            open(argv[-1], "wb").write(b"wav")
        return json.dumps(MEASURED)

    dst = tmp_path / "sub" / "o.wav"
    m = normalize(tmp_path / "i.wav", dst, run=run)
    assert m == MEASURED and dst.read_bytes() == b"wav"
    assert len(calls) == 2
    assert not list(dst.parent.glob("*.tmp.wav"))


def test_normalize_failure_leaves_no_output(tmp_path):
    def run(argv):
        if argv[-1] == "-":
            return json.dumps(MEASURED)
        open(argv[-1], "wb").write(b"partial")
        raise NormalizeError("boom")

    dst = tmp_path / "o.wav"
    with pytest.raises(NormalizeError):
        normalize(tmp_path / "i.wav", dst, run=run)
    assert not dst.exists() and not list(tmp_path.glob("*.tmp.wav"))


def _write_tone(path, seconds=4.0, rate=44100, amp=0.05, channels=2):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            s = struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * 440 * i / rate)))
            frames += s * channels
        w.writeframes(bytes(frames))


def _measure(path):
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-af",
         "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    return float(parse_loudnorm_json(r.stderr)["input_i"])


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@needs_ffmpeg
def test_normalize_real_ffmpeg(tmp_path):
    src, dst = tmp_path / "quiet.wav", tmp_path / "out.wav"
    _write_tone(src)
    assert _measure(src) < -25
    m = normalize(src, dst)
    assert float(m["input_i"]) < -25
    with wave.open(str(dst)) as w:
        assert (w.getframerate(), w.getsampwidth(), w.getnchannels()) == (48000, 2, 1)
    assert _measure(dst) == pytest.approx(-16, abs=1.0)


@needs_ffmpeg
def test_normalize_real_ffmpeg_silence(tmp_path):
    src, dst = tmp_path / "s.wav", tmp_path / "out.wav"
    _write_tone(src, seconds=1, amp=0)
    normalize(src, dst)
    with wave.open(str(dst)) as w:
        assert w.getframerate() == 48000


def test_normalize_missing_ffmpeg(tmp_path):
    src = tmp_path / "i.wav"
    _write_tone(src, seconds=0.1)
    with pytest.raises(NormalizeError):
        normalize(src, tmp_path / "o.wav", ffmpeg="/nonexistent/ffmpeg")


@needs_ffmpeg
def test_normalize_real_ffmpeg_stereo_out(tmp_path):
    src, dst = tmp_path / "loud.wav", tmp_path / "out.wav"
    _write_tone(src, amp=0.5, channels=1, rate=22050)
    normalize(src, dst, channels=2)
    with wave.open(str(dst)) as w:
        assert (w.getframerate(), w.getnchannels()) == (48000, 2)
    assert _measure(dst) == pytest.approx(-16, abs=1.0)
