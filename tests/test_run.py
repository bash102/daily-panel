"""zozobox.run: the supervisor pi-ops runs as app-zozobox.service.

Scheduling and restart logic run against fake processes and a fake clock;
process handling (output prefix, working directory, signals, killing
stragglers) against real `python -c` children. No hardware, no network."""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from zozobox import run
from zozobox.run import Settings, Spec, Supervisor, last_scheduled, nightly_due

NY = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[1]
KEYS = {"ANTHROPIC_API_KEY": "sk-ant-x", "OPENAI_API_KEY": "sk-x",
        "ZOZOBOX_CLOUD_URL": "https://zozo.example.net", "ZOZOBOX_BOX_TOKEN": "t0ken"}
PY = sys.executable


def local(*a) -> datetime:
    return datetime(*a, tzinfo=NY)


# ------------------------------------------------------------- fakes ----


class FakeProc:
    def __init__(self, name: str, argv, capture: bool):
        self.name = name
        self.argv = list(argv)
        self.capture = capture
        self.pid = 4242
        self.rc: int | None = None
        self.out = ""
        self.signals: list[int] = []
        self.killed = False
        self.ignore_term = False
        self.nice = 0
        self.data_limit_mb = 0

    def poll(self):
        return self.rc

    def finish(self, timeout: float = 1.0) -> None:
        pass

    def close(self) -> None:
        pass

    def output(self) -> str:
        return self.out

    def signal(self, sig: int) -> None:
        self.signals.append(sig)
        if not self.ignore_term:
            self.rc = -sig

    def signal_group(self, sig: int) -> None:
        self.signal(sig)

    def kill_group(self) -> None:
        self.killed = True
        self.rc = -9

    @property
    def popen(self):
        return self

    def wait(self, timeout=None):
        return self.rc


class Harness:
    """A Supervisor with fake processes, a fake monotonic clock and a fake wall clock."""

    def __init__(self, tmp_path: Path, *, env=None, wall=None, specs=None, synced=True,
                 head="aaaa", **settings):
        self.t = 1000.0
        self.synced = synced
        self.head = head
        self.wall = wall or local(2026, 11, 25, 12, 0)
        s = Settings(data_dir=tmp_path / "data", config=tmp_path / "box.toml", python=PY,
                     tz=NY, mood_first_s=300, bootstrap_first_s=60, start_stagger_s=0)
        for k, v in settings.items():
            setattr(s, k, v)
        if specs is None:
            specs = {n: Spec(n, [n], frozenset({2})) for n in ("dashboard", "engine")}
            specs["sync"] = Spec("sync", ["sync"], frozenset({3}))
        s.job_ionice = False
        self.sup = Supervisor(s, env=KEYS if env is None else env, specs=specs,
                              clock=lambda: self.t, now=lambda: self.wall, out=io.StringIO(),
                              clock_synced=lambda: self.synced, code_version=lambda: self.head)
        self.spawned: list[FakeProc] = []

        def spawn(name, argv, *, capture=False, nice=0, data_limit_mb=0):
            p = FakeProc(name, argv, capture)
            p.nice, p.data_limit_mb = nice, data_limit_mb
            self.spawned.append(p)
            return p

        self.sup._spawn = spawn

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.wall += timedelta(seconds=seconds)

    def started(self, name: str) -> list[FakeProc]:
        return [p for p in self.spawned if p.name == name]

    def job(self, name: str) -> FakeProc:
        return self.sup.jobs[name].proc

    def finish_job(self, name: str, rc: int = 0, out: str = "") -> None:
        p = self.job(name)
        p.out, p.rc = out, rc
        self.sup.step()

    def status(self, missing=(), pending=()) -> str:
        return json.dumps({"phrases_missing": list(missing), "phrases_pending": list(pending)})


# ------------------------------------------------- restart and backoff ----


def test_starts_dashboard_first_then_the_rest(tmp_path):
    h = Harness(tmp_path, start_stagger_s=3)
    h.sup.start()
    assert [p.name for p in h.spawned] == ["dashboard"]
    h.advance(3)
    h.sup.step()
    assert sorted(p.name for p in h.spawned) == ["dashboard", "engine", "sync"]


def test_crash_restarts_with_exponential_backoff_capped(tmp_path, caplog):
    h = Harness(tmp_path, backoff_max_s=60)
    h.sup.start()
    svc = h.sup.services["engine"]
    delays = []
    for _ in range(8):
        svc.proc.rc = 1
        h.sup.step()
        assert svc.proc is None
        delays.append(svc.next_start - h.t)
        h.advance(delays[-1] - 0.01)
        h.sup.step()
        assert svc.proc is None  # not before the backoff is over
        h.advance(0.01)
        h.sup.step()
        assert svc.proc is not None
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
    assert len(h.started("engine")) == 9
    assert "engine exited 1" in caplog.text and "restart #8" in caplog.text


def test_backoff_resets_after_a_stable_run(tmp_path):
    h = Harness(tmp_path, stable_s=60)
    h.sup.start()
    svc = h.sup.services["engine"]
    for _ in range(3):
        svc.proc.rc = 1
        h.sup.step()
        h.advance(svc.next_start - h.t)
        h.sup.step()
    assert svc.failures == 3
    h.advance(61)  # ran fine for a minute
    h.sup.step()
    assert svc.failures == 0
    svc.proc.rc = 1
    h.sup.step()
    assert svc.next_start - h.t == 1


def test_config_error_exit_is_not_hot_looped(tmp_path, caplog):
    h = Harness(tmp_path, config_retry_s=300)
    h.sup.start()
    svc = h.sup.services["engine"]
    svc.proc.rc = 2  # bad box.toml
    h.sup.step()
    assert svc.proc is None and svc.next_start == h.t + 300
    assert "configuration or store error" in caplog.text
    h.advance(120)
    h.sup.step()
    assert svc.proc is None and len(h.started("engine")) == 1
    h.advance(180)
    h.sup.step()
    assert svc.proc is not None and len(h.started("engine")) == 2
    # sync's config error status is 3; a plain crash (1, an uncaught
    # exception) is restarted with backoff, not parked for 5 minutes
    sync = h.sup.services["sync"]
    sync.proc.rc = 1
    h.sup.step()
    assert sync.next_start == h.t + 1
    h.advance(1)
    h.sup.step()
    sync.proc.rc = 3
    h.sup.step()
    assert sync.next_start == h.t + 300


def test_other_children_keep_running_when_one_crashes(tmp_path):
    h = Harness(tmp_path)
    h.sup.start()
    dash = h.sup.services["dashboard"].proc
    h.sup.services["engine"].proc.rc = -11
    h.sup.step()
    assert h.sup.services["dashboard"].proc is dash and dash.rc is None


# ---------------------------------------------------------- secrets ----


def test_missing_secrets_skip_jobs_and_sync_but_not_engine_or_dashboard(tmp_path, caplog):
    env = {"ZOZOBOX_CLOUD_URL": "replace-me", "ZOZOBOX_BOX_TOKEN": "replace-me",
           "ANTHROPIC_API_KEY": "replace-me"}
    h = Harness(tmp_path, env=env)
    h.sup.start()
    assert sorted(p.name for p in h.spawned) == ["dashboard", "engine"]
    assert "sync" not in h.sup.services
    for job in ("nightly", "bootstrap", "content-status", "mood"):
        assert job in h.sup.skipped
    assert "child sync skipped" in caplog.text
    assert "ANTHROPIC_API_KEY is still a placeholder; OPENAI_API_KEY is not set" in caplog.text
    h.advance(48 * 3600)  # nothing is ever scheduled
    h.sup.step()
    assert sorted(p.name for p in h.spawned) == ["dashboard", "engine"] and not h.sup.jobs


def test_cloud_secrets_alone_still_run_sync(tmp_path):
    env = {"ZOZOBOX_CLOUD_URL": "https://zozo.example.net", "ZOZOBOX_BOX_TOKEN": "abc"}
    h = Harness(tmp_path, env=env)
    h.sup.start()
    assert "sync" in h.sup.services and "mood" in h.sup.skipped


def test_plain_http_cloud_url_is_refused(tmp_path):
    h = Harness(tmp_path, env={**KEYS, "ZOZOBOX_CLOUD_URL": "http://zozo.example.net"})
    assert "sync" in h.sup.skipped and "https" in h.sup.skipped["sync"]


# ------------------------------------------------------ nightly schedule ----


@pytest.mark.parametrize("now, last, due", [
    (local(2026, 11, 25, 9, 0), local(2026, 11, 24, 2, 40), True),    # Pi off at 02:30
    (local(2026, 11, 25, 9, 0), local(2026, 11, 25, 2, 45), False),   # ran today
    (local(2026, 11, 25, 2, 29), local(2026, 11, 24, 2, 40), False),  # not yet
    (local(2026, 11, 25, 2, 30), local(2026, 11, 24, 2, 40), True),   # exactly then
    (local(2026, 11, 25, 1, 0), local(2026, 11, 23, 2, 40), True),    # missed yesterday's
    (local(2026, 11, 25, 9, 0), None, False),                         # never: no state yet
])
def test_nightly_due(now, last, due):
    assert nightly_due(now, last, (2, 30), NY) is due


def test_last_scheduled_uses_the_box_zone_across_dst():
    # 2026-03-08: New York springs forward at 02:00; 2026-11-01: falls back.
    assert last_scheduled(local(2026, 3, 9, 1, 0), (2, 30), NY).date().isoformat() == "2026-03-08"
    s = last_scheduled(local(2026, 11, 1, 12, 0), (2, 30), NY)
    assert (s.hour, s.minute) == (2, 30) and s.utcoffset() == timedelta(hours=-5)
    s = last_scheduled(local(2026, 7, 1, 12, 0), (2, 30), NY)
    assert s.utcoffset() == timedelta(hours=-4)
    assert s.astimezone(timezone.utc).hour == 6


def test_first_start_waits_for_the_next_0230(tmp_path):
    h = Harness(tmp_path, wall=local(2026, 11, 25, 12, 0))
    h.sup.start()
    h.advance(60)
    h.sup.step()
    h.finish_job("content-status", out=h.status())
    assert "nightly" not in h.sup.jobs
    state = json.loads((tmp_path / "data" / "supervisor-state.json").read_text())
    assert state["nightly_last"].startswith("2026-11-25T12:00")
    h.advance(14 * 3600 + 30 * 60)  # 02:30 next day
    h.sup.step()
    assert "nightly" in h.sup.jobs


def write_state(tmp_path: Path, **state: datetime) -> None:
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    (d / "supervisor-state.json").write_text(json.dumps({k: v.isoformat() for k, v in state.items()}))


def test_catch_up_after_the_pi_was_off_at_0230(tmp_path):
    write_state(tmp_path, nightly_last=local(2026, 11, 23, 2, 50))
    h = Harness(tmp_path, wall=local(2026, 11, 25, 8, 15))
    h.sup.start()
    h.sup.step()
    assert "nightly" not in h.sup.jobs  # the phrases check goes first
    h.advance(60)
    h.sup.step()
    h.finish_job("content-status", out=h.status())
    assert "nightly" in h.sup.jobs
    assert h.job("nightly").argv[-2:] == ["zozobox.content", "nightly"]
    h.advance(600)
    h.finish_job("nightly", rc=1)  # some job failed: flagged, not retried today
    h.advance(3600)
    h.sup.step()
    assert "nightly" not in h.sup.jobs and len(h.started("nightly")) == 1
    state = json.loads((tmp_path / "data" / "supervisor-state.json").read_text())
    assert datetime.fromisoformat(state["nightly_last"]) > local(2026, 11, 25, 8, 15)
    h.wall = local(2026, 11, 26, 2, 31)
    h.sup.step()
    assert "nightly" in h.sup.jobs


def test_interrupted_nightly_runs_again_after_restart(tmp_path):
    write_state(tmp_path, nightly_last=local(2026, 11, 24, 2, 40))
    h = Harness(tmp_path, wall=local(2026, 11, 25, 2, 31))
    h.sup.first_check_done = True
    h.sup.start()
    assert "nightly" in h.sup.jobs
    h.sup.shutdown()  # e.g. a deploy restarts the unit mid-run
    assert h.started("nightly")[0].signals == [signal.SIGTERM]
    h2 = Harness(tmp_path, wall=local(2026, 11, 25, 2, 40))
    h2.sup.first_check_done = True
    h2.sup.start()
    assert "nightly" in h2.sup.jobs


def test_mood_reprocess_runs_hourly(tmp_path):
    h = Harness(tmp_path)
    h.sup.start()
    h.advance(299)
    h.sup.step()
    assert "mood" not in h.sup.jobs
    h.advance(1)
    h.sup.step()
    assert h.job("mood").argv[-2:] == ["zozobox.mood", "reprocess"]
    h.finish_job("mood", rc=2)  # still offline: fine
    h.advance(3599)
    h.sup.step()
    assert "mood" not in h.sup.jobs
    h.advance(1)
    h.sup.step()
    assert len(h.started("mood")) == 2


# -------------------------------------------------------------- bootstrap ----


def test_bootstrap_runs_once_when_phrases_are_missing(tmp_path, caplog):
    caplog.set_level(logging.INFO, "zozobox.run")
    h = Harness(tmp_path, bootstrap_check_every_s=3600, bootstrap_min_gap_s=12 * 3600)
    h.sup.start()
    h.advance(60)
    h.sup.step()
    assert h.job("content-status").capture
    h.finish_job("content-status", out=h.status(missing=["sleepy", "hello"]))
    assert h.job("bootstrap").argv[-2:] == ["zozobox.content", "bootstrap"]
    assert "2 fixed phrase(s) missing (hello, sleepy)" in caplog.text
    # While it runs: no second check, no second bootstrap, no nightly.
    h.wall = local(2026, 11, 26, 2, 35)
    h.advance(3600)
    h.sup.step()
    assert "content-status" not in h.sup.jobs and "nightly" not in h.sup.jobs
    assert len(h.started("bootstrap")) == 1
    h.finish_job("bootstrap", rc=1)  # spend cap / some phrase failed
    assert "nightly" in h.sup.jobs  # due since 02:30, waited for the bootstrap
    h.finish_job("nightly")
    # Phrases still missing an hour later: not again within 12 h.
    h.advance(3600)
    h.sup.step()
    h.finish_job("content-status", out=h.status(missing=["sleepy"]))
    assert "bootstrap" not in h.sup.jobs and len(h.started("bootstrap")) == 1
    # ...but again once 12 h have passed.
    h.advance(12 * 3600)
    h.sup.step()
    h.finish_job("content-status", out=h.status(missing=["sleepy"]))
    assert len(h.started("bootstrap")) == 2


def test_no_bootstrap_when_phrases_only_await_approval(tmp_path):
    h = Harness(tmp_path)
    h.sup.start()
    h.advance(60)
    h.sup.step()
    h.finish_job("content-status", out=h.status(missing=["sleepy"], pending=["sleepy"]))
    assert not h.started("bootstrap")


@pytest.mark.parametrize("rc, out", [(2, ""), (0, "not json"), (0, "[1, 2]")])
def test_bad_content_status_never_bootstraps(tmp_path, rc, out):
    h = Harness(tmp_path)
    h.sup.start()
    h.advance(60)
    h.sup.step()
    h.finish_job("content-status", rc=rc, out=out)
    assert not h.started("bootstrap")


def test_bootstrap_waits_for_a_running_nightly(tmp_path):
    write_state(tmp_path, nightly_last=local(2026, 11, 24, 2, 40))
    h = Harness(tmp_path, wall=local(2026, 11, 25, 3, 0))
    h.sup.first_check_done = True
    h.sup.start()
    assert "nightly" in h.sup.jobs
    h.sup._on_content_status(0, h.status(missing=["sleepy"]))
    assert "bootstrap" not in h.sup.jobs  # never two content runs at once


def test_bootstrap_gap_survives_a_restart(tmp_path):
    write_state(tmp_path, nightly_last=local(2026, 11, 25, 2, 40),
                bootstrap_last=local(2026, 11, 25, 10, 0))
    h = Harness(tmp_path, wall=local(2026, 11, 25, 12, 0))
    h.sup.start()
    h.advance(60)
    h.sup.step()
    h.finish_job("content-status", out=h.status(missing=["sleepy"]))
    assert not h.started("bootstrap")


# ------------------------------------- OOM restarts, limits, deadlines ----


def read_state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "data" / "supervisor-state.json").read_text())


def test_catch_up_nightly_is_not_retried_forever_after_oom_restarts(tmp_path, caplog):
    """OOMPolicy=stop: an OOM kill stops the whole unit and Restart=on-failure
    starts it again. The killed nightly never finished, so it looks due again;
    one retry is allowed, then the slot is skipped (no restart/spend loop)."""
    write_state(tmp_path, nightly_last=local(2026, 11, 24, 2, 40))
    walls = [local(2026, 11, 25, 8, 0), local(2026, 11, 25, 8, 2), local(2026, 11, 25, 8, 4)]
    starts = []
    for wall in walls:
        h = Harness(tmp_path, wall=wall)
        h.sup.first_check_done = True
        h.sup.start()
        starts.append("nightly" in h.sup.jobs)
        h.sup.shutdown()  # the unit is stopped (OOM) mid-run
    assert starts == [True, True, False]
    assert "started 2 times without finishing" in caplog.text
    assert read_state(tmp_path)["nightly_unfinished"] == "2"
    # The next scheduled time is a new slot: it runs, and a finish resets the count.
    h = Harness(tmp_path, wall=local(2026, 11, 26, 2, 31))
    h.sup.first_check_done = True
    h.sup.start()
    assert "nightly" in h.sup.jobs
    h.finish_job("nightly", rc=0)
    assert read_state(tmp_path)["nightly_unfinished"] == "0"


def test_bootstrap_is_not_retried_forever_after_oom_restarts(tmp_path):
    write_state(tmp_path, nightly_last=local(2026, 11, 25, 2, 40))
    started = []
    wall = local(2026, 11, 25, 12, 0)
    for i in range(3):
        h = Harness(tmp_path, wall=wall + timedelta(minutes=2 * i))
        h.sup.start()
        h.advance(60)
        h.sup.step()
        h.finish_job("content-status", out=h.status(missing=["sleepy"]))
        started.append("bootstrap" in h.sup.jobs)
        h.sup.shutdown()
    assert started == [True, True, False]
    # 12 h after the last start it may try once more.
    h = Harness(tmp_path, wall=wall + timedelta(hours=12, minutes=5))
    h.sup.start()
    h.advance(60)
    h.sup.step()
    h.finish_job("content-status", out=h.status(missing=["sleepy"]))
    assert "bootstrap" in h.sup.jobs


def test_mood_is_not_retried_forever_after_oom_restarts(tmp_path):
    started = []
    for i in range(3):
        h = Harness(tmp_path, wall=local(2026, 11, 25, 12, 0) + timedelta(minutes=6 * i))
        h.sup.start()
        h.advance(300)
        h.sup.step()
        started.append("mood" in h.sup.jobs)
        h.sup.shutdown()
    assert started == [True, True, False]


def test_heavy_jobs_never_overlap(tmp_path):
    write_state(tmp_path, nightly_last=local(2026, 11, 24, 2, 40))
    h = Harness(tmp_path, wall=local(2026, 11, 25, 8, 0))
    h.sup.first_check_done = True
    h.sup.start()
    assert "nightly" in h.sup.jobs
    h.advance(300)
    h.sup.step()
    assert "mood" not in h.sup.jobs  # waits for the nightly run
    h.finish_job("nightly")
    assert "mood" in h.sup.jobs  # ...and runs right after it
    # The (light) phrases check may run beside mood; a bootstrap it wants
    # waits until mood ends.
    h.finish_job("content-status", out=h.status(missing=["sleepy"]))
    assert "bootstrap" not in h.sup.jobs and h.sup.bootstrap_wanted
    h.finish_job("mood")
    assert "bootstrap" in h.sup.jobs and not h.sup.bootstrap_wanted


def test_heavy_jobs_get_a_memory_limit_and_low_priority(tmp_path):
    write_state(tmp_path, nightly_last=local(2026, 11, 24, 2, 40))
    h = Harness(tmp_path, wall=local(2026, 11, 25, 8, 0), job_memory_mb=300)
    h.sup.ionice = "/usr/bin/ionice"
    h.sup.start()
    h.advance(60)
    h.sup.step()
    status = h.job("content-status")
    assert status.data_limit_mb == 0 and status.nice == 0 and status.argv[0] != "/usr/bin/ionice"
    h.finish_job("content-status", out=h.status())
    nightly = h.job("nightly")
    assert nightly.data_limit_mb == 300 and nightly.nice == 10
    assert nightly.argv[:2] == ["/usr/bin/ionice", "-c3"]
    assert nightly.argv[-2:] == ["zozobox.content", "nightly"]
    for svc in ("engine", "dashboard", "sync"):
        assert h.started(svc)[0].data_limit_mb == 0


def test_a_stuck_job_is_stopped_at_its_deadline(tmp_path, caplog):
    h = Harness(tmp_path, stop_grace_s=25)
    h.sup.start()
    h.advance(300)
    h.sup.step()
    mood = h.job("mood")
    mood.ignore_term = True
    h.advance(1799)
    h.sup.step()
    assert mood.signals == []
    h.advance(1)
    h.sup.step()
    assert mood.signals == [signal.SIGTERM] and not mood.killed
    assert "still running after 1800 s" in caplog.text
    h.advance(25)
    h.sup.step()
    assert mood.killed
    h.sup.step()
    assert "mood" not in h.sup.jobs
    # A deadline kill is a finish: the job is not held back as interrupted.
    assert read_state(tmp_path)["mood_unfinished"] == "0"
    h.advance(3600)
    h.sup.step()
    assert len(h.started("mood")) == 2


def test_nothing_uses_the_wall_clock_until_it_is_synchronized(tmp_path, caplog):
    write_state(tmp_path, nightly_last=local(2026, 11, 23, 2, 40))
    h = Harness(tmp_path, wall=local(2026, 11, 25, 8, 0), synced=False)
    h.sup.start()
    h.advance(300)
    h.sup.step()
    assert "content-status" not in h.sup.jobs and "nightly" not in h.sup.jobs
    assert "mood" in h.sup.jobs  # no wall-clock decision in it
    assert "not synchronized" in caplog.text
    h.finish_job("mood")
    h.synced = True
    h.sup.step()
    h.finish_job("content-status", out=h.status())
    assert "nightly" in h.sup.jobs


def test_first_start_state_waits_for_the_clock(tmp_path):
    h = Harness(tmp_path, wall=local(2026, 1, 1, 0, 0), synced=False)  # stale boot clock
    h.sup.start()
    h.advance(60)
    h.sup.step()
    assert "nightly_last" not in h.sup.state
    h.wall = local(2026, 11, 25, 12, 0)  # NTP stepped the clock
    h.synced = True
    h.sup.step()
    assert read_state(tmp_path)["nightly_last"].startswith("2026-11-25T12:00")


def test_a_nightly_last_in_the_future_is_reset(tmp_path, caplog):
    write_state(tmp_path, nightly_last=local(2027, 3, 1, 2, 40))
    h = Harness(tmp_path, wall=local(2026, 11, 25, 12, 0))
    h.sup.start()
    assert read_state(tmp_path)["nightly_last"].startswith("2026-11-25T12:00")
    assert "in the future" in caplog.text
    h.wall = local(2026, 11, 26, 2, 31)
    h.sup.first_check_done = True
    h.sup.step()
    assert "nightly" in h.sup.jobs


def test_no_new_jobs_once_the_code_on_disk_changed(tmp_path, caplog):
    write_state(tmp_path, nightly_last=local(2026, 11, 24, 2, 40))
    h = Harness(tmp_path, wall=local(2026, 11, 25, 8, 0), head="old")
    h.sup.start()
    h.head = "new"  # git-deploy rebased the clone; install failed, unit not restarted
    h.advance(300)
    h.sup.step()
    assert not h.sup.jobs
    assert "code changed on disk since start" in caplog.text
    # A crashed child is still restarted, so the box keeps working.
    h.sup.services["engine"].proc.rc = 1
    h.sup.step()
    h.advance(1)
    h.sup.step()
    assert len(h.started("engine")) == 2


def test_git_head_reads_loose_and_packed_refs(tmp_path):
    g = tmp_path / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    assert run.git_head(tmp_path) is None
    (g / "packed-refs").write_text("# pack-refs\n" + "a" * 40 + " refs/heads/main\n")
    assert run.git_head(tmp_path) == "a" * 40
    (g / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    assert run.git_head(tmp_path) == "b" * 40
    (g / "HEAD").write_text("c" * 40 + "\n")  # detached
    assert run.git_head(tmp_path) == "c" * 40
    assert run.git_head(tmp_path / "nope") is None


def test_clock_synchronized_probe_returns_a_bool():
    assert run.clock_synchronized() in (True, False)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prlimit is Linux-only")
def test_real_job_memory_limit_turns_an_oom_into_a_memory_error(tmp_path):
    code = ("import resource\n"
            "print('limit', resource.getrlimit(resource.RLIMIT_DATA)[0] // (1024 * 1024))\n"
            "try:\n"
            "    b = bytearray(512 * 1024 * 1024)\n"
            "    print('allocated')\n"
            "except MemoryError:\n"
            "    print('memoryerror')\n")
    sup = real_supervisor(tmp_path, {}, job_memory_mb=128, job_ionice=False)
    sup.job_argv = {"mood": [PY, "-c", code]}
    sup.start()
    assert sup._start_job("mood")
    wait_for(lambda: "mood" not in sup.jobs, step=sup.step)
    out = sup.out.getvalue()
    assert "mood: limit 128\n" in out and "mood: memoryerror\n" in out
    sup.shutdown()


# ---------------------------------------------------------- settings ----


def test_settings_defaults_use_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = Settings.from_env({})
    assert s.data_dir == tmp_path / ".local" / "share" / "zozobox"
    assert s.config == tmp_path / ".config" / "zozobox" / "box.toml"
    assert (s.dashboard_host, s.dashboard_port, s.dashboard_printer) == ("127.0.0.1", 8791, "brother")
    assert s.secure_cookies and s.nightly_at == (2, 30) and s.tz is None
    argv = run.default_specs(s)["dashboard"].argv
    assert argv[1:] == ["-m", "zozobox.dashboard", "--host", "127.0.0.1", "--port", "8791",
                        "--printer", "brother", "--secure-cookies"]


def test_settings_from_env():
    s = Settings.from_env({"ZOZOBOX_DATA_DIR": "/d", "ZOZOBOX_CONFIG": "/c/box.toml",
                           "ZOZOBOX_DASHBOARD_PORT": "9000", "ZOZOBOX_NIGHTLY_AT": "03:15",
                           "ZOZOBOX_TZ": "Europe/Paris",
                           "ZOZOBOX_DASHBOARD_SECURE_COOKIES": "0"})
    assert s.data_dir == Path("/d") and s.config == Path("/c/box.toml")
    assert s.dashboard_port == 9000 and s.nightly_at == (3, 15) and not s.secure_cookies
    assert s.tz.key == "Europe/Paris"
    with pytest.raises(ValueError):
        Settings.from_env({"ZOZOBOX_NIGHTLY_AT": "25:00"})
    assert s.job_memory_mb == 384 and s.require_time_sync
    s = Settings.from_env({"ZOZOBOX_JOB_MEMORY_MB": "0", "ZOZOBOX_REQUIRE_TIME_SYNC": "0"})
    assert s.job_memory_mb == 0 and not s.require_time_sync
    with pytest.raises(ValueError):
        Settings.from_env({"ZOZOBOX_JOB_MEMORY_MB": "-1"})


def test_default_children_use_the_existing_clis(tmp_path):
    s = Settings(data_dir=tmp_path, config=tmp_path / "b.toml", python="/v/bin/python")
    specs = run.default_specs(s)
    assert specs["engine"].argv == ["/v/bin/python", "-m", "zozobox.app"]
    assert specs["sync"].argv == ["/v/bin/python", "-m", "zozobox.sync", "run"]
    assert specs["engine"].config_exit_codes == {2} and specs["sync"].config_exit_codes == {3}
    from zozobox.sync.__main__ import EXIT_CONFIG

    assert run.SYNC_CONFIG_EXIT == EXIT_CONFIG
    jobs = run.default_jobs(s)
    assert jobs["nightly"][1:] == ["-m", "zozobox.content", "nightly"]
    assert jobs["bootstrap"][1:] == ["-m", "zozobox.content", "bootstrap"]
    assert jobs["mood"][1:] == ["-m", "zozobox.mood", "reprocess"]


def test_plan_lists_children_and_skips(tmp_path, capsys, monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ZOZOBOX_DATA_DIR", str(tmp_path / "d"))
    assert run.main(["--plan"]) == 0
    out = capsys.readouterr().out
    assert "child engine:" in out and "child dashboard:" in out
    assert "child sync: skipped" in out and "job nightly: skipped" in out
    assert not (tmp_path / "d").exists()  # --plan changes nothing


# --------------------------------------------- real child processes ----


def real_supervisor(tmp_path: Path, specs: dict[str, Spec], **settings) -> Supervisor:
    s = Settings(data_dir=tmp_path / "data", config=tmp_path / "box.toml", python=PY,
                 start_stagger_s=0, tick_s=0.02, stop_grace_s=2.0, mood_first_s=1e9,
                 bootstrap_first_s=1e9)
    for k, v in settings.items():
        setattr(s, k, v)
    return Supervisor(s, env={**os.environ, **KEYS}, specs=specs, jobs={}, out=io.StringIO())


def wait_for(cond, timeout: float = 10.0, step=None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if step:
            step()
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_real_child_output_is_prefixed_and_runs_in_the_data_dir(tmp_path):
    code = ("import os, sys; print('hello'); print('oops', file=sys.stderr); "
            "print('cwd=' + os.getcwd()); print('data=' + os.environ['ZOZOBOX_DATA_DIR']); "
            "print('cfg=' + os.environ['ZOZOBOX_CONFIG']); print('lgwd=' + os.environ['LG_WD'])")
    sup = real_supervisor(tmp_path, {"svc": Spec("svc", [PY, "-c", code])}, backoff_base_s=1000)
    sup.start()
    wait_for(lambda: sup.services["svc"].proc is None, step=sup.step)
    out = sup.out.getvalue()
    data = tmp_path / "data"
    assert "svc: hello\n" in out and "svc: oops\n" in out
    assert f"svc: cwd={data}\n" in out and f"svc: data={data}\n" in out
    assert f"svc: cfg={tmp_path / 'box.toml'}\n" in out and f"svc: lgwd={data}\n" in out
    sup.shutdown()


def test_real_crashing_child_is_restarted(tmp_path):
    marker = tmp_path / "starts"
    code = f"open({str(marker)!r}, 'a').write('x'); raise SystemExit(3)"
    sup = real_supervisor(tmp_path, {"crashy": Spec("crashy", [PY, "-c", code])},
                          backoff_base_s=0.05, backoff_max_s=0.1)
    sup.start()
    wait_for(lambda: marker.exists() and len(marker.read_text()) >= 3, step=sup.step)
    assert sup.services["crashy"].restarts >= 2
    sup.shutdown()


def test_real_captured_job_output(tmp_path):
    sup = real_supervisor(tmp_path, {})
    sup.job_argv = {"content-status": [PY, "-c", "import json; print(json.dumps("
                                                 "{'phrases_missing': [], 'phrases_pending': []}))"]}
    sup.start()
    seen = {}
    sup._on_content_status = lambda rc, out: seen.update(rc=rc, out=out)
    assert sup._start_job("content-status", capture=True)
    wait_for(lambda: "rc" in seen, step=sup.step)
    assert seen["rc"] == 0 and json.loads(seen["out"])["phrases_missing"] == []
    sup.shutdown()


DRIVER = textwrap.dedent("""
    import logging, os, signal, sys
    sys.path.insert(0, {repo!r})
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from pathlib import Path
    from zozobox.run import Settings, Spec, Supervisor
    tmp = Path({tmp!r})
    py = sys.executable
    polite = (
        "import signal, sys, time\\n"
        "def bye(*a):\\n"
        "    time.sleep(0.2)\\n"
        "    open({marker!r}, 'w').write('clean')\\n"
        "    sys.exit(0)\\n"
        "signal.signal(signal.SIGTERM, bye)\\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\\n"
        "print('polite up', flush=True)\\n"
        "while True: time.sleep(0.1)\\n")
    stubborn = (
        "import os, signal, time\\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\\n"
        "open({pidfile!r}, 'w').write(str(os.getpid()))\\n"
        "print('stubborn up', flush=True)\\n"
        "while True: time.sleep(0.1)\\n")
    s = Settings(data_dir=tmp / "data", config=tmp / "box.toml", python=py, start_stagger_s=0,
                 tick_s=0.02, stop_grace_s=1.5, mood_first_s=1e9, bootstrap_first_s=1e9)
    sup = Supervisor(s, env=dict(os.environ), specs={{
        "polite": Spec("polite", [py, "-c", polite]),
        "stubborn": Spec("stubborn", [py, "-c", stubborn])}}, jobs={{}})
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, sup.request_stop)
    sys.exit(sup.run())
""")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_shutdown_forwards_waits_and_kills_stragglers(tmp_path, sig):
    marker, pidfile = tmp_path / "polite.done", tmp_path / "stubborn.pid"
    script = tmp_path / "driver.py"
    script.write_text(DRIVER.format(repo=str(REPO), tmp=str(tmp_path), marker=str(marker),
                                    pidfile=str(pidfile)))
    proc = subprocess.Popen([PY, str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        wait_for(lambda: pidfile.exists() and pidfile.read_text().strip())
        time.sleep(0.3)  # the polite child has its handler installed
        proc.send_signal(sig)
        out, _ = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    assert marker.read_text() == "clean"  # got SIGTERM and finished its cleanup
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # the one ignoring SIGTERM was killed
    assert "stubborn did not stop" in out and "polite: polite up" in out
    assert "supervisor: stopped" in out
