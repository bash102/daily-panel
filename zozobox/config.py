"""Static hardware configuration for the box.

Runtime-adjustable settings (quiet hours, volume, recording cap, ...) live in
the settings store, not here. These values change only when the wiring changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Btn(str, Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    YELLOW = "yellow"


class Role(str, Enum):
    PRINT = "print"
    GRANDMA = "grandma"
    SOUNDBOARD = "soundboard"
    STORY = "story"


@dataclass(frozen=True)
class ButtonConfig:
    # BCM pin numbers. Each button: one leg to the pin, the other to GND
    # (internal pull-up). Avoids I2C (2, 3) and UART (14, 15).
    pins: dict[Btn, int] = field(
        default_factory=lambda: {Btn.RED: 17, Btn.GREEN: 27, Btn.BLUE: 22, Btn.YELLOW: 23}
    )
    debounce_s: float = 0.020
    # After an accepted press, all presses (any button) are dropped, not queued.
    lockout_s: float = 1.0
    # Hidden parent gesture: hold both for chord_hold_s.
    chord: tuple[Btn, Btn] = (Btn.RED, Btn.YELLOW)
    chord_hold_s: float = 5.0
    # How often the input thread reconciles pin levels and checks the chord.
    poll_s: float = 0.010


@dataclass(frozen=True)
class LedConfig:
    i2c_address: int = 0x40
    channels: dict[Btn, int] = field(
        default_factory=lambda: {Btn.RED: 0, Btn.GREEN: 1, Btn.BLUE: 2, Btn.YELLOW: 3}
    )
    pwm_hz: int = 1000  # >= 1 kHz: no visible flicker. PCA9685 max ~1526 Hz.
    # LEDs: anode -> 5V, cathode -> PCA9685 pin. Open-drain + inverted output
    # means duty cycle == LED on-time. Flip `invert` if brightness is backwards.
    open_drain: bool = True
    invert: bool = True
    # Perceived-brightness levels (0..1), gamma-corrected before output.
    max_brightness: float = 1.0
    dim_level: float = 0.06
    trim: dict[Btn, float] = field(
        default_factory=lambda: {Btn.RED: 1.0, Btn.GREEN: 1.0, Btn.BLUE: 1.0, Btn.YELLOW: 1.0}
    )
    gamma: float = 2.2
    slow_pulse_s: float = 2.0
    fast_pulse_s: float = 0.5
    frame_hz: float = 50.0


@dataclass(frozen=True)
class BoxConfig:
    buttons: ButtonConfig = field(default_factory=ButtonConfig)
    leds: LedConfig = field(default_factory=LedConfig)
    # Which colour does what. Not fixed by the spec; change to taste.
    roles: dict[Btn, Role] = field(
        default_factory=lambda: {
            Btn.GREEN: Role.PRINT,
            Btn.RED: Role.GRANDMA,
            Btn.YELLOW: Role.SOUNDBOARD,
            Btn.BLUE: Role.STORY,
        }
    )
