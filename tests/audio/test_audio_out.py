import subprocess
import threading
import time
from pathlib import Path

import pytest

from zozobox.audio import AlsaAudioOut, AudioConfig, CardConfig, FakeProc, Output, Playback, volume_percent
from zozobox.audio.alsa import aplay_cmd, amixer_cmd, clamp01
from zozobox.audio.base import reap

CFG = AudioConfig(
    handset=CardConfig(card="Phone", mixer_control="PCM", max_fraction=0.5),
    speaker=CardConfig(card="Spk", mixer_control="Speaker", max_fraction=0.8),
)


class Spawner:
    def __init__(self, fail_on: str | None = None):
        self.procs: list[FakeProc] = []
        self.kwargs = []
        self.fail_on = fail_on

    def __call__(self, argv, **kw):
        if self.fail_on and any(self.fail_on in a for a in argv):
            raise FileNotFoundError("aplay")
        p = FakeProc(argv)
        self.procs.append(p)
        self.kwargs.append(kw)
        return p

    def devices(self):
        return [p.argv[p.argv.index("-D") + 1] for p in self.procs]


class Runner:
    def __init__(self, fail=False):
        self.cmds = []
        self.fail = fail

    def __call__(self, argv):
        self.cmds.append(list(argv))
        if self.fail:
            raise subprocess.CalledProcessError(1, argv)


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFF")
    return p


def make(**kw):
    sp, run = Spawner(**kw), Runner()
    return AlsaAudioOut(CFG, spawn=sp, run=run), sp, run


def test_pcm_names_and_commands(wav):
    assert CFG.handset.pcm == "plughw:CARD=Phone,DEV=0"
    assert aplay_cmd(CFG.speaker, wav) == ["aplay", "-q", "-D", "plughw:CARD=Spk,DEV=0", "--", str(wav)]
    assert amixer_cmd("Phone", "PCM", 35) == ["amixer", "-q", "-M", "-c", "Phone", "sset", "PCM", "35%"]
    assert amixer_cmd("Phone", "Mic", 80, capture=True)[-1] == "cap"


def test_both_plays_on_handset_and_speaker(wav):
    out, sp, _ = make()
    pb = out.play(wav, Output.BOTH)
    assert sp.devices() == ["plughw:CARD=Phone,DEV=0", "plughw:CARD=Spk,DEV=0"]
    assert all(k["stdin"] is subprocess.DEVNULL for k in sp.kwargs)
    assert not pb.done
    sp.procs[0].finish()
    assert not pb.done  # waits for both outputs
    sp.procs[1].finish()
    assert pb.done and pb.wait(0)


def test_default_output_is_both(wav):
    out, sp, _ = make()
    out.play(wav)
    assert len(sp.procs) == 2


def test_speaker_only_for_chimes(wav):
    out, sp, _ = make()
    out.play(wav, Output.SPEAKER)
    assert sp.devices() == ["plughw:CARD=Spk,DEV=0"]


def test_stop_terminates_and_reaps(wav):
    out, sp, _ = make()
    pb = out.play(wav)
    pb.stop()
    assert pb.done and pb.stopped
    assert all(p.terminated and p.waited for p in sp.procs)
    pb.stop()  # idempotent


def test_stop_kills_if_terminate_ignored():
    p = FakeProc(hang_on_terminate=True)
    reap(p, grace_s=0.01)
    assert p.terminated and p.killed and p.waited


def test_stop_all(wav):
    out, sp, _ = make()
    a = out.play(wav)
    b = out.play(wav, Output.SPEAKER)
    out.stop_all()
    assert a.done and b.done
    assert out.active == []


def test_finished_playbacks_are_pruned(wav):
    out, sp, _ = make()
    out.play(wav)
    for p in sp.procs:
        p.finish()
    assert out.active == []


def test_missing_file_returns_failed_done_playback(tmp_path):
    out, sp, _ = make()
    pb = out.play(tmp_path / "nope.wav")
    assert pb.done and pb.failed and sp.procs == []


def test_spawn_failure_is_not_fatal(wav):
    out, _, _ = make(fail_on="aplay")
    pb = out.play(wav)
    assert pb.done and pb.failed


def test_wait_timeout(wav):
    out, sp, _ = make()
    pb = out.play(wav, Output.SPEAKER)
    assert pb.wait(0.05) is False
    threading.Timer(0.05, sp.procs[0].finish).start()
    assert pb.wait(2) is True


@pytest.mark.parametrize(
    "level,ceiling,pct",
    [(1, 0.5, 50), (0.5, 0.5, 25), (0, 0.8, 0), (2, 0.8, 80), (-1, 0.8, 0),
     (float("nan"), 0.8, 0), (1, 1.5, 100), (0.333, 0.6, 19)],
)
def test_volume_percent_never_exceeds_ceiling(level, ceiling, pct):
    assert volume_percent(level, ceiling) == pct
    assert volume_percent(level, ceiling) <= clamp01(ceiling) * 100


def test_set_volume_applies_ceiling_per_card():
    out, _, run = make()
    run.cmds.clear()
    out.set_volume(0.5)
    assert run.cmds == [
        ["amixer", "-q", "-M", "-c", "Phone", "sset", "PCM", "25%"],
        ["amixer", "-q", "-M", "-c", "Spk", "sset", "Speaker", "40%"],
    ]
    out.set_volume(5)
    assert run.cmds[-1][-1] == "80%" and run.cmds[-2][-1] == "50%"


def test_initial_volume_applied_on_start():
    out, _, run = make()
    assert len(run.cmds) == 2
    assert run.cmds[0][-1] == f"{volume_percent(CFG.default_volume, 0.5)}%"


def test_card_without_mixer_is_skipped():
    cfg = AudioConfig(handset=CardConfig(card="Phone", mixer_control=None), speaker=CFG.speaker)
    run = Runner()
    AlsaAudioOut(cfg, spawn=Spawner(), run=run)
    assert [c[4] for c in run.cmds] == ["Spk"]


def test_amixer_failure_is_logged_not_raised(caplog):
    out = AlsaAudioOut(CFG, spawn=Spawner(), run=Runner(fail=True))
    out.set_volume(0.3)
    assert out.volume == 0.3
    assert "command failed" in caplog.text


def test_speaker_muted_stops_speaker_and_blocks_new(wav):
    out, sp, run = make()
    both = out.play(wav)
    chime = out.play(wav, Output.SPEAKER)
    handset_p, spk_p, chime_p = sp.procs
    with out.speaker_muted():
        assert out.speaker_is_muted
        assert spk_p.terminated and chime_p.terminated and not handset_p.terminated
        assert chime.done and not both.done
        assert run.cmds[-1] == ["amixer", "-q", "-M", "-c", "Spk", "sset", "Speaker", "0%"]
        n = len(sp.procs)
        pb = out.play(wav, Output.BOTH)
        assert sp.devices()[n:] == ["plughw:CARD=Phone,DEV=0"]
        silent = out.play(wav, Output.SPEAKER)
        assert silent.done and not silent.failed
        out.set_volume(1.0)  # dashboard change while muted keeps speaker at 0
        assert run.cmds[-1][-1] == "0%"
        with out.speaker_muted():  # nested
            pass
        assert out.speaker_is_muted
    assert not out.speaker_is_muted
    assert run.cmds[-1][-1] == "80%"  # restored at current volume
    pb.stop()


def test_thread_safe_play_and_stop_all(wav):
    out, sp, _ = make()
    errs = []

    def worker():
        try:
            for _ in range(50):
                out.play(wav)
                out.stop_all()
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    out.stop_all()
    assert not errs
    assert all(p.poll() is not None for p in sp.procs)


def test_real_subprocess_reaped(tmp_path, wav):
    # Use `sleep` as a stand-in for aplay to check real process cleanup.
    def spawn(argv, **kw):
        return subprocess.Popen(["sleep", "30"], **kw)

    out = AlsaAudioOut(CFG, spawn=spawn, run=Runner())
    pb = out.play(wav)
    t0 = time.monotonic()
    out.stop_all()
    assert pb.done and time.monotonic() - t0 < 2
    assert all(p.returncode is not None for p in pb.procs)


def test_playback_with_no_procs_is_done():
    assert Playback().done and Playback().wait(0)
