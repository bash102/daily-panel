"""The engine process (PLAN M10): wires the real backends to the Engine.

    python -m zozobox.app [--config ~/.config/zozobox/box.toml] [--data-dir DIR] [-v]
    python -m zozobox.app --fake          # desktop smoke test, no hardware
    python -m zozobox.app --check-config  # load + validate the config, print it

Runs until SIGTERM/SIGINT, then shuts down in order: button input, engine
(a grandma reply being recorded is saved, print jobs finish), LEDs off,
audio stopped, store closed.

Hardware is optional at runtime: a part that fails to initialize is logged
(journal + an `error` event `hw_init_failed` for the dashboard) and replaced
by an inert stand-in, so for example a missing printer never stops audio or
buttons. Buttons and LEDs are retried every `app.hw_retry_s` (a GPIO chip or
I2C bus that was not ready at boot comes back without a restart), the
printer every `app.printer_retry_s`.

Parent chord (red + yellow held 5 s, SPEC 3): a network-status chime (two
beeps = the last sync reached the cloud, one = offline), the LEDs blink,
then `app.shutdown_command` runs (default `sudo -n /usr/bin/systemctl
poweroff`; deploy/sudoers/zozobox-poweroff, installed for the service
user by deploy/host-setup.sh, allows exactly that).

With --fake every backend is a fake; type r/g/b/y (or print/grandma/
soundboard/story) + Enter to press a button, `chord` for the parent chord,
`q` to quit. Prints land as PNGs in <data-dir>/fake-prints/.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .assets import BEEP, CLICK, ensure_chimes
from .audio import AudioIn, AudioOut, FakeAudioIn, FakeAudioOut, FakeTake, Output
from .boxconfig import AppConfig, ConfigError, load_app_config
from .buttons import ButtonInput, FakeButtonBackend, ParentChord
from .config import Btn
from .engine import (DirPhraseBook, Engine, FakeMoodClassifier, MoodResult, StorePhraseBook,
                     StoreSoundboard)
from .leds import FakeLedBackend, LedController, LedState
from .printer import FakePrinter, LockedPrinter, SupervisedPrinter, brother_factory, lock_path
from .store import Store
from .store.events import EventType

log = logging.getLogger("zozobox.app")

HW_INIT_FAILED = "hw_init_failed"
# The network chime says "online" only if a sync pass reached the cloud this
# recently. The sync process passes every 30 s while online, so a stopped or
# crash-looping sync makes the chime say "offline" after a few minutes.
NETWORK_STATUS_MAX_AGE_S = 300.0
FAKE_DATA_DIR = Path.home() / ".zozobox-fake"


class OfflineClassifier:
    """Stand-in when the cloud classifier cannot be built: every take is
    queued for offline recognition (python -m zozobox.mood reprocess)."""

    def classify(self, path: Path) -> MoodResult:
        return MoodResult("unknown", offline=True)


@dataclass
class Backends:
    """Injected backends (tests). Anything None is built from the config."""

    buttons: Any = None
    leds: Any = None
    audio: tuple[AudioOut, AudioIn] | None = None
    printer: Any = None
    classifier: Any = None


class App:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        fake: bool = False,
        backends: Backends | None = None,
        store: Store | None = None,
        clock: Callable[[], float] = time.monotonic,
        start_threads: bool = True,
        run_command: Callable[..., Any] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.fake = fake
        self.backends = backends or Backends()
        self.clock = clock
        self.start_threads = start_threads
        self.run_command = run_command
        self.sleep = sleep
        self.health: dict[str, str] = {}
        self._own_store = store is None
        self.store: Store | None = store
        self.engine: Engine | None = None
        self.buttons: ButtonInput | None = None
        self.leds: LedController | None = None
        self.audio_out: AudioOut | None = None
        self.audio_in: AudioIn | None = None
        self.printer: Any = None
        self.printer_supervisor: SupervisedPrinter | None = None
        self.retrying: dict[str, Any] = {}  # "buttons"/"leds" -> Retrying*Backend
        self.button_backend: Any = None
        self.chimes: dict[str, Path] = {}
        self._chord_lock = threading.Lock()
        self._chord_running = False
        self.chord_threads: list[threading.Thread] = []
        self.shutdown_runs: list[list[str]] = []
        self._closed = False

    # ------------------------------------------------------------ building ---
    def _hw(self, name: str, make: Callable[[], Any], fallback: Callable[[], Any]) -> Any:
        try:
            obj = make()
            self.health[name] = "ok"
            return obj
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            log.error("%s: init failed (%s); running without it", name, msg)
            self.health[name] = f"failed: {msg}"
            self._event(EventType.ERROR, {"kind": HW_INIT_FAILED, "component": name,
                                          "error": msg[:300]})
            return fallback()

    def _retrying(self, name: str, backend: Any) -> Any:
        """First attempt now; maintain() keeps retrying a failed part."""
        self.retrying[name] = backend
        if backend.ensure():
            self.health[name] = "ok"
        else:
            err = backend.last_error or "unknown"  # already logged by the backend
            self.health[name] = f"failed: {err} (retrying)"
            self._event(EventType.ERROR, {"kind": HW_INIT_FAILED, "component": name,
                                          "error": err[:300]})
        return backend

    def _event(self, type_: str, payload: dict) -> None:
        if self.store is None:
            return
        try:
            self.store.events.append(type_, payload)
        except Exception:
            log.exception("could not log %s event", type_)

    def build(self) -> "App":
        cfg = self.cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if self.store is None:
            self.store = Store(cfg.data_dir)  # never recover_outbox: the sync process owns it
        try:
            self.chimes = ensure_chimes(cfg.chimes_dir)
        except Exception as e:
            log.error("chimes: cannot write %s: %s (no clicks or beeps)", cfg.chimes_dir, e)
        settings = dataclasses.replace(
            cfg.engine,
            click=cfg.engine.click or self.chimes.get(CLICK),
            beep=cfg.engine.beep or self.chimes.get(BEEP),
        )
        b = self.backends

        # LEDs
        if b.leds is not None:
            led_backend = b.leds
            self.health["leds"] = "injected"
        elif self.fake:
            led_backend = FakeLedBackend()
            self.health["leds"] = "fake"
        else:
            def make_leds():
                from .hw.pi import Pca9685LedBackend
                return Pca9685LedBackend(cfg.box.leds)
            from .hw.supervised import RetryingLedBackend
            led_backend = self._retrying("leds", RetryingLedBackend(
                make_leds, retry_s=cfg.app.hw_retry_s, clock=self.clock))
        self.leds = LedController(cfg.box.leds, led_backend, clock=self.clock,
                                  start_thread=self.start_threads)

        # Buttons
        if b.buttons is not None:
            self.button_backend = b.buttons
            self.health["buttons"] = "injected"
        elif self.fake:
            self.button_backend = FakeButtonBackend(cfg.box.buttons.pins)
            self.health["buttons"] = "fake"
        else:
            def make_buttons():
                from .hw.pi import GpioButtonBackend
                return GpioButtonBackend(cfg.box.buttons)
            from .hw.supervised import RetryingButtonBackend
            self.button_backend = self._retrying("buttons", RetryingButtonBackend(
                make_buttons, cfg.box.buttons.pins, retry_s=cfg.app.hw_retry_s,
                clock=self.clock))
        self.buttons = ButtonInput(cfg.box.buttons, self.button_backend, clock=self.clock,
                                   start_thread=self.start_threads)

        # Audio
        if b.audio is not None:
            self.audio_out, self.audio_in = b.audio
            self.health["audio"] = "injected"
        elif self.fake:
            self.audio_out, self.audio_in = _fake_audio()
            self.health["audio"] = "fake"
        else:
            def make_audio():
                from .audio import create_alsa_audio
                return create_alsa_audio(cfg.audio)
            self.audio_out, self.audio_in = self._hw("audio", make_audio, _fake_audio)

        # Printer (always behind the cross-process lock, shared with the dashboard)
        if b.printer is not None:
            self.printer = b.printer
            self.health["printer"] = "injected"
        elif self.fake:
            self.printer = FakePrinter(cfg.data_dir / "fake-prints")
            self.health["printer"] = "fake"
        else:
            self.printer_supervisor = SupervisedPrinter(
                brother_factory(cfg.printer), retry_s=cfg.app.printer_retry_s,
                clock=self.clock, name="QL-800 printer")
            if not self.printer_supervisor.ensure():
                err = self.printer_supervisor.last_error or "unknown"
                self.health["printer"] = f"failed: {err} (retrying)"
                self._event(EventType.ERROR, {"kind": HW_INIT_FAILED, "component": "printer",
                                              "error": err[:300]})
            else:
                self.health["printer"] = "ok"
            self.printer = LockedPrinter(self.printer_supervisor, lock_path(cfg.data_dir),
                                         timeout_s=cfg.app.printer_lock_timeout_s)

        # Mood classifier (M7)
        if b.classifier is not None:
            classifier = b.classifier
        elif self.fake:
            classifier = FakeMoodClassifier()
        else:
            def make_classifier():
                from .mood import OpenAIClaudeMoodClassifier
                return OpenAIClaudeMoodClassifier.from_store(self.store)
            classifier = self._hw("mood_classifier", make_classifier, OfflineClassifier)

        phrases = StorePhraseBook(self.store, fallback=DirPhraseBook(cfg.phrases_dir))
        try:
            cfg.soundboard_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error("soundboard dir %s: %s", cfg.soundboard_dir, e)
        soundboard = StoreSoundboard(self.store, cfg.soundboard_dir)
        if not soundboard.clips():
            log.warning("soundboard: no playable clips (run `python -m zozobox.content bootstrap` "
                        "and approve the soundboard clips, or import some into %s)",
                        cfg.soundboard_dir)

        self.engine = Engine(
            cfg.box, self.store, self.audio_out, self.audio_in, self.printer, self.leds,
            phrases, classifier, soundboard, clock=self.clock, settings=settings,
            on_parent_chord=self.on_parent_chord,
        )
        log.info("zozobox engine ready: %s", ", ".join(f"{k}={v}" for k, v in self.health.items()))
        return self

    # ------------------------------------------------------------ running ---
    def start(self) -> None:
        assert self.engine is not None and self.buttons is not None, "call build() first"
        self.engine.start(self.buttons.events)

    def maintain(self) -> None:
        """Periodic housekeeping from the main loop: buttons, LEDs and printer
        re-init."""
        for name, backend in self.retrying.items():
            if not backend.available and backend.ensure():
                self.health[name] = "ok"
                log.warning("%s: hardware is up now", name)
                self._event("hw_recovered", {"component": name, "attempts": backend.attempts})
                if name == "leds" and self.leds is not None:
                    self.leds.invalidate()
        if self.printer_supervisor is not None and not self.printer_supervisor.available:
            if self.printer_supervisor.ensure():
                self.health["printer"] = "ok"

    def run(self, stop: threading.Event, *, poll_s: float = 1.0) -> int:
        self.start()
        while not stop.wait(poll_s):
            try:
                self.maintain()
            except Exception:
                log.exception("maintenance failed")
            t = self.engine._thread if self.engine else None
            if t is not None and not t.is_alive():
                log.error("engine thread died; exiting so systemd restarts us")
                return 1
        return 0

    # ------------------------------------------------------- parent chord ---
    def on_parent_chord(self, ev: ParentChord) -> None:
        """Called on the engine thread: do the slow part in a thread."""
        with self._chord_lock:
            if self._chord_running:
                return
            self._chord_running = True
        t = threading.Thread(target=self._parent_shutdown, name="parent-chord", daemon=True)
        self.chord_threads.append(t)
        t.start()

    def _status_chime(self) -> None:
        beep = self.engine.settings.beep if self.engine else None
        if beep is None or self.audio_out is None:
            return
        online = self.network_online()
        for _ in range(2 if online else 1):
            try:
                self.audio_out.play(beep, Output.SPEAKER).wait(timeout=1.5)
            except Exception:
                log.exception("status chime failed")
            self.sleep(0.15)

    def network_online(self, now: "datetime | None" = None) -> bool:
        """True if the last sync pass reached the cloud and ran recently."""
        from datetime import datetime, timezone

        try:
            from .sync import load_status

            st = load_status(self.cfg.data_dir)
            if not st.online or not st.last_run_at:
                return False
            last = datetime.fromisoformat(st.last_run_at.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now is None:
                now = self.store.now() if self.store is not None else datetime.now(timezone.utc)
            age = (now - last).total_seconds()
            return -60.0 <= age <= NETWORK_STATUS_MAX_AGE_S
        except Exception:
            return False

    def _blink(self, times: int = 4, period_s: float = 0.5) -> None:
        if self.leds is None:
            return
        for _ in range(times):
            for state in (LedState.BRIGHT, LedState.OFF):
                self.leds.set_all(state)
                self.leds.render()
                self.sleep(period_s / 2)

    def _parent_shutdown(self) -> None:
        cmd = list(self.cfg.app.shutdown_command)
        try:
            log.warning("parent chord: shutting down the box")
            self._status_chime()
            self._blink()
            self._event("parent_shutdown", {"command": cmd, "fake": self.fake})
            if self.fake:
                log.warning("--fake: not running %s", " ".join(cmd))
                self.shutdown_runs.append(cmd)
                return
            self.shutdown_runs.append(cmd)
            r = self.run_command(cmd, timeout=60, capture_output=True, text=True)
            code = getattr(r, "returncode", 0)
            if code:
                err = (getattr(r, "stderr", "") or "").strip()[-300:]
                log.error("shutdown command %s failed (%s): %s", cmd, code, err)
                self._event(EventType.ERROR, {"kind": "shutdown_failed", "code": code,
                                              "error": err})
        except Exception as e:
            log.exception("parent shutdown failed")
            self._event(EventType.ERROR, {"kind": "shutdown_failed", "error": str(e)[:300]})
        finally:
            with self._chord_lock:
                self._chord_running = False

    # ------------------------------------------------------------ closing ---
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for name, fn in (
            ("buttons", lambda: self.buttons and self.buttons.close()),
            ("engine", lambda: self.engine and self.engine.close()),
            ("leds", lambda: self.leds and self.leds.close()),
            ("audio", lambda: self.audio_out and self.audio_out.stop_all()),
            ("printer", lambda: getattr(self.printer, "close", lambda: None)()),
        ):
            try:
                fn()
            except Exception:
                log.exception("closing %s failed", name)
        if self._own_store and self.store is not None:
            try:
                self.store.close()
            except Exception:
                log.exception("closing the store failed")
        log.info("zozobox engine stopped")


def _fake_audio() -> tuple[FakeAudioOut, FakeAudioIn]:
    out = FakeAudioOut(auto_finish=True)
    return out, FakeAudioIn(default=FakeTake(duration_s=1.0), muter=out)


# --------------------------------------------------------------- fake keys ---

KEYS = {"r": Btn.RED, "g": Btn.GREEN, "b": Btn.BLUE, "y": Btn.YELLOW}


def key_reader(app: App, stop: threading.Event, stream=None) -> None:
    """--fake: map typed lines to presses on the FakeButtonBackend."""
    stream = stream or sys.stdin
    by_role = {r.value: btn for btn, r in app.cfg.box.roles.items()}
    print("keys: r g b y | print grandma soundboard story | chord | q", flush=True)
    for line in stream:
        word = line.strip().lower()
        if word in ("q", "quit", "exit"):
            stop.set()
            return
        if word == "chord":
            app.buttons.events.put(ParentChord(app.clock()))
            continue
        btn = KEYS.get(word) or by_role.get(word) or next((b for b in Btn if b.value == word), None)
        if btn is None:
            print(f"? {word!r}", flush=True)
            continue
        be = app.button_backend
        if isinstance(be, FakeButtonBackend):
            be.set(btn, True)
            time.sleep(0.05)
            be.set(btn, False)
            print(f"pressed {btn.value} ({app.cfg.box.roles[btn].value})", flush=True)
    stop.set()


# -------------------------------------------------------------------- CLI ---

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m zozobox.app", description="Zozo box engine")
    p.add_argument("--config", help="box config TOML (default $ZOZOBOX_CONFIG or "
                                     "~/.config/zozobox/box.toml; missing = defaults)")
    p.add_argument("--data-dir", help="data dir (default $ZOZOBOX_DATA_DIR or the config's)")
    p.add_argument("--fake", action="store_true", help="fake backends (desktop smoke test)")
    p.add_argument("--keys", action=argparse.BooleanOptionalAction, default=None,
                   help="--fake: read button presses from stdin (default: when a TTY)")
    p.add_argument("--check-config", action="store_true", help="validate the config and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Sequence[str] | None = None, *, stop: threading.Event | None = None,
         backends: Backends | None = None, install_signals: bool = True) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load_app_config(args.config, required=bool(args.config))
    except ConfigError as e:
        print(f"zozobox: config error: {e}", file=sys.stderr)
        return 2
    if args.data_dir:
        cfg = dataclasses.replace(cfg, data_dir=Path(args.data_dir))
    elif args.fake and not os.environ.get("ZOZOBOX_DATA_DIR"):
        cfg = dataclasses.replace(cfg, data_dir=FAKE_DATA_DIR)
    level = logging.DEBUG if args.verbose else getattr(logging, cfg.app.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    if args.check_config:
        print(f"data_dir = {cfg.data_dir}")
        print(f"pins = { {b.value: p for b, p in cfg.box.buttons.pins.items()} }")
        print(f"leds = { {b.value: c for b, c in cfg.box.leds.channels.items()} } "
              f"invert={cfg.box.leds.invert} open_drain={cfg.box.leds.open_drain}")
        print(f"roles = { {b.value: r.value for b, r in cfg.box.roles.items()} }")
        print(f"handset = {cfg.audio.handset}")
        print(f"speaker = {cfg.audio.speaker}")
        print(f"mic = {cfg.audio.mic}")
        print(f"printer = {cfg.printer.identifier} ({cfg.printer.model})")
        print(f"shutdown_command = {' '.join(cfg.app.shutdown_command)}")
        return 0

    stop = stop or threading.Event()
    previous: dict[int, Any] = {}
    if install_signals and threading.current_thread() is threading.main_thread():
        def _sig(signum, frame):
            log.info("signal %s: stopping", signal.Signals(signum).name)
            stop.set()
        for s in (signal.SIGTERM, signal.SIGINT):
            previous[s] = signal.signal(s, _sig)

    app = App(cfg, fake=args.fake, backends=backends)
    try:
        try:
            app.build()
        except Exception as e:
            log.exception("zozobox: startup failed")
            print(f"zozobox: startup failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        keys = args.keys if args.keys is not None else (args.fake and sys.stdin.isatty())
        if args.fake and keys:
            threading.Thread(target=key_reader, args=(app, stop), name="keys", daemon=True).start()
        return app.run(stop)
    finally:
        app.close()  # SIGTERM path: buttons, engine, LEDs off, audio, store
        for s, h in previous.items():
            signal.signal(s, h)


if __name__ == "__main__":
    sys.exit(main())
