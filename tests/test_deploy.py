"""Static checks of deploy/ for the pi-ops deployment (no root, no systemd).

deploy/pi.toml is checked twice: against a self-contained copy of pi-ops'
manifest rules (always), and with pi-ops' own gitdeploy package when a pi-ops
checkout sits next to this repo (or at $PI_OPS_DIR)."""

from __future__ import annotations

import importlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
MANIFEST = DEPLOY / "pi.toml"
PI_OPS = Path(os.environ.get("PI_OPS_DIR") or REPO.parent / "pi-ops")
SERVICE_USER = "bash102"
APP_DIR = Path(f"/home/{SERVICE_USER}/apps/daily-panel")
# Ports already served on the pi-ops Pi: dsdigest 8000/8443, emporia-data 8787/8788.
PORTS_IN_USE = {8000, 8443, 8787, 8788}


def manifest() -> dict:
    return tomllib.loads(MANIFEST.read_text())


# ------------------------------------------------ pi.toml: pi-ops rules ----

REQUIRED = ("service", "start", "port", "health", "serve_port")
OPTIONAL = ("install", "memory_max", "autocommit", "env", "env_file")
ENV_FILE_RE = re.compile(r"(/|~/)[A-Za-z0-9._/@+-]+")


def test_manifest_follows_the_pi_ops_rules():
    """The key rules of pi-ops' gitdeploy/manifest.py, restated so they are
    checked even without a pi-ops checkout."""
    m = manifest()
    assert not set(m) - set(REQUIRED) - set(OPTIONAL), "unknown keys are an error in pi-ops"
    assert all(k in m for k in REQUIRED)
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", m["service"])
    for key in ("start", "health", "install", "memory_max"):
        assert isinstance(m[key], str) and m[key].strip()
    for key in ("port", "serve_port"):
        assert type(m[key]) is int and 1 <= m[key] <= 65535
    assert m["serve_port"] != 443, "443 is the public Funnel"
    assert isinstance(m["autocommit"], list)
    assert isinstance(m["env"], dict) and all(isinstance(v, str) for v in m["env"].values())
    assert ENV_FILE_RE.fullmatch(m["env_file"]) and ".." not in PurePosixPath(m["env_file"]).parts


def test_manifest_values():
    m = manifest()
    assert m["service"] == "zozobox"
    assert m["start"] == ".venv/bin/python -m zozobox.run"
    assert (m["port"], m["serve_port"]) == (8791, 8792)
    assert not {m["port"], m["serve_port"]} & PORTS_IN_USE
    assert m["health"] == "http://127.0.0.1:8791/healthz"
    assert m["autocommit"] == []  # nothing is committed back; read-only deploy key
    assert m["env_file"] == "~/.config/zozobox/env"
    assert m["install"] == ("python3 -m venv --system-site-packages .venv && "
                            ".venv/bin/pip install -q -e '.[pi]'")
    mem = re.fullmatch(r"(\d+)([MG])", m["memory_max"])
    mb = int(mem.group(1)) * (1024 if mem.group(2) == "G" else 1)
    assert 400 <= mb <= 700, "the whole box on a 1 GB Pi 3, with room for the OS"


def test_start_command_resolves_inside_the_venv():
    # pi-ops resolves a relative first word against the clone, never PATH.
    argv = shlex.split(manifest()["start"])
    assert argv[0] == ".venv/bin/python" and argv[1:] == ["-m", "zozobox.run"]
    assert importlib.util.find_spec("zozobox.run") is not None
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["zozobox-run"] == "zozobox.run:main"


def test_env_is_non_secret_and_matches_the_port(monkeypatch, tmp_path):
    m = manifest()
    env = m["env"]
    for k, v in env.items():
        assert not re.search(r"KEY|TOKEN|SECRET|PASSWORD|CLOUD_URL", k), f"{k} belongs in env_file"
        assert "~" not in v, "systemd Environment= does not expand ~"
    assert "ZOZOBOX_DATA_DIR" not in env and "ZOZOBOX_CONFIG" not in env
    # The supervisor, given exactly this environment, binds the dashboard where
    # pi-ops health-checks and serves it.
    from zozobox.run import Settings, default_specs

    monkeypatch.setenv("HOME", str(tmp_path))
    s = Settings.from_env(env)
    assert (s.dashboard_host, s.dashboard_port) == ("127.0.0.1", m["port"])
    assert m["health"].startswith(f"http://{s.dashboard_host}:{s.dashboard_port}/")
    argv = default_specs(s)["dashboard"].argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert s.data_dir == tmp_path / ".local" / "share" / "zozobox"
    assert s.config == tmp_path / ".config" / "zozobox" / "box.toml"
    assert m["env_file"] == "~/.config/zozobox/env"  # next to box.toml, outside the clone


def test_venv_rerun_enables_system_site_packages(tmp_path):
    """git-deploy creates .venv with plain `python3 -m venv .venv`; the install
    command re-runs venv with --system-site-packages over it. That must switch
    the existing venv to see the system (apt) packages, keeping what is installed."""
    venv = tmp_path / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    cfg = venv / "pyvenv.cfg"
    assert "include-system-site-packages = false" in cfg.read_text()
    marker = next(venv.glob("lib/python3*/site-packages")) / "kept.pth"
    marker.write_text("")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages",
                    str(venv)], check=True)
    assert "include-system-site-packages = true" in cfg.read_text()
    assert marker.exists()
    py = venv / "bin" / "python"
    out = subprocess.run([str(py), "-c", "import sys, site; print(site.ENABLE_USER_SITE is not None,"
                          " any(p.startswith(sys.base_prefix) and p.endswith(('site-packages',"
                          " 'dist-packages')) for p in sys.path))"],
                         capture_output=True, text=True, check=True).stdout.split()
    assert out[1] == "True", "system site-packages not on sys.path"


# --------------------------------------------- pi.toml: pi-ops itself ----


@pytest.fixture
def gitdeploy(monkeypatch):
    if not (PI_OPS / "gitdeploy" / "manifest.py").is_file():
        pytest.skip(f"no pi-ops checkout at {PI_OPS}")
    monkeypatch.syspath_prepend(str(PI_OPS))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # never write into the pi-ops checkout
    for name in [n for n in sys.modules if n == "gitdeploy" or n.startswith("gitdeploy.")]:
        monkeypatch.delitem(sys.modules, name)
    yield (importlib.import_module("gitdeploy.manifest"), importlib.import_module("gitdeploy.units"))
    for name in [n for n in sys.modules if n == "gitdeploy" or n.startswith("gitdeploy.")]:
        del sys.modules[name]


def test_manifest_is_valid_for_pi_ops(gitdeploy, monkeypatch):
    manifest_mod, units = gitdeploy
    m = manifest_mod.load_manifest(REPO)
    assert (m.service, m.port, m.serve_port, m.autocommit) == ("zozobox", 8791, 8792, ())
    assert units.unit_name(m) == "app-zozobox.service"
    monkeypatch.setenv("HOME", f"/home/{SERVICE_USER}")
    unit = units.render_unit(m, APP_DIR, SERVICE_USER)
    assert f"User={SERVICE_USER}" in unit and f"WorkingDirectory={APP_DIR}" in unit
    assert f'ExecStart="{APP_DIR}/.venv/bin/python" "-m" "zozobox.run"' in unit
    assert f"EnvironmentFile=/home/{SERVICE_USER}/.config/zozobox/env" in unit
    assert 'Environment="ZOZOBOX_DASHBOARD_HOST=127.0.0.1"' in unit
    assert "MemoryMax=600M" in unit and "Restart=on-failure" in unit
    # The secrets file is outside the clone (pi-ops blocks it otherwise).
    env_file = manifest_mod.resolve_env_file(m)
    assert APP_DIR not in env_file.parents


def test_pi_ops_rejects_what_the_local_rules_reject(gitdeploy):
    manifest_mod, _ = gitdeploy
    text = MANIFEST.read_text()
    with pytest.raises(manifest_mod.ManifestError):
        manifest_mod.parse_manifest('mystery = "x"\n' + text)  # before [env]
    with pytest.raises(manifest_mod.ManifestError):
        manifest_mod.parse_manifest(text.replace("serve_port = 8792", "serve_port = 443"))


# ------------------------------------------------- nothing in the clone ----


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(),
                    reason="not a git checkout")
@pytest.mark.parametrize("path", [".venv/bin/python", "zozobox.egg-info/PKG-INFO",
                                  "zozobox/__pycache__/run.cpython-313.pyc", "build/lib/x.py",
                                  "dist/zozobox.whl", ".lgd-nfy0", ".pytest_cache/v",
                                  "cloud/worker/node_modules/x/index.js"])
def test_gitignore_covers_install_and_runtime_files(path):
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", path])
    assert r.returncode == 0, f"{path} would block git-deploy (worktree has local changes)"


def test_runtime_data_defaults_are_outside_the_clone(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("ZOZOBOX_DATA_DIR", "ZOZOBOX_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    from zozobox import paths
    from zozobox.boxconfig import load_app_config

    assert paths.default_data_dir() == tmp_path / ".local/share/zozobox"
    cfg = load_app_config()
    for d in (cfg.data_dir, cfg.soundboard_dir, cfg.chimes_dir, cfg.phrases_dir):
        assert tmp_path in d.parents and REPO not in d.parents


def test_standalone_install_is_gone():
    assert not (DEPLOY / "install.sh").exists() and not (DEPLOY / "systemd").exists()
    assert not (DEPLOY / "secrets.env.example").exists()
    stale = re.compile(r"install\.sh|/opt/zozobox|/etc/zozobox|/var/lib/zozobox|secrets\.env|"
                       r"zozobox-[a-z-]+\.(service|timer)|-u '?zozobox|"
                       r"systemctl [a-z-]+ .*\bzozobox-(engine|dashboard|sync|nightly|bootstrap|mood)")
    files = [REPO / "README.md", REPO / "SPEC.md", *(REPO / "docs").glob("*.md"),
             *DEPLOY.iterdir(), *(DEPLOY / "udev").iterdir(), *(DEPLOY / "sudoers").iterdir(),
             *(REPO / "zozobox").rglob("*.py")]
    hits = []
    for f in files:
        if f.is_dir() or f.name == "DEPLOY.md":  # DEPLOY.md has a migration section
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            # host-setup.sh's warning about the old units names them on purpose
            if stale.search(line) and "disable --now zozobox-" not in line \
                    and "rm /etc/systemd/system/zozobox-" not in line:
                hits.append(f"{f.relative_to(REPO)}:{i}: {line.strip()}")
    assert not hits, "\n".join(hits)


# ------------------------------------------------------- host-setup.sh ----

HOST_SETUP = DEPLOY / "host-setup.sh"


def test_host_setup_script():
    text = HOST_SETUP.read_text()
    r = subprocess.run(["bash", "-n", str(HOST_SETUP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert os.access(HOST_SETUP, os.X_OK)
    for pkg in ("python3-venv", "python3-dev", "python3-lgpio", "alsa-utils", "ffmpeg",
                "libusb-1.0-0", "fonts-dejavu-core", "i2c-tools"):
        assert pkg in text
    assert "swig liblgpio-dev" in text  # pip's lgpio fallback on plain Debian
    assert "SERVICE_USER=${SUDO_USER:-bash102}" in text and "--user)" in text
    # I2C: raspi-config when present, else config.txt
    assert "raspi-config nonint do_i2c 0" in text and "dtparam=i2c_arm=on" in text
    assert "/boot/firmware/config.txt /boot/config.txt" in text
    for g in ("gpio", "i2c", "audio", "lp", "plugdev"):
        assert g in text.split("GROUPS_WANTED=(")[1].split(")")[0]
    assert "99-zozobox-gpio-i2c.rules" in text and "99-com.rules" in text
    assert "99-zozobox-ql800.rules" in text
    # never overwrites the box's config, secrets or asound.conf
    assert "if [[ ! -e $CONF/box.toml ]]" in text and "if [[ ! -e $CONF/env ]]" in text
    assert "/etc/asound.conf.zozobox-new" in text
    assert 'install -m 0600 -o "$SERVICE_USER"' in text and "$DEPLOY/env.example" in text
    assert 'chmod 0700 "$CONF" "$DATA"' in text
    assert "CONF=$USER_HOME/.config/zozobox" in text and "DATA=$USER_HOME/.local/share/zozobox" in text
    # sudoers: rendered for the service user and checked before install
    assert 'visudo -cf "$tmp"' in text and "/etc/sudoers.d/zozobox-poweroff" in text


def test_host_setup_review_fixes():
    text = HOST_SETUP.read_text()
    # pip's lgpio build needs a compiler; a failed install is recovered with
    # unblock + sync (no app-zozobox unit exists before the first good install)
    assert "build-essential swig liblgpio-dev" in text
    assert "git-deploy unblock daily-panel && git-deploy sync daily-panel" in text
    assert "lg, then: sudo systemctl restart" not in text
    # I2C on plain Debian: base-DT parameter in its own [all] section, also in
    # raspi-firmware's custom file, idempotent by marker or /dev/i2c-1
    assert r"[all]\ndtoverlay=\ndtparam=i2c_arm=on" in text
    assert "/etc/default/raspi-firmware-custom" in text and "/dev/i2c-1" in text
    assert 'grep -qF "$I2C_MARK"' in text
    assert "if [[ $SERVICE_USER != bash102 ]]" in text
    assert "NTPSynchronized" in text


def test_deploy_doc_review_fixes():
    doc = (REPO / "docs" / "DEPLOY.md").read_text()
    assert "The service user must be `bash102`" in doc and "<user>" not in doc
    assert "OOMPolicy=stop" in doc and "MemoryPeak" in doc
    assert "git-deploy unblock daily-panel`, then `git-deploy sync daily-panel`. With no good" in doc
    assert "build-essential swig liblgpio-dev" in doc
    assert "root-equivalent" in doc and "accepted risk" in doc
    assert "fake-hwclock` keeps the time" not in doc
    assert "root-equivalent" in SUDOERS.read_text()


def test_host_setup_placeholder_check_matches_the_template():
    regex = re.search(r"PLACEHOLDER_RE='([^']+)'", HOST_SETUP.read_text()).group(1)
    tmpl = (DEPLOY / "env.example").read_text()
    r = subprocess.run(["grep", "-Ec", regex], input=tmpl, capture_output=True, text=True)
    assert r.stdout.strip() == "4"
    filled = "ANTHROPIC_API_KEY=a\nOPENAI_API_KEY=b\nZOZOBOX_CLOUD_URL=https://x.y.workers.dev\n" \
             "ZOZOBOX_BOX_TOKEN=c\n# ZOZOBOX_TTS_VOICE=<voice>\n"
    r = subprocess.run(["grep", "-Ec", regex], input=filled, capture_output=True, text=True)
    assert r.stdout.strip() == "0"


def test_env_template_has_only_placeholders():
    text = (DEPLOY / "env.example").read_text()
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert not any(ln.startswith("export ") for ln in lines)
    keys = dict(ln.split("=", 1) for ln in lines)
    assert set(keys) == {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ZOZOBOX_CLOUD_URL",
                         "ZOZOBOX_BOX_TOKEN"}
    # Obvious placeholders: no resolvable host the token could be sent to, and
    # the supervisor skips sync/content while they are placeholders.
    assert set(keys.values()) == {"replace-me"}
    from zozobox.run import ai_problem, cloud_problem

    assert ai_problem(keys) and cloud_problem(keys)


# --------------------------------------------------------- sudoers ----

SUDOERS = DEPLOY / "sudoers" / "zozobox-poweroff"


def render_sudoers(user: str) -> str:
    return re.sub(r"^SERVICE_USER ", f"{user} ", SUDOERS.read_text(), flags=re.M)


def test_sudoers_template_allows_only_poweroff():
    lines = [ln for ln in SUDOERS.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines == ["SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff"]
    rendered = [ln for ln in render_sudoers(SERVICE_USER).splitlines()
                if ln.strip() and not ln.startswith("#")]
    assert rendered == [f"{SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff"]
    # host-setup.sh renders it with the same substitution
    assert "sed \"s/^SERVICE_USER /$SERVICE_USER /\"" in HOST_SETUP.read_text()
    from zozobox.boxconfig import DEFAULT_SHUTDOWN

    assert DEFAULT_SHUTDOWN == ("sudo", "-n", "/usr/bin/systemctl", "poweroff")


@pytest.mark.skipif(shutil.which("visudo") is None, reason="no visudo")
def test_rendered_sudoers_syntax(tmp_path):
    f = tmp_path / "zozobox-poweroff"
    f.write_text(render_sudoers(SERVICE_USER))
    f.chmod(0o440)
    r = subprocess.run(["visudo", "-cf", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ---------------------------------------------------- udev and ALSA ----


def test_udev_rule_matches_ql800():
    text = (DEPLOY / "udev" / "99-zozobox-ql800.rules").read_text()
    assert 'ATTR{idVendor}=="04f9"' in text and 'ATTR{idProduct}=="209b"' in text
    assert 'GROUP="lp"' in text


def test_udev_rule_for_gpio_and_i2c_on_plain_debian():
    text = (DEPLOY / "udev" / "99-zozobox-gpio-i2c.rules").read_text()
    assert re.search(r'SUBSYSTEM=="gpio", KERNEL=="gpiochip\[0-9\]\*", GROUP="gpio", MODE="0660"', text)
    assert re.search(r'SUBSYSTEM=="i2c-dev", KERNEL=="i2c-\[0-9\]\*", GROUP="i2c", MODE="0660"', text)


def test_asound_conf_caps_both_outputs():
    text = (DEPLOY / "asound.conf").read_text()
    for name in ("zozobox_handset", "zozobox_speaker"):
        assert re.search(rf"pcm\.{name}\s*\{{", text)
        block = text[text.index(f"pcm.{name}_capped"):]
        block = block[:block.index("\n}")]
        assert "type softvol" in block and re.search(r"max_dB\s+-\d", block)
    assert text.count("{") == text.count("}")


def test_box_toml_template_loads(tmp_path):
    from zozobox.boxconfig import load_app_config

    cfg = load_app_config(DEPLOY / "box.toml.example", required=True, env={})
    assert cfg.box.leds.invert is True


def test_engine_exit_code_for_config_errors_is_2(tmp_path):
    # The supervisor treats 2 as "fix the config" (no hot restart loop).
    from zozobox.app import main
    from zozobox.run import Settings, default_specs

    bad = tmp_path / "box.toml"
    bad.write_text("[buttons]\npinz = {}\n")
    assert main(["--config", str(bad), "--check-config"]) == 2
    specs = default_specs(Settings(data_dir=tmp_path, config=bad))
    assert 2 in specs["engine"].config_exit_codes and 2 in specs["dashboard"].config_exit_codes
