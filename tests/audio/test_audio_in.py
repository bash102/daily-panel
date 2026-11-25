import math
import struct
import subprocess
import threading
import time
import wave

import pytest

from zozobox.audio import (
    AlsaAudioIn, AudioConfig, FakeAudioOut, MicConfig, Recording, record_pcm, rms_dbfs,
)
from zozobox.audio.alsa import ArecordSource, arecord_cmd

RATE = 16000
CHUNK = int(RATE * 0.05) * 2  # bytes in 50 ms mono


def tone(seconds, amp=0.3, freq=300):
    n = int(seconds * RATE)
    return b"".join(
        struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / RATE))) for i in range(n)
    )


def silence(seconds):
    return b"\x00\x00" * int(seconds * RATE)


class ListSource:
    """Feeds fixed bytes in pieces; optional per-read hook and error."""

    def __init__(self, data=b"", piece=None, fail_after=None, on_read=None):
        self.data = data
        self.pos = 0
        self.piece = piece
        self.reads = 0
        self.fail_after = fail_after
        self.on_read = on_read
        self.closed = False

    def read(self, n):
        self.reads += 1
        if self.on_read:
            self.on_read(self.reads)
        if self.fail_after is not None and self.reads > self.fail_after:
            raise OSError("device gone")
        n = min(n, self.piece or n)
        out = self.data[self.pos : self.pos + n]
        self.pos += len(out)
        return out

    def close(self):
        self.closed = True


def rec(src, tmp_path, max_s=10.0, stop=None, **kw):
    kw.setdefault("rate", RATE)
    return record_pcm(src, tmp_path / "r.wav", max_s, stop or threading.Event(), **kw)


def wav_frames(path):
    with wave.open(str(path), "rb") as w:
        assert w.getsampwidth() == 2
        return w.getnframes(), w.getframerate(), w.getnchannels()


def test_rms_dbfs():
    assert rms_dbfs(b"") == -math.inf
    assert rms_dbfs(silence(0.1)) == -math.inf
    full = struct.pack("<h", 32767) * 100
    assert rms_dbfs(full) == pytest.approx(0, abs=0.01)
    assert rms_dbfs(tone(0.1, amp=0.5)) == pytest.approx(20 * math.log10(0.5 / math.sqrt(2)), abs=0.2)
    assert rms_dbfs(b"\x01") == -math.inf  # odd byte ignored


def test_speech_recording_written_until_eof(tmp_path):
    src = ListSource(silence(0.5) + tone(1.0) + silence(0.5))
    r = rec(src, tmp_path)
    assert isinstance(r, Recording)
    assert r.duration_s == pytest.approx(2.0)
    assert not r.silent
    assert wav_frames(r.path) == (2 * RATE, RATE, 1)
    assert src.closed


def test_silent_recording_flagged(tmp_path):
    r = rec(ListSource(silence(2.0)), tmp_path)
    assert r.silent and r.duration_s == pytest.approx(2.0)


def test_quiet_noise_below_threshold_is_silent(tmp_path):
    r = rec(ListSource(tone(2.0, amp=0.003)), tmp_path)  # ~ -53 dBFS
    assert r.silent


def test_brief_blip_shorter_than_min_speech_is_silent(tmp_path):
    r = rec(ListSource(silence(1) + tone(0.1) + silence(1)), tmp_path, min_speech_s=0.3)
    assert r.silent


def test_threshold_configurable(tmp_path):
    r = rec(ListSource(tone(1.0, amp=0.003)), tmp_path, speech_dbfs=-60)
    assert not r.silent


def test_max_s_caps_exactly(tmp_path):
    src = ListSource(tone(5.0))
    r = rec(src, tmp_path, max_s=1.23)
    assert r.duration_s == pytest.approx(1.23, abs=1 / RATE)
    assert wav_frames(r.path)[0] == round(1.23 * RATE)
    assert src.closed


def test_stop_event_ends_early(tmp_path):
    stop = threading.Event()
    src = ListSource(tone(5.0), on_read=lambda n: n == 10 and stop.set())
    r = rec(src, tmp_path, stop=stop)
    assert r.duration_s == pytest.approx(10 * 0.05)
    assert not r.silent


def test_stop_already_set_gives_empty_silent_wav(tmp_path):
    stop = threading.Event()
    stop.set()
    r = rec(ListSource(tone(1)), tmp_path, stop=stop)
    assert r.duration_s == 0 and r.silent
    assert wav_frames(r.path)[0] == 0


def test_odd_sized_reads_are_reassembled(tmp_path):
    data = tone(1.0)
    r = rec(ListSource(data, piece=333), tmp_path)
    assert r.duration_s == pytest.approx(1.0)
    with wave.open(str(r.path), "rb") as w:
        assert w.readframes(w.getnframes()) == data


def test_stereo(tmp_path):
    mono = tone(0.5)
    stereo = b"".join(mono[i : i + 2] * 2 for i in range(0, len(mono), 2))
    r = rec(ListSource(stereo), tmp_path, channels=2)
    assert r.duration_s == pytest.approx(0.5)
    assert wav_frames(r.path) == (RATE // 2, RATE, 2)


def test_source_error_keeps_partial(tmp_path, caplog):
    src = ListSource(tone(5.0), fail_after=4)
    r = rec(src, tmp_path)
    assert r.duration_s == pytest.approx(0.2)
    assert src.closed
    assert "keeping partial" in caplog.text


def test_creates_parent_dirs(tmp_path):
    r = record_pcm(ListSource(tone(0.1)), tmp_path / "a" / "b" / "x.wav", 1, threading.Event(), rate=RATE)
    assert r.path.exists()


# --- AlsaAudioIn ---------------------------------------------------------

MIC = MicConfig(card="Phone", mixer_control="Mic", capture_fraction=0.75, rate=RATE)
CFG = AudioConfig(mic=MIC)


class Runner:
    def __init__(self):
        self.cmds = []

    def __call__(self, argv):
        self.cmds.append(list(argv))


def test_arecord_cmd():
    assert arecord_cmd(MIC) == [
        "arecord", "-q", "-D", "plughw:CARD=Phone,DEV=0", "-t", "raw", "-f", "S16_LE",
        "-r", "16000", "-c", "1",
    ]


def test_mic_gain_set_on_start():
    run = Runner()
    AlsaAudioIn(CFG, muter=FakeAudioOut(), source_factory=lambda m: ListSource(), run=run)
    assert run.cmds == [["amixer", "-q", "-M", "-c", "Phone", "sset", "Mic", "75%", "cap"]]


def test_alsa_in_records_and_mutes_speaker(tmp_path):
    out = FakeAudioOut()
    seen = []
    src = ListSource(tone(1.0), on_read=lambda n: seen.append(out.speaker_is_muted))
    ain = AlsaAudioIn(CFG, muter=out, source_factory=lambda m: src, run=Runner())
    r = ain.record(tmp_path / "x.wav", 30, threading.Event())
    assert r.duration_s == pytest.approx(1.0) and not r.silent
    assert seen and all(seen)
    assert not out.speaker_is_muted


def test_alsa_in_unmutes_on_error(tmp_path):
    out = FakeAudioOut()

    def bad(m):
        raise FileNotFoundError("arecord")

    ain = AlsaAudioIn(CFG, muter=out, source_factory=bad, run=Runner())
    r = ain.record(tmp_path / "x.wav", 30, threading.Event())
    assert r.silent and r.duration_s == 0
    assert not out.speaker_is_muted


class BlockingSource:
    """read() blocks forever until close(), like a hung device."""

    def __init__(self):
        self._closed = threading.Event()

    def read(self, n):
        self._closed.wait()
        return b""

    def close(self):
        self._closed.set()


def test_watchdog_unblocks_stalled_device_on_stop(tmp_path):
    ain = AlsaAudioIn(CFG, muter=FakeAudioOut(), source_factory=lambda m: BlockingSource(), run=Runner(), stall_grace_s=0.1)
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    t0 = time.monotonic()
    r = ain.record(tmp_path / "x.wav", 30, stop)
    assert time.monotonic() - t0 < 2
    assert r.duration_s == 0 and r.silent


def test_watchdog_unblocks_stalled_device_at_max(tmp_path):
    ain = AlsaAudioIn(CFG, muter=FakeAudioOut(), source_factory=lambda m: BlockingSource(), run=Runner(), stall_grace_s=0.1)
    t0 = time.monotonic()
    ain.record(tmp_path / "x.wav", 0.2, threading.Event())
    assert time.monotonic() - t0 < 2


def test_arecord_source_with_real_process(tmp_path):
    # `cat` of a file stands in for arecord's raw stdout.
    raw = tmp_path / "in.raw"
    raw.write_bytes(tone(0.5))

    def spawn(argv, **kw):
        assert argv[0] == "arecord"
        return subprocess.Popen(["cat", str(raw)], **kw)

    src = ArecordSource(MIC, spawn=spawn)
    r = record_pcm(src, tmp_path / "x.wav", 10, threading.Event(), rate=RATE)
    assert r.duration_s == pytest.approx(0.5)
    assert src._proc.returncode is not None


def test_arecord_source_close_kills_live_process(tmp_path):
    def spawn(argv, **kw):
        return subprocess.Popen(["sleep", "30"], **kw)

    src = ArecordSource(MIC, spawn=spawn)
    t = threading.Timer(0.1, src.close)
    t.start()
    assert src.read(100) == b""
    t.join(2)
    assert src._proc.returncode is not None
    src.close()  # idempotent
