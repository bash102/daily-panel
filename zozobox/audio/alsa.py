"""ALSA backends: `aplay` per output card, `arecord` for the handset mic, `amixer`.

Lives here (not in zozobox/hw/) because it needs only the ALSA command-line
tools (`alsa-utils`), no Python hardware libraries. Commands are built by plain
functions and processes are created through an injectable `spawn`, so all of
this is tested off-Pi with fake processes.

Hearing safety (SPEC section 5): the mixer ceiling is re-applied before a card
is used whenever the last apply failed or the card looked like it dropped out
(aplay failed to start or exited non-zero), and by default a card whose ceiling
cannot be confirmed is not played on. This is best effort: the real backstop
is a `softvol` hard cap in /etc/asound.conf, which M10 must install (M10
checklist item: "asound.conf softvol max_dB cap on handset and speaker").
"""

from __future__ import annotations

import contextlib
import logging
import os
import select
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Iterator, Sequence

from .base import Output, Playback, Proc, Recording, SpeakerMuter, muted, reap
from .config import AudioConfig, CardConfig, MicConfig
from .recording import record_pcm

log = logging.getLogger(__name__)

Spawn = Callable[..., Proc]  # (argv, **popen_kwargs) -> process
Run = Callable[[Sequence[str]], None]  # runs a short command, raises on failure


def _run(argv: Sequence[str]) -> None:
    subprocess.run(list(argv), check=True, capture_output=True, timeout=5)


def clamp01(x: float) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if x != x:  # NaN
        return 0.0
    return min(max(x, 0.0), 1.0)


def volume_percent(level: float, ceiling: float) -> int:
    """Mixer percent for dashboard `level` (0..1) under a hearing-safe ceiling."""
    return int(clamp01(level) * clamp01(ceiling) * 100)  # floor: never above ceiling


def aplay_cmd(card: CardConfig, path: Path, aplay: str = "aplay") -> list[str]:
    return [aplay, "-q", "-D", card.pcm, "--", str(path)]


def amixer_cmd(card: str, control: str, percent: int, amixer: str = "amixer", capture: bool = False) -> list[str]:
    # -M: mapped (perceptual) volume, so the ceiling fraction tracks loudness.
    argv = [amixer, "-q", "-M", "-c", card, "sset", control, f"{int(percent)}%"]
    if capture:
        argv.append("cap")
    return argv


def arecord_cmd(mic: MicConfig, arecord: str = "arecord") -> list[str]:
    return [
        arecord, "-q", "-D", mic.pcm, "-t", "raw", "-f", "S16_LE",
        "-r", str(mic.rate), "-c", str(mic.channels),
    ]


HANDSET, SPEAKER = "handset", "speaker"


@dataclass
class _Child:
    proc: Proc
    role: str
    card: CardConfig
    err: IO[bytes] | None
    reported: bool = False


def _stderr_file() -> IO[bytes] | None:
    # A temp file, not a pipe: aplay can print many xrun lines on a long
    # sound, and a full pipe nobody reads would stall it.
    try:
        return tempfile.TemporaryFile(prefix="aplay-err-")
    except OSError:
        return None


def _read_err(f: IO[bytes] | None, limit: int = 500) -> str:
    if f is None:
        return ""
    try:
        f.seek(0)
        return f.read(limit).decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _close(f: IO[bytes] | None) -> None:
    if f is not None:
        try:
            f.close()
        except Exception:
            pass


class AlsaAudioOut:
    """Plays WAVs with one `aplay` per output card.

    BOTH starts handset and speaker together; SPEAKER plays on the box speaker
    only. While the speaker is muted (recording), speaker processes are
    stopped and not started, and the speaker mixer is set to 0%. A SPEAKER
    sound requested while muted is not played and its Playback has
    `suppressed=True`. Note for M5: a button press that stops a recording
    should play its click after `record()` returns (or replay it when the
    Playback came back suppressed), otherwise the click is not heard.

    `refuse_uncapped=True` (default) skips a card whose volume ceiling could
    not be applied (amixer failed even on retry); the error is logged.
    `ceiling_ok` reports per-output state for diagnostics.
    """

    def __init__(
        self,
        cfg: AudioConfig | None = None,
        *,
        spawn: Spawn = subprocess.Popen,
        run: Run = _run,
        apply_volume: bool = True,
        refuse_uncapped: bool = True,
    ):
        self.cfg = cfg or AudioConfig()
        self._spawn = spawn
        self._run = run
        self._refuse_uncapped = refuse_uncapped
        # One lock for state, process list and amixer runs, so a volume is
        # computed and applied atomically (no stale level winning a race).
        self._lock = threading.RLock()
        self._active: list[tuple[Playback, list[_Child]]] = []
        self._mute_depth = 0
        self._volume = clamp01(self.cfg.default_volume)
        # Whether the ceiling is known to be applied on each card. Without
        # apply_volume we trust the caller and only re-apply after a failure.
        self._vol_ok = {HANDSET: not apply_volume, SPEAKER: not apply_volume}
        if apply_volume:
            self.set_volume(self._volume)

    def _card(self, role: str) -> CardConfig:
        return self.cfg.handset if role == HANDSET else self.cfg.speaker

    # -- playback ---------------------------------------------------------
    def play(self, path: Path, output: Output = Output.BOTH) -> Playback:
        path = Path(path)
        if not path.is_file():
            log.warning("audio file missing: %s", path)
            return Playback(failed=True)
        with self._lock:
            self._prune()
            roles: list[str] = []
            if output is Output.BOTH:
                roles.append(HANDSET)
            if self._mute_depth == 0:
                roles.append(SPEAKER)
            elif output is Output.SPEAKER:
                return Playback(suppressed=True)
            children: list[_Child] = []
            failed = False
            for role in roles:
                card = self._card(role)
                if not self._vol_ok[role]:
                    self._apply([role])  # retry: card may be back / re-enumerated
                    if not self._vol_ok[role] and self._refuse_uncapped:
                        log.error("not playing on %s (%s): volume ceiling not applied", role, card.card)
                        failed = True
                        continue
                err = _stderr_file()
                try:
                    p = self._spawn(
                        aplay_cmd(card, path, self.cfg.aplay),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=err if err is not None else subprocess.DEVNULL,
                    )
                except Exception as e:  # aplay missing, fork failure, ...
                    _close(err)
                    log.warning("aplay on %s (%s) failed to start: %s", role, card.card, e)
                    self._vol_ok[role] = False  # card may have re-enumerated
                    failed = True
                    continue
                children.append(_Child(p, role, card, err))
            pb = Playback([c.proc for c in children], failed=failed)  # any output failed
            if children:
                self._active.append((pb, children))
            return pb

    def stop_all(self) -> None:
        with self._lock:
            active, self._active = self._active, []
        for pb, _ in active:
            pb.stop()
        with self._lock:
            for pb, children in active:
                self._collect(pb, children)

    def _collect(self, pb: Playback, children: list[_Child]) -> None:
        """Report and clean up children that have exited (lock held)."""
        for c in children:
            if c.reported or c.proc.poll() is None:
                continue
            c.reported = True
            rc = c.proc.poll()
            if rc != 0 and not pb.released(c.proc):
                log.warning("aplay on %s (%s) exited with %s: %s",
                            c.role, c.card.card, rc, _read_err(c.err) or "no stderr")
                self._vol_ok[c.role] = False  # re-apply ceiling before next use
            _close(c.err)

    def _prune(self) -> None:
        # poll() reaps finished children, so no zombies accumulate.
        keep = []
        for pb, children in self._active:
            self._collect(pb, children)
            if not pb.done:
                keep.append((pb, children))
        self._active = keep

    @property
    def active(self) -> list[Playback]:
        with self._lock:
            self._prune()
            return [pb for pb, _ in self._active]

    # -- volume -----------------------------------------------------------
    def _percent(self, role: str, level: float) -> int:
        if role == SPEAKER and self._mute_depth:
            return 0
        return volume_percent(level, self._card(role).max_fraction)

    def volume_commands(self, level: float) -> list[list[str]]:
        cmds = []
        for role in (HANDSET, SPEAKER):
            card = self._card(role)
            if card.mixer_control is None:
                continue
            cmds.append(amixer_cmd(card.card, card.mixer_control, self._percent(role, level), self.cfg.amixer))
        return cmds

    def _apply(self, roles: Sequence[str]) -> None:
        with self._lock:
            for role in roles:
                card = self._card(role)
                if card.mixer_control is None:
                    self._vol_ok[role] = True  # cap must come from asound.conf
                    continue
                argv = amixer_cmd(card.card, card.mixer_control, self._percent(role, self._volume), self.cfg.amixer)
                self._vol_ok[role] = self._try_run(argv)

    def set_volume(self, level: float) -> None:
        with self._lock:
            self._volume = clamp01(level)
            self._apply((HANDSET, SPEAKER))

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def ceiling_ok(self) -> dict[str, bool]:
        """Per output ("handset", "speaker"): was the last volume apply OK?"""
        with self._lock:
            return dict(self._vol_ok)

    def _try_run(self, argv: Sequence[str]) -> bool:
        try:
            self._run(argv)
            return True
        except Exception as e:
            log.warning("command failed: %s: %s", " ".join(argv), e)
            return False

    # -- speaker mute (used by AlsaAudioIn while recording) ---------------
    @contextlib.contextmanager
    def speaker_muted(self) -> Iterator[None]:
        with self._lock:
            self._mute_depth += 1
            first = self._mute_depth == 1
            to_stop = [(pb, [c.proc for c in ch if c.role == SPEAKER]) for pb, ch in self._active] if first else []
        if first:
            for pb, procs in to_stop:
                if procs:
                    pb.stop_procs(procs)
            self._apply([SPEAKER])  # speaker mixer -> 0%
        try:
            yield
        finally:
            with self._lock:
                self._mute_depth -= 1
                if self._mute_depth == 0:
                    self._apply([SPEAKER])

    @property
    def speaker_is_muted(self) -> bool:
        return self._mute_depth > 0

    def close(self) -> None:
        self.stop_all()


class ArecordSource:
    """PcmSource reading raw S16_LE from an `arecord` child's stdout.

    `kill()` (safe from another thread, e.g. a watchdog) stops arecord and makes
    a pending or later `read()` return b"" within ~0.1 s, even if the process
    cannot be killed (stuck in the kernel). Only the reading thread calls
    `close()`, which also closes the pipe.
    """

    POLL_S = 0.1

    def __init__(self, mic: MicConfig, *, arecord: str = "arecord", spawn: Spawn = subprocess.Popen):
        self._proc = spawn(
            arecord_cmd(mic, arecord),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._killed = threading.Event()
        self._closed = False
        self._lock = threading.Lock()

    def _fd(self) -> int | None:
        try:
            return self._proc.stdout.fileno()  # type: ignore[attr-defined]
        except Exception:
            return None

    def read(self, n: int) -> bytes:
        out = self._proc.stdout  # type: ignore[attr-defined]
        fd = self._fd()
        buf = b""
        while len(buf) < n and not self._killed.is_set():  # pipe reads may return short
            try:
                if fd is not None:
                    ready, _, _ = select.select([fd], [], [], self.POLL_S)
                    if not ready:
                        continue
                    part = os.read(fd, n - len(buf))
                else:
                    part = out.read(n - len(buf))
            except (OSError, ValueError):  # pipe closed under us: end of stream
                break
            if not part:
                break
            buf += part
        return buf

    def kill(self) -> None:
        self._killed.set()
        reap(self._proc)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.kill()
        try:
            self._proc.stdout.close()  # type: ignore[attr-defined]
        except Exception:
            pass


class AlsaAudioIn:
    """Records from the handset mic with `arecord`, muting the box speaker.

    `muter` is required (normally the AlsaAudioOut; see `create_alsa_audio`).
    Passing None explicitly records without muting and logs a warning.
    """

    def __init__(
        self,
        cfg: AudioConfig | None = None,
        *,
        muter: SpeakerMuter | None,
        source_factory: Callable[[MicConfig], object] | None = None,
        run: Run = _run,
        apply_gain: bool = True,
        stall_grace_s: float = 2.0,
    ):
        self.cfg = cfg or AudioConfig()
        if muter is None:
            log.warning("AlsaAudioIn without a speaker muter: the box speaker will not be muted while recording")
        self._muter = muter
        self._source_factory = source_factory or (
            lambda mic: ArecordSource(mic, arecord=self.cfg.arecord)
        )
        self._stall_grace_s = stall_grace_s
        self._lock = threading.Lock()  # one recording at a time
        mic = self.cfg.mic
        if apply_gain and mic.mixer_control is not None:
            pct = int(clamp01(mic.capture_fraction) * 100)
            argv = amixer_cmd(mic.card, mic.mixer_control, pct, self.cfg.amixer, capture=True)
            try:
                run(argv)
            except Exception as e:
                log.warning("mic gain command failed: %s: %s", " ".join(argv), e)

    @property
    def muter(self) -> SpeakerMuter | None:
        return self._muter

    def record(self, path: Path, max_s: float, stop: threading.Event) -> Recording:
        mic = self.cfg.mic
        with self._lock, muted(self._muter):
            try:
                source = self._source_factory(mic)
            except Exception:
                log.exception("could not start recording")
                return record_pcm(_EmptySource(), path, 0, stop, rate=mic.rate, channels=mic.channels)
            # Watchdog: if the device stalls, a blocked read() would ignore stop
            # and max_s. Killing the source unblocks it; the reader thread
            # itself closes the source (record_pcm's finally).
            done = threading.Event()
            unblock = getattr(source, "kill", None) or source.close

            def watchdog():
                start = time.monotonic()
                stopped_at = None
                while not done.wait(0.1):
                    now = time.monotonic()
                    if stopped_at is None and stop.is_set():
                        stopped_at = now
                    late_stop = stopped_at is not None and now - stopped_at > self._stall_grace_s
                    overdue = now - start > max(0.0, max_s) + self._stall_grace_s
                    if late_stop or overdue:
                        log.warning("recording stalled; stopping source")
                        try:
                            unblock()
                        except Exception:
                            log.exception("stopping stalled recording source failed")
                        return

            t = threading.Thread(target=watchdog, name="rec-watchdog", daemon=True)
            t.start()
            try:
                return record_pcm(
                    source, path, max_s, stop,
                    rate=mic.rate, channels=mic.channels, chunk_s=mic.chunk_s,
                    speech_dbfs=mic.speech_dbfs, min_speech_s=mic.min_speech_s,
                )
            finally:
                done.set()


def create_alsa_audio(
    cfg: AudioConfig | None = None, **out_kwargs
) -> tuple[AlsaAudioOut, AlsaAudioIn]:
    """The production wiring: the recorder mutes this AlsaAudioOut's speaker."""
    cfg = cfg or AudioConfig()
    out = AlsaAudioOut(cfg, **out_kwargs)
    return out, AlsaAudioIn(cfg, muter=out)


class _EmptySource:
    def read(self, n: int) -> bytes:
        return b""

    def close(self) -> None:
        pass
