"""Regression tests for review findings on zozobox.audio."""

import logging
import os
import subprocess
import threading
import time

import pytest

from zozobox.audio import (
    AlsaAudioIn, AlsaAudioOut, AudioConfig, CardConfig, FakeAudioIn, FakeAudioOut, FakeProc,
    FakeTake, MicConfig, Output, create_alsa_audio, volume_percent,
)
from zozobox.audio.alsa import ArecordSource

CFG = AudioConfig(
    handset=CardConfig(card="Phone", mixer_control="PCM", max_fraction=0.5),
    speaker=CardConfig(card="Spk", mixer_control="Speaker", max_fraction=0.8),
    mic=MicConfig(card="Phone", mixer_control=None, rate=16000),
)


class Spawner:
    def __init__(self):
        self.procs, self.kwargs, self.fail = [], [], False

    def __call__(self, argv, **kw):
        if self.fail:
            raise FileNotFoundError("aplay")
        p = FakeProc(argv)
        self.procs.append(p)
        self.kwargs.append(kw)
        return p


class Runner:
    """amixer stand-in; `fail` fails every command, `fail_cards` only those cards."""

    def __init__(self):
        self.cmds, self.fail, self.fail_cards, self.hook = [], False, set(), None

    def __call__(self, argv):
        argv = list(argv)
        if self.hook:
            self.hook(argv)
        self.cmds.append(argv)
        if self.fail or argv[4] in self.fail_cards:
            raise subprocess.CalledProcessError(1, argv)

    def for_card(self, card):
        return [c for c in self.cmds if c[4] == card]


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFF")
    return p


def make(**kw):
    sp, run = Spawner(), Runner()
    return AlsaAudioOut(CFG, spawn=sp, run=run, **kw), sp, run


# --- 1. hearing-safe ceiling re-applied / enforced ------------------------

def test_failed_boot_volume_is_retried_before_play(wav):
    sp, run = Spawner(), Runner()
    run.fail = True  # card not enumerated yet at boot
    out = AlsaAudioOut(CFG, spawn=sp, run=run)
    assert out.ceiling_ok == {"handset": False, "speaker": False}
    run.fail = False
    run.cmds.clear()
    pb = out.play(wav)
    assert run.cmds == [
        ["amixer", "-q", "-M", "-c", "Phone", "sset", "PCM", f"{volume_percent(CFG.default_volume, 0.5)}%"],
        ["amixer", "-q", "-M", "-c", "Spk", "sset", "Speaker", f"{volume_percent(CFG.default_volume, 0.8)}%"],
    ]
    assert len(sp.procs) == 2 and not pb.failed
    assert out.ceiling_ok == {"handset": True, "speaker": True}
    run.cmds.clear()
    out.play(wav)
    assert run.cmds == []  # no re-apply while known good


def test_refuses_card_whose_ceiling_cannot_be_applied(wav, caplog):
    sp, run = Spawner(), Runner()
    run.fail_cards = {"Phone"}
    out = AlsaAudioOut(CFG, spawn=sp, run=run)
    pb = out.play(wav)
    devs = [p.argv[p.argv.index("-D") + 1] for p in sp.procs]
    assert devs == ["plughw:CARD=Spk,DEV=0"]  # handset uncapped: not played
    assert pb.failed
    assert "volume ceiling not applied" in caplog.text


def test_refuse_uncapped_can_be_disabled(wav):
    sp, run = Spawner(), Runner()
    run.fail = True
    out = AlsaAudioOut(CFG, spawn=sp, run=run, refuse_uncapped=False)
    out.play(wav)
    assert len(sp.procs) == 2


def test_spawn_failure_marks_ceiling_for_reapply(wav):
    out, sp, run = make()
    sp.fail = True
    assert out.play(wav).failed
    assert out.ceiling_ok == {"handset": False, "speaker": False}
    sp.fail = False
    run.cmds.clear()
    out.play(wav)
    assert len(run.cmds) == 2 and len(sp.procs) == 2


def test_aplay_nonzero_exit_logged_failed_and_reapplies(wav, caplog):
    out, sp, run = make()
    pb = out.play(wav)
    handset, speaker = sp.procs
    handset.finish(1)  # "No such device"
    speaker.finish(0)
    assert pb.done and pb.failed
    run.cmds.clear()
    with caplog.at_level(logging.WARNING):
        out.play(wav)
    assert "aplay on handset (Phone) exited with 1" in caplog.text
    assert [c[4] for c in run.cmds] == ["Phone"]  # only the dropped card re-applied


def test_stopped_or_muted_playback_is_not_a_failure(wav, caplog):
    out, sp, run = make()
    pb = out.play(wav)
    with out.speaker_muted():
        pass
    pb.stop()
    out.play(wav)
    assert not pb.failed
    assert "exited with" not in caplog.text
    assert out.ceiling_ok == {"handset": True, "speaker": True}


def test_aplay_stderr_captured_and_logged(tmp_path, wav, caplog):
    def spawn(argv, **kw):
        assert kw["stderr"] is not subprocess.DEVNULL
        return subprocess.Popen(["sh", "-c", "echo 'No such device' >&2; exit 1"], **kw)

    out = AlsaAudioOut(CFG, spawn=spawn, run=Runner())
    pb = out.play(wav, Output.SPEAKER)
    assert pb.wait(5) and pb.failed
    with caplog.at_level(logging.WARNING):
        assert out.active == []
    assert "aplay on speaker (Spk) exited with 1: No such device" in caplog.text


# --- 2. recorder must mute the speaker ------------------------------------

def test_alsa_in_requires_muter():
    with pytest.raises(TypeError):
        AlsaAudioIn(CFG, run=Runner())  # type: ignore[call-arg]


def test_alsa_in_explicit_none_muter_warns(caplog):
    AlsaAudioIn(CFG, muter=None, run=Runner())
    assert "will not be muted" in caplog.text


def test_create_alsa_audio_wires_muter(tmp_path):
    run = Runner()
    out, ain = create_alsa_audio(CFG, spawn=Spawner(), run=run)
    assert ain.muter is out
    seen = []

    class Src:
        def read(self, n):
            seen.append(out.speaker_is_muted)
            return b""

        def close(self):
            pass

    ain._source_factory = lambda m: Src()
    ain.record(tmp_path / "x.wav", 1, threading.Event())
    assert seen == [True] and not out.speaker_is_muted
    assert ["amixer", "-q", "-M", "-c", "Spk", "sset", "Speaker", "0%"] in run.cmds


# --- 3. suppressed sounds while muted --------------------------------------

def test_muted_speaker_play_is_marked_suppressed(wav):
    out, sp, _ = make()
    assert not out.play(wav, Output.SPEAKER).suppressed
    with out.speaker_muted():
        pb = out.play(wav, Output.SPEAKER)
    assert pb.done and pb.suppressed and not pb.failed


def test_fake_muted_speaker_play_is_marked_suppressed(tmp_path):
    out = FakeAudioOut()
    with out.speaker_muted():
        assert out.play(tmp_path / "click.wav", Output.SPEAKER).suppressed
    assert not out.play(tmp_path / "click.wav", Output.SPEAKER).suppressed


# --- 5/6. FakeAudioIn matches the real recorder ----------------------------

@pytest.mark.parametrize("max_s", [0, -1])
def test_fake_in_wait_for_stop_with_zero_cap_returns(tmp_path, max_s):
    ain = FakeAudioIn([FakeTake(1, wait_for_stop=True)])
    res = []
    t = threading.Thread(target=lambda: res.append(ain.record(tmp_path / "x.wav", max_s, threading.Event())))
    t.start()
    t.join(2)
    assert not t.is_alive()
    assert res[0].duration_s == 0 and res[0].silent


def test_fake_in_stop_already_set_gives_empty_silent(tmp_path):
    stop = threading.Event()
    stop.set()
    r = FakeAudioIn().record(tmp_path / "x.wav", 30, stop)
    assert r.duration_s == 0 and r.silent


def test_fake_in_short_take_is_silent(tmp_path):
    ain = FakeAudioIn([FakeTake(0.1), FakeTake(0.5)])
    assert ain.record(tmp_path / "a.wav", 30, threading.Event()).silent
    assert not ain.record(tmp_path / "b.wav", 30, threading.Event()).silent


# --- 7. FakeAudioOut mute and missing files --------------------------------

def test_fake_out_mute_stops_running_speaker_sounds(tmp_path):
    out = FakeAudioOut()
    chime = out.play(tmp_path / "chime.wav", Output.SPEAKER)
    speech = out.play(tmp_path / "speech.wav", Output.BOTH)
    with out.speaker_muted():
        assert chime.done and not speech.done
        assert not chime.failed


def test_fake_out_check_files(tmp_path):
    out = FakeAudioOut(check_files=True)
    pb = out.play(tmp_path / "missing.wav")
    assert pb.done and pb.failed and out.plays == []
    assert out.failed_plays == [(tmp_path / "missing.wav", Output.BOTH)]
    real = tmp_path / "ok.wav"
    real.write_bytes(b"RIFF")
    assert not out.play(real).failed


# --- 8. volume apply is atomic ---------------------------------------------

def test_set_volume_during_mute_restore_wins(wav):
    out, _, run = make()
    threads = []

    def hook(argv):
        # While the mute-exit restore is mid-run, the dashboard sets 0.2.
        if not threads and argv[4] == "Spk" and argv[-1] != "0%":
            t = threading.Thread(target=out.set_volume, args=(0.2,))
            threads.append(t)
            t.start()
            t.join(0.2)

    with out.speaker_muted():
        run.hook = hook
    threads[0].join(2)
    assert out.volume == 0.2
    assert run.for_card("Spk")[-1][-1] == f"{volume_percent(0.2, 0.8)}%"


# --- 9. watchdog kills arecord without closing the reader's pipe -----------

def test_watchdog_stall_recovery_logs_no_traceback(tmp_path, caplog):
    def spawn(argv, **kw):
        return subprocess.Popen(["sleep", "30"], **kw)

    ain = AlsaAudioIn(CFG, muter=FakeAudioOut(), run=Runner(), stall_grace_s=0.1,
                      source_factory=lambda m: ArecordSource(m, spawn=spawn))
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    t0 = time.monotonic()
    r = ain.record(tmp_path / "x.wav", 30, stop)
    assert time.monotonic() - t0 < 3
    assert r.duration_s == 0
    assert "recording source failed" not in caplog.text
    assert "Traceback" not in caplog.text


class StuckProc:
    """A process that ignores terminate/kill (e.g. stuck in a USB D-state)."""

    def __init__(self):
        r, self._w = os.pipe()
        self.stdout = os.fdopen(r, "rb", buffering=0)

    def poll(self):
        return None

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("arecord", timeout)

    def terminate(self):
        pass

    def kill(self):
        pass

    def cleanup(self):
        os.close(self._w)


def test_unkillable_arecord_still_lets_record_return(tmp_path):
    procs = []

    def spawn(argv, **kw):
        procs.append(StuckProc())
        return procs[-1]

    out = FakeAudioOut()
    ain = AlsaAudioIn(CFG, muter=out, run=Runner(), stall_grace_s=0.1,
                      source_factory=lambda m: ArecordSource(m, spawn=spawn))
    t0 = time.monotonic()
    ain.record(tmp_path / "x.wav", 0.2, threading.Event())
    assert time.monotonic() - t0 < 3
    assert not out.speaker_is_muted
    procs[0].cleanup()
