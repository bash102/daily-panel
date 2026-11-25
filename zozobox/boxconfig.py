"""Per-box static configuration from a TOML file (PLAN M10).

    cfg = load_app_config("/etc/zozobox/box.toml")   # missing file = defaults

Every table overrides the dataclass defaults field by field; anything left out
keeps its default. Unknown keys and wrong types are errors (a typo in a pin
name must not silently fall back to a default). See deploy/box.toml.example
for every key.

    data_dir = "/var/lib/zozobox"         # env ZOZOBOX_DATA_DIR wins over this
    [buttons]  pins = {red=17, ...}, debounce_s, lockout_s, chord, chord_hold_s
    [roles]    green = "print", red = "grandma", yellow = "soundboard", blue = "story"
    [leds]     channels = {red=0, ...}, invert, open_drain, pwm_hz, dim_level, trim, ...
    [audio]    default_volume; [audio.handset] / [audio.speaker]: card, device,
               mixer_control, max_fraction; [audio.mic]: card, mixer_control,
               capture_fraction, speech_dbfs, min_speech_s, ...
    [printer]  identifier, model, label, backend
    [engine]   EngineSettings tunables (repeat_pause_s, mood_record_s, ...)
    [app]      shutdown_command, printer_retry_s, hw_retry_s, soundboard_dir, phrases_dir,
               chimes_dir, log_level
"""

from __future__ import annotations

import dataclasses
import os
import shlex
import tomllib
import types
import typing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .audio.config import AudioConfig
from .config import BoxConfig, Btn, Role
from .engine.engine import EngineSettings
from .printer.backend import QLConfig

DEFAULT_CONFIG_PATH = Path("/etc/zozobox/box.toml")
DEFAULT_DATA_DIR = Path("/var/lib/zozobox")
CONFIG_ENV = "ZOZOBOX_CONFIG"
DATA_DIR_ENV = "ZOZOBOX_DATA_DIR"
DEFAULT_SHUTDOWN = ("sudo", "-n", "/usr/bin/systemctl", "poweroff")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AppSettings:
    # Run on the parent chord (red + yellow held 5 s). deploy/sudoers allows
    # exactly this command for the zozobox user.
    shutdown_command: tuple[str, ...] = DEFAULT_SHUTDOWN
    printer_retry_s: float = 60.0
    # Buttons (GPIO) and LEDs (I2C) that fail at start-up are retried this often.
    hw_retry_s: float = 30.0
    printer_lock_timeout_s: float = 30.0
    # None = under the data dir (soundboard/, assets/phrases/, chimes/).
    soundboard_dir: Path | None = None
    phrases_dir: Path | None = None  # fallback phrase WAVs, <name>.wav
    chimes_dir: Path | None = None
    log_level: str = "INFO"


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path = DEFAULT_DATA_DIR
    box: BoxConfig = field(default_factory=BoxConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    printer: QLConfig = field(default_factory=QLConfig)
    engine: EngineSettings = field(default_factory=EngineSettings)
    app: AppSettings = field(default_factory=AppSettings)

    @property
    def soundboard_dir(self) -> Path:
        return self.app.soundboard_dir or self.data_dir / "soundboard"

    @property
    def phrases_dir(self) -> Path:
        return self.app.phrases_dir or self.data_dir / "assets" / "phrases"

    @property
    def chimes_dir(self) -> Path:
        return self.app.chimes_dir or self.data_dir / "chimes"


# --------------------------------------------------------------- coercion ----


def _type_name(tp: Any) -> str:
    return getattr(tp, "__name__", None) or str(tp)


def _coerce(value: Any, tp: Any, where: str) -> Any:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        if type(None) in args and (value is None or value == "" or
                                   (isinstance(value, str) and value.lower() == "none")):
            return None
        rest = [a for a in args if a is not type(None)]
        errors = []
        for a in rest:
            try:
                return _coerce(value, a, where)
            except ConfigError as e:
                errors.append(str(e))
        raise ConfigError(errors[0] if errors else f"{where}: bad value {value!r}")
    if origin is dict:
        kt, vt = args
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a table, got {value!r}")
        return {_coerce(k, kt, f"{where} key"): _coerce(v, vt, f"{where}.{k}")
                for k, v in value.items()}
    if origin is tuple:
        if isinstance(value, str) and args and args[-1] is Ellipsis and args[0] is str:
            value = shlex.split(value)  # shutdown_command = "sudo -n ..."
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{where}: expected a list, got {value!r}")
        if args and args[-1] is Ellipsis:
            return tuple(_coerce(v, args[0], f"{where}[{i}]") for i, v in enumerate(value))
        if len(value) != len(args):
            raise ConfigError(f"{where}: expected {len(args)} items, got {len(value)}")
        return tuple(_coerce(v, a, f"{where}[{i}]") for i, (v, a) in enumerate(zip(value, args)))
    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(value.lower() if isinstance(value, str) else value)
        except ValueError:
            choices = ", ".join(m.value for m in tp)
            raise ConfigError(f"{where}: {value!r} is not one of {choices}") from None
    if tp is bool:
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{where}: expected true/false, got {value!r}")
    if tp is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                return int(value, 0)  # "0x40"
            except ValueError:
                pass
        raise ConfigError(f"{where}: expected an integer, got {value!r}")
    if tp is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    if tp is str:
        if isinstance(value, str):
            return value
        raise ConfigError(f"{where}: expected a string, got {value!r}")
    if tp is Path:
        if isinstance(value, str) and value:
            return Path(value)
        raise ConfigError(f"{where}: expected a path, got {value!r}")
    if dataclasses.is_dataclass(tp):
        return override(tp(), value, where)
    raise ConfigError(f"{where}: unsupported type {_type_name(tp)}")  # pragma: no cover


def override(base: Any, table: Any, where: str) -> Any:
    """A copy of dataclass `base` with the fields in `table` replaced."""
    if not isinstance(table, dict):
        raise ConfigError(f"[{where}]: expected a table, got {table!r}")
    hints = typing.get_type_hints(type(base))
    names = {f.name for f in dataclasses.fields(base)}
    changes: dict[str, Any] = {}
    for key, value in table.items():
        if key not in names:
            raise ConfigError(f"[{where}]: unknown key {key!r} (known: {', '.join(sorted(names))})")
        tp = hints[key]
        cur = getattr(base, key)
        if dataclasses.is_dataclass(cur) and isinstance(value, dict):
            changes[key] = override(cur, value, f"{where}.{key}")
        elif isinstance(cur, dict) and isinstance(value, dict):
            changes[key] = {**cur, **_coerce(value, tp, f"{where}.{key}")}  # partial table
        else:
            changes[key] = _coerce(value, tp, f"{where}.{key}")
    return dataclasses.replace(base, **changes)


# ----------------------------------------------------------------- checks ----


def validate(cfg: AppConfig) -> None:
    b = cfg.box
    pins = b.buttons.pins
    if set(pins) != set(Btn):
        raise ConfigError(f"[buttons].pins must name all of {', '.join(x.value for x in Btn)}")
    if len(set(pins.values())) != len(pins):
        raise ConfigError("[buttons].pins: two buttons share a GPIO pin")
    bad = [p for p in pins.values() if p in (2, 3)]
    if bad:
        raise ConfigError(f"[buttons].pins: GPIO {bad[0]} is I2C (the LED driver)")
    if set(b.leds.channels) != set(Btn):
        raise ConfigError("[leds].channels must name all four buttons")
    if len(set(b.leds.channels.values())) != len(b.leds.channels) or \
            any(not 0 <= c <= 15 for c in b.leds.channels.values()):
        raise ConfigError("[leds].channels: four distinct PCA9685 channels 0-15")
    if set(b.roles) != set(Btn) or set(b.roles.values()) != set(Role):
        raise ConfigError("[roles]: each button needs exactly one role, and each role one button")
    if b.buttons.chord[0] == b.buttons.chord[1]:
        raise ConfigError("[buttons].chord: two different buttons")
    for name in ("handset", "speaker"):
        card = getattr(cfg.audio, name)
        if not 0.0 < card.max_fraction <= 1.0:
            raise ConfigError(f"[audio.{name}].max_fraction must be in (0, 1]")
    if not cfg.app.shutdown_command:
        raise ConfigError("[app].shutdown_command must not be empty")


# ----------------------------------------------------------------- loader ----


def config_from_dict(data: dict[str, Any], *, env: dict[str, str] | None = None) -> AppConfig:
    env = os.environ if env is None else env
    data = dict(data)
    cfg = AppConfig()
    if "data_dir" in data:
        cfg = dataclasses.replace(cfg, data_dir=_coerce(data.pop("data_dir"), Path, "data_dir"))
    box = cfg.box
    for key, attr in (("buttons", "buttons"), ("leds", "leds")):
        if key in data:
            box = dataclasses.replace(box, **{attr: override(getattr(box, attr), data.pop(key), key)})
    if "roles" in data:
        roles = _coerce(data.pop("roles"), dict[Btn, Role], "roles")
        merged = dict(box.roles)
        merged.update(roles)
        box = dataclasses.replace(box, roles=merged)
    cfg = dataclasses.replace(cfg, box=box)
    for key in ("audio", "printer", "engine", "app"):
        if key in data:
            cfg = dataclasses.replace(cfg, **{key: override(getattr(cfg, key), data.pop(key), key)})
    if data:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(data))}")
    if env.get(DATA_DIR_ENV):
        cfg = dataclasses.replace(cfg, data_dir=Path(env[DATA_DIR_ENV]))
    validate(cfg)
    return cfg


def load_app_config(path: str | Path | None = None, *, env: dict[str, str] | None = None,
                    required: bool = False) -> AppConfig:
    """Load `path` (default $ZOZOBOX_CONFIG or /etc/zozobox/box.toml). A
    missing file gives the defaults unless `required`."""
    env = os.environ if env is None else env
    p = Path(path or env.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        if required:
            raise ConfigError(f"config file {p} not found") from None
        return config_from_dict({}, env=env)
    except OSError as e:
        raise ConfigError(f"cannot read {p}: {e}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"{p}: {e}") from None
    try:
        return config_from_dict(data, env=env)
    except ConfigError as e:
        raise ConfigError(f"{p}: {e}") from None


__all__ = [
    "AppConfig", "AppSettings", "ConfigError", "DEFAULT_CONFIG_PATH", "DEFAULT_SHUTDOWN",
    "config_from_dict", "load_app_config", "override", "validate",
]
