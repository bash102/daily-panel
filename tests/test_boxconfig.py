from pathlib import Path

import pytest

from zozobox.boxconfig import (DEFAULT_SHUTDOWN, AppConfig, ConfigError, config_from_dict,
                               load_app_config)
from zozobox.config import Btn, Role

REPO = Path(__file__).resolve().parents[1]


def test_missing_file_gives_defaults(tmp_path):
    cfg = load_app_config(tmp_path / "nope.toml", env={})
    assert cfg == AppConfig()
    assert cfg.app.shutdown_command == DEFAULT_SHUTDOWN
    assert cfg.soundboard_dir == cfg.data_dir / "soundboard"


def test_required_missing_file_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_app_config(tmp_path / "nope.toml", env={}, required=True)


def test_example_file_loads_and_matches_defaults_where_given():
    cfg = load_app_config(REPO / "deploy" / "box.toml.example", env={})
    assert cfg.box.buttons.pins == AppConfig().box.buttons.pins
    assert cfg.box.roles == AppConfig().box.roles
    assert cfg.audio.handset.alsa_pcm == "zozobox_handset"
    assert cfg.audio.handset.pcm == "zozobox_handset"
    assert cfg.audio.mic.pcm.startswith("plughw:CARD=Phone")


def test_overrides_every_section(tmp_path):
    p = tmp_path / "box.toml"
    p.write_text('''
data_dir = "/srv/zz"
[buttons]
pins = { red = 5 }
chord = ["green", "blue"]
[roles]
red = "story"
blue = "grandma"
[leds]
invert = false
i2c_address = 0x41
trim = { blue = 0.8 }
[audio.handset]
card = "POP"
mixer_control = "none"
max_fraction = 0.35
[audio.mic]
speech_dbfs = -45
[printer]
identifier = "usb://0x04f9:0x209b/000123"
[engine]
mood_record_s = 6
click = "/x/click.wav"
[app]
shutdown_command = "sudo -n /usr/bin/systemctl poweroff"
printer_retry_s = 10
''')
    cfg = load_app_config(p, env={})
    assert cfg.data_dir == Path("/srv/zz")
    assert cfg.box.buttons.pins[Btn.RED] == 5 and cfg.box.buttons.pins[Btn.GREEN] == 27
    assert cfg.box.buttons.chord == (Btn.GREEN, Btn.BLUE)
    assert cfg.box.roles[Btn.RED] is Role.STORY and cfg.box.roles[Btn.BLUE] is Role.GRANDMA
    assert cfg.box.leds.invert is False and cfg.box.leds.i2c_address == 0x41
    assert cfg.box.leds.trim[Btn.BLUE] == 0.8 and cfg.box.leds.trim[Btn.RED] == 1.0
    assert cfg.audio.handset.card == "POP" and cfg.audio.handset.mixer_control is None
    assert cfg.audio.handset.max_fraction == 0.35
    assert cfg.audio.speaker.card == "Device"
    assert cfg.audio.mic.speech_dbfs == -45.0
    assert cfg.printer.identifier.endswith("000123")
    assert cfg.engine.mood_record_s == 6.0 and cfg.engine.click == Path("/x/click.wav")
    assert cfg.app.shutdown_command == DEFAULT_SHUTDOWN
    assert cfg.app.printer_retry_s == 10.0


def test_env_data_dir_wins():
    cfg = config_from_dict({"data_dir": "/a"}, env={"ZOZOBOX_DATA_DIR": "/b"})
    assert cfg.data_dir == Path("/b")


@pytest.mark.parametrize("data,msg", [
    ({"leds": {"invrt": True}}, "unknown key 'invrt'"),
    ({"bogus": 1}, "unknown top-level"),
    ({"buttons": {"pins": {"red": "x"}}}, "integer"),
    ({"buttons": {"pins": {"purple": 4}}}, "not one of"),
    ({"buttons": {"pins": {"red": 27}}}, "share a GPIO"),
    ({"buttons": {"pins": {"red": 2}}}, "I2C"),
    ({"roles": {"red": "print"}}, "exactly one role"),
    ({"roles": {"red": "dance"}}, "not one of"),
    ({"leds": {"channels": {"red": 16}}}, "0-15"),
    ({"leds": {"invert": "yes"}}, "true/false"),
    ({"audio": {"speaker": {"max_fraction": 1.5}}}, "max_fraction"),
    ({"app": {"shutdown_command": []}}, "must not be empty"),
    ({"buttons": {"chord": ["red", "red"]}}, "two different"),
])
def test_bad_config_is_an_error(data, msg):
    with pytest.raises(ConfigError, match=msg):
        config_from_dict(data, env={})


def test_bad_toml_names_the_file(tmp_path):
    p = tmp_path / "box.toml"
    p.write_text("[leds\n")
    with pytest.raises(ConfigError, match="box.toml"):
        load_app_config(p, env={})
