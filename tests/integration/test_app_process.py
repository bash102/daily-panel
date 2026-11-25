"""`python -m zozobox.app --fake` as a real process: it builds every backend
(fakes), runs, and shuts down cleanly on SIGTERM and SIGINT."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zozobox.store import Store

REPO = Path(__file__).resolve().parents[2]


def _start(data: Path) -> subprocess.Popen:
    env = dict(os.environ, PYTHONUNBUFFERED="1", ZOZOBOX_CONFIG=str(data / "absent.toml"))
    env.pop("ZOZOBOX_DATA_DIR", None)
    return subprocess.Popen(
        [sys.executable, "-m", "zozobox.app", "--fake", "--no-keys", "--data-dir", str(data)],
        cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)


def _wait_ready(proc: subprocess.Popen, timeout: float = 30) -> list[str]:
    lines: list[str] = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line)
        if "engine ready" in line:
            return lines
    proc.kill()
    raise AssertionError("never became ready:\n" + "".join(lines))


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_fake_app_stops_cleanly_on_signal(tmp_path, sig):
    data = tmp_path / "data"
    proc = _start(data)
    try:
        ready = _wait_ready(proc)
        assert "leds=fake, buttons=fake, audio=fake, printer=fake" in ready[-1]
        proc.send_signal(sig)
        out, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    assert "engine stopped" in out and "Traceback" not in out
    assert (data / "chimes" / "click.wav").is_file()
    with Store(data) as s:  # the DB was closed cleanly and opens again
        assert s.settings.volume is not None
