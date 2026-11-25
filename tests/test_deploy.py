"""Static checks of deploy/ (no root, no systemd needed)."""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
UNITS = sorted((DEPLOY / "systemd").glob("*.service"))
TIMERS = sorted((DEPLOY / "systemd").glob("*.timer"))
NNP_IMPLIERS = ("SystemCallFilter", "SystemCallArchitectures", "RestrictAddressFamilies",
                "RestrictNamespaces", "PrivateDevices", "ProtectKernelTunables",
                "ProtectKernelModules", "ProtectKernelLogs", "ProtectClock",
                "MemoryDenyWriteExecute", "RestrictRealtime", "RestrictSUIDSGID",
                "DynamicUser", "LockPersonality")


def unit(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    cp.optionxform = str
    cp.read_string(path.read_text())
    return cp


def test_expected_units_exist():
    names = {p.name for p in UNITS + TIMERS}
    assert {"zozobox-engine.service", "zozobox-dashboard.service", "zozobox-sync.service",
            "zozobox-nightly.service", "zozobox-nightly.timer",
            "zozobox-mood-reprocess.service", "zozobox-mood-reprocess.timer"} <= names


@pytest.mark.parametrize("path", UNITS, ids=lambda p: p.name)
def test_service_basics(path):
    s = unit(path)["Service"]
    assert s["User"] == "zozobox"
    assert s["EnvironmentFile"] == "/etc/zozobox/secrets.env"
    assert s["WorkingDirectory"] == "/var/lib/zozobox"
    assert "/var/lib/zozobox" in s["ReadWritePaths"]
    assert s["ExecStart"].startswith("/opt/zozobox/venv/bin/python -m zozobox.")
    assert s["StandardOutput"] == "journal"
    if s["Type"] == "simple":
        assert s["Restart"] in ("always", "on-failure")


def test_engine_can_sudo_poweroff():
    s = unit(DEPLOY / "systemd" / "zozobox-engine.service")["Service"]
    assert s.get("NoNewPrivileges", "no") == "no"
    for opt in NNP_IMPLIERS:
        assert opt not in s, f"{opt} implies NoNewPrivileges and breaks sudo"
    assert s["ProtectSystem"] == "full"  # strict would make /var read-only for sudo


@pytest.mark.parametrize("name", ["zozobox-dashboard.service", "zozobox-sync.service",
                                  "zozobox-nightly.service", "zozobox-mood-reprocess.service"])
def test_other_units_are_hardened(name):
    s = unit(DEPLOY / "systemd" / name)["Service"]
    assert s["NoNewPrivileges"] == "yes" and s["ProtectSystem"] == "strict"


def test_dashboard_uses_the_locked_printer():
    s = unit(DEPLOY / "systemd" / "zozobox-dashboard.service")["Service"]
    assert "--printer brother" in s["ExecStart"]


def test_timers():
    n = unit(DEPLOY / "systemd" / "zozobox-nightly.timer")["Timer"]
    assert n["OnCalendar"].endswith("02:30:00") and "RandomizedDelaySec" in n
    m = unit(DEPLOY / "systemd" / "zozobox-mood-reprocess.timer")["Timer"]
    assert m["OnCalendar"] == "hourly"
    assert unit(DEPLOY / "systemd" / "zozobox-mood-reprocess.service")["Service"][
        "SuccessExitStatus"] == "2"


def test_sudoers_allows_only_poweroff():
    lines = [ln for ln in (DEPLOY / "sudoers" / "zozobox-poweroff").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert lines == ["zozobox ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff"]
    from zozobox.boxconfig import DEFAULT_SHUTDOWN
    assert " ".join(DEFAULT_SHUTDOWN[2:]) == "/usr/bin/systemctl poweroff"


@pytest.mark.skipif(shutil.which("visudo") is None, reason="no visudo")
def test_sudoers_syntax():
    r = subprocess.run(["visudo", "-cf", str(DEPLOY / "sudoers" / "zozobox-poweroff")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_udev_rule_matches_ql800():
    text = (DEPLOY / "udev" / "99-zozobox-ql800.rules").read_text()
    assert 'ATTR{idVendor}=="04f9"' in text and 'ATTR{idProduct}=="209b"' in text
    assert 'GROUP="lp"' in text


def test_asound_conf_caps_both_outputs():
    text = (DEPLOY / "asound.conf").read_text()
    for name in ("zozobox_handset", "zozobox_speaker"):
        assert re.search(rf"pcm\.{name}\s*\{{", text)
        block = text[text.index(f"pcm.{name}_capped"):]
        block = block[:block.index("\n}")]
        assert "type softvol" in block and re.search(r"max_dB\s+-\d", block)
    assert text.count("{") == text.count("}")


def test_secrets_template_has_only_placeholders():
    text = (DEPLOY / "secrets.env.example").read_text()
    keys = dict(ln.split("=", 1) for ln in text.splitlines()
                if ln and not ln.startswith("#"))
    assert set(keys) == {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ZOZOBOX_CLOUD_URL",
                         "ZOZOBOX_BOX_TOKEN"}
    # Every value is an obvious placeholder: no resolvable host the token could
    # be sent to (sync refuses placeholders), and install.sh warns on each.
    assert set(keys.values()) == {"replace-me"}


@pytest.mark.parametrize("name", ["zozobox-engine.service", "zozobox-dashboard.service"])
def test_config_errors_do_not_restart_loop(name):
    u = unit(DEPLOY / "systemd" / name)
    assert u["Service"]["Restart"] == "always"
    assert u["Service"]["RestartPreventExitStatus"] == "2"
    assert "ZOZOBOX_CONFIG=/etc/zozobox/box.toml" in (DEPLOY / "systemd" / name).read_text()
    assert "time-sync.target" in u["Unit"]["After"]


def test_engine_exit_code_for_config_errors_is_2(tmp_path):
    from zozobox.app import main

    bad = tmp_path / "box.toml"
    bad.write_text("[buttons]\npinz = {}\n")
    assert main(["--config", str(bad), "--check-config"]) == 2


def test_install_script():
    text = (DEPLOY / "install.sh").read_text()
    r = subprocess.run(["bash", "-n", str(DEPLOY / "install.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for pkg in ("python3-venv", "python3-dev", "alsa-utils", "ffmpeg", "libusb-1.0-0",
                "fonts-dejavu-core", "i2c-tools", "git"):
        assert pkg in text
    assert "raspi-config nonint do_i2c 0" in text
    assert "PREFIX=/opt/zozobox" in text and "VENV=$PREFIX/venv" in text and '-e "$SRC[pi]"' in text
    # never overwrites the box's own config or secrets
    assert '[[ ! -e $ETC/box.toml ]]' in text and '[[ ! -e $ETC/secrets.env ]]' in text
    assert "systemctl daemon-reload" in text and "visudo -cf" in text
    for u in UNITS + TIMERS:
        assert u.name in text, u.name
    # offline re-runs skip pip too; bytecode is compiled for the read-only src
    assert "--no-pip) DO_PIP=0" in text and "--offline) DO_APT=0; DO_PIP=0" in text
    assert 'if [[ $DO_PIP -eq 1 ]]; then' in text
    assert '-m compileall -q "$SRC/zozobox"' in text
    # the source copy is swapped in whole, so removed files do not linger
    assert 'mktemp -d "$PREFIX/.src-new.XXXXXX"' in text and 'mv "$new" "$SRC"' in text
    # timers start on a re-run once the secrets are filled in
    assert "systemctl start zozobox-nightly.timer zozobox-mood-reprocess.timer" in text


def test_install_placeholder_check_matches_the_template(tmp_path):
    text = (DEPLOY / "install.sh").read_text()
    regex = re.search(r"PLACEHOLDER_RE='([^']+)'", text).group(1)
    tmpl = (DEPLOY / "secrets.env.example").read_text()
    r = subprocess.run(["grep", "-Ec", regex], input=tmpl, capture_output=True, text=True)
    assert r.stdout.strip() == "4"
    filled = "ANTHROPIC_API_KEY=a\nOPENAI_API_KEY=b\nZOZOBOX_CLOUD_URL=https://x.y.workers.dev\n" \
             "ZOZOBOX_BOX_TOKEN=c\n# ZOZOBOX_TTS_VOICE=<voice>\n"
    r = subprocess.run(["grep", "-Ec", regex], input=filled, capture_output=True, text=True)
    assert r.stdout.strip() == "0"


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_units_verify():
    r = subprocess.run(["systemd-analyze", "verify", "--man=no",
                        *map(str, UNITS + TIMERS)], capture_output=True, text=True)
    bad = [ln for ln in (r.stdout + r.stderr).splitlines()
           if ln.strip() and "is not executable" not in ln and "/opt/zozobox" not in ln]
    assert not bad, "\n".join(bad)
