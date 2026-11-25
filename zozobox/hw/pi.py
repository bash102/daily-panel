"""Raspberry Pi backends: gpiozero buttons and a PCA9685 LED driver.

Install with `pip install -e .[pi]` on the Pi. Enable I2C in raspi-config.
"""

from __future__ import annotations

import os
import tempfile
from typing import Callable

from ..config import Btn, ButtonConfig, LedConfig

PCA9685_MODE2 = 0x01
MODE2_INVRT = 0x10
MODE2_OUTDRV = 0x04  # 1 = totem-pole, 0 = open-drain


LG_WD_ENV = "LG_WD"


def ensure_lg_wd(cwd: str | None = None, env: dict | None = None) -> str | None:
    """gpiozero's default pin factory on Bookworm (lgpio) creates its
    notification files in $LG_WD, else the current directory. When that is
    not writable (e.g. zozobox-bench run as another user from an admin's home),
    opening the gpiochip fails. Point LG_WD at a writable directory first.
    Returns the directory set, or None when nothing needed changing."""
    env = os.environ if env is None else env
    if env.get(LG_WD_ENV):
        return None
    cwd = cwd or os.getcwd()
    if os.access(cwd, os.W_OK | os.X_OK):
        return None
    for d in (env.get("ZOZOBOX_DATA_DIR"), tempfile.gettempdir()):
        if d and os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK):
            env[LG_WD_ENV] = d
            return d
    return None


class GpioButtonBackend:
    def __init__(self, cfg: ButtonConfig):
        ensure_lg_wd()  # before lgpio is loaded
        from gpiozero import Button  # lazy: only on the Pi

        # bounce_time=None: debouncing is done in ButtonArbiter so it is testable.
        self._buttons = {
            b: Button(pin, pull_up=True, bounce_time=None) for b, pin in cfg.pins.items()
        }

    def start(self, on_edge: Callable[[Btn, bool], None]) -> None:
        def make(b: Btn, pressed: bool):
            def cb():
                on_edge(b, pressed)

            return cb

        for b, gz in self._buttons.items():
            gz.when_pressed = make(b, True)
            gz.when_released = make(b, False)

    def read(self, button: Btn) -> bool:
        return bool(self._buttons[button].is_pressed)

    def close(self) -> None:
        for gz in self._buttons.values():
            gz.close()


def mode2_value(open_drain: bool, invert: bool) -> int:
    return (MODE2_INVRT if invert else 0) | (0 if open_drain else MODE2_OUTDRV)


class Pca9685LedBackend:
    """Adafruit PCA9685 breakout (#815): VCC from Pi 3.3V, V+ and LED anodes on 5V."""

    def __init__(self, cfg: LedConfig):
        import board  # adafruit-blinka
        from adafruit_pca9685 import PCA9685

        self._pca = PCA9685(board.I2C(), address=cfg.i2c_address)
        self._pca.frequency = cfg.pwm_hz
        # Open-drain so an "off" pin floats instead of driving 3.3V into a
        # 5V-anode LED (which would glow faintly). Inverted so duty = on-time.
        with self._pca.i2c_device as dev:
            dev.write(bytes([PCA9685_MODE2, mode2_value(cfg.open_drain, cfg.invert)]))

    def set_duty(self, channel: int, duty: int) -> None:
        self._pca.channels[channel].duty_cycle = duty

    def close(self) -> None:
        for ch in range(16):
            self._pca.channels[ch].duty_cycle = 0
        self._pca.deinit()
