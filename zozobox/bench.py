"""Bench tool for bringing up buttons and LEDs on the Pi.

    zozobox-bench leds      # cycle each LED through every state
    zozobox-bench buttons   # print accepted presses / parent chord
    zozobox-bench           # both: a press fast-pulses its LED for 1 s
    --fake                  # run without hardware (desktop smoke test)
"""

from __future__ import annotations

import argparse
import logging
import queue
import time

from .buttons import ButtonInput, FakeButtonBackend, ParentChord, Press
from .config import BoxConfig
from .leds import FakeLedBackend, LedController, LedState


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


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="zozobox-bench")
    p.add_argument("mode", nargs="?", choices=["leds", "buttons", "both"], default="both")
    p.add_argument("--fake", action="store_true", help="no hardware")
    p.add_argument("--hold", type=float, default=2.0, help="seconds per LED state")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO)

    cfg = BoxConfig()
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


if __name__ == "__main__":
    main()
