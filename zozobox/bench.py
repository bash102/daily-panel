"""Bench tool for bringing up the hardware on the Pi (docs/BRINGUP.md).

    zozobox-bench leds      # cycle each LED through every state
    zozobox-bench buttons   # print accepted presses / parent chord
    zozobox-bench           # both: a press fast-pulses its LED for 1 s
    zozobox-bench print [letter|weather|mood|coloring|all]   # test cards
    zozobox-bench audio [--file F] [--volume V] [--seconds S]
                            # beep on the speaker, F (or the beep) on both
                            # outputs, then a mic take with its levels
    --config FILE           # box.toml (default $ZOZOBOX_CONFIG or /etc/zozobox/box.toml)
    --fake                  # run without hardware (desktop smoke test)

Stop zozobox-engine first (`sudo systemctl stop zozobox-engine`): it owns
the GPIO pins, LEDs and audio devices. Printing goes through the shared
printer lock, so it is safe while the dashboard runs.
"""

from __future__ import annotations

import argparse
import logging
import queue
import time

from pathlib import Path

from .buttons import ButtonInput, FakeButtonBackend, ParentChord, Press
from .config import BoxConfig
from .leds import FakeLedBackend, LedController, LedState

CARDS = ("letter", "weather", "mood", "coloring")


def _backends(cfg: BoxConfig, fake: bool):
    if fake:
        return FakeButtonBackend(cfg.buttons.pins), FakeLedBackend()
    from .hw.pi import GpioButtonBackend, Pca9685LedBackend

    return GpioButtonBackend(cfg.buttons), Pca9685LedBackend(cfg.leds)


def run_leds(leds: LedController, cfg: BoxConfig, hold: float) -> None:
    for b in cfg.leds.channels:
        for state in LedState:
            print(f"{b.value:>6}: {state.value}")
            leds.set(b, state)
            time.sleep(hold)
        leds.set(b, LedState.OFF)


def run_interactive(buttons: ButtonInput, leds: LedController, cfg: BoxConfig) -> None:
    leds.set_all(LedState.DIM)
    revert: dict = {}
    print("Press buttons (Ctrl-C to quit). Hold red+yellow 5 s for the parent chord.")
    while True:
        try:
            ev = buttons.events.get(timeout=0.05)
        except queue.Empty:
            ev = None
        now = time.monotonic()
        if isinstance(ev, Press):
            print(f"press {ev.button.value} -> {cfg.roles[ev.button].value}")
            leds.set(ev.button, LedState.PULSE_FAST)
            revert[ev.button] = now + 1.0
        elif isinstance(ev, ParentChord):
            print("PARENT CHORD")
            leds.set_all(LedState.BRIGHT)
            revert = {b: now + 2.0 for b in cfg.leds.channels}
        for b, due in list(revert.items()):
            if now >= due:
                leds.set(b, LedState.DIM)
                del revert[b]


def test_card(kind: str):
    """A sample card of each kind, rendered exactly like the engine does."""
    from PIL import Image, ImageDraw

    from .printer import (render_coloring_page, render_letter_card, render_mood_card,
                          render_weather_card)

    art = Image.new("L", (600, 600), 255)
    d = ImageDraw.Draw(art)
    d.ellipse((60, 60, 540, 540), outline=0, width=14)
    d.ellipse((200, 220, 250, 270), fill=0)
    d.ellipse((350, 220, 400, 270), fill=0)
    d.arc((180, 260, 420, 440), 20, 160, fill=0, width=14)
    if kind == "letter":
        return render_letter_card("B", ["ball", "bear", "bus"], art)
    if kind == "weather":
        return render_weather_card("Sunny and cold. Wear your jacket!", "sunny", 8, -2)
    if kind == "mood":
        return render_mood_card(None)
    if kind == "coloring":
        return render_coloring_page(art, "A happy face")
    raise ValueError(kind)


def run_print(app_cfg, which: str, fake: bool) -> int:
    from .printer import FakePrinter, PrinterError, make_locked_brother

    if fake:
        printer = FakePrinter(Path(app_cfg.data_dir) / "bench-prints")
    else:
        printer = make_locked_brother(app_cfg.data_dir, app_cfg.printer)
    bad = 0
    for kind in (CARDS if which == "all" else (which,)):
        try:
            printer.print_image(test_card(kind))
            print(f"printed {kind}")
        except PrinterError as e:
            print(f"{kind}: FAILED: {e}")
            bad += 1
    return 1 if bad else 0


def run_audio(app_cfg, fake: bool, file: str | None, volume: float | None,
              seconds: float) -> int:
    import tempfile
    import threading
    import wave

    from .assets import ensure_chimes
    from .audio import FakeAudioIn, FakeAudioOut, Output, rms_dbfs

    if fake:
        out = FakeAudioOut(auto_finish=True)
        inp = FakeAudioIn(muter=out)
    else:
        from .audio import create_alsa_audio
        out, inp = create_alsa_audio(app_cfg.audio)
    if volume is not None:
        out.set_volume(volume)
    with tempfile.TemporaryDirectory() as td:
        beep = ensure_chimes(td)["beep.wav"]
        print(f"speaker only: {beep.name}")
        out.play(beep, Output.SPEAKER).wait(timeout=5)
        target = Path(file) if file else beep
        print(f"handset + speaker: {target}  (measure the earpiece now)")
        pb = out.play(target, Output.BOTH)
        pb.wait(timeout=None if file else 5)
        if pb.failed:
            print("playback FAILED (see the log: card name, mixer control, ceiling)")
        mic = app_cfg.audio.mic
        print(f"recording {seconds:.0f}s from the mic ({mic.card}); speak normally...")
        rec = inp.record(Path(td) / "take.wav", seconds, threading.Event())
        with wave.open(str(rec.path), "rb") as w:
            pcm = w.readframes(w.getnframes())
            rate = w.getframerate()
        chunk = max(int(rate * mic.chunk_s) * 2, 2)
        levels = sorted(rms_dbfs(pcm[i:i + chunk]) for i in range(0, len(pcm), chunk))
        if levels:
            loud = levels[int(len(levels) * 0.9)]
            quiet = levels[int(len(levels) * 0.1)]
            print(f"took {rec.duration_s:.1f}s, silent={rec.silent}; level p10={quiet:.1f} dBFS "
                  f"p90={loud:.1f} dBFS; speech threshold speech_dbfs={mic.speech_dbfs}")
        else:
            print("no audio captured")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="zozobox-bench")
    p.add_argument("mode", nargs="?", choices=["leds", "buttons", "both", "print", "audio"],
                   default="both")
    p.add_argument("card", nargs="?", default="all", choices=CARDS + ("all",),
                   help="print: which card")
    p.add_argument("--config", help="box.toml (default $ZOZOBOX_CONFIG or /etc/zozobox/box.toml)")
    p.add_argument("--fake", action="store_true", help="no hardware")
    p.add_argument("--hold", type=float, default=2.0, help="seconds per LED state")
    p.add_argument("--file", help="audio: file to play on both outputs (calibration)")
    p.add_argument("--volume", type=float, help="audio: dashboard volume 0..1 (default: config)")
    p.add_argument("--seconds", type=float, default=4.0, help="audio: mic take length")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO)

    from .boxconfig import ConfigError, load_app_config

    try:
        app_cfg = load_app_config(a.config, required=bool(a.config))
    except ConfigError as e:
        print(f"config error: {e}")
        return 2
    if a.mode == "print":
        return run_print(app_cfg, a.card, a.fake)
    if a.mode == "audio":
        return run_audio(app_cfg, a.fake, a.file, a.volume, a.seconds)
    cfg: BoxConfig = app_cfg.box
    btn_backend, led_backend = _backends(cfg, a.fake)
    leds = LedController(cfg.leds, led_backend)
    buttons = ButtonInput(cfg.buttons, btn_backend)
    try:
        if a.mode == "leds":
            run_leds(leds, cfg, a.hold)
        else:
            run_interactive(buttons, leds, cfg)
    except KeyboardInterrupt:
        pass
    finally:
        buttons.close()
        leds.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
