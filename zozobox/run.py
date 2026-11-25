"""The box supervisor: the one process pi-ops runs (app-zozobox.service).

    python -m zozobox.run            # console script: zozobox-run
    python -m zozobox.run --plan     # print what would run (and what is skipped), exit

pi-ops' git-deploy renders a single systemd unit per app, so everything the
box needs runs as children of this process, using the existing CLIs:

Long-running (restarted when they exit):
    engine     python -m zozobox.app
    dashboard  python -m zozobox.dashboard --host 127.0.0.1 --port 8791 --printer brother
               (its unauthenticated /healthz is pi-ops' health check; reached
               from the tailnet through `tailscale serve`, never directly)
    sync       python -m zozobox.sync run   (the only recover_outbox=True opener)

A crashed child is restarted with exponential backoff (1 s, 2 s, 4 s ... capped
at 60 s; the count resets once a child has run for 60 s). An exit with the
child's config-error status (engine/dashboard 2: bad box.toml or the store
cannot be opened; sync 3) is not hot-looped: it is logged as an error and
retried every 5 minutes, so a fixed file is picked up without a restart.

One-shot jobs. The heavy ones (nightly, bootstrap, mood) never run at the same
time, so at most one of them adds to the unit's memory; content also holds its
own <data dir>/.content.lock:
    nightly    python -m zozobox.content nightly   daily at ~02:30 box-local
               time; if the Pi was off (or the run was interrupted) at that
               time it runs at the next start (catch-up). The last run is kept
               in <data dir>/supervisor-state.json.
    mood       python -m zozobox.mood reprocess    hourly (offline mood takes)
    bootstrap  python -m zozobox.content bootstrap only while fixed phrases are
               missing (`content status`: missing and not awaiting approval);
               at most once per 12 h, and the content CLI enforces the spend cap.

Limits on the jobs, because the unit has one MemoryMax= and systemd's default
OOMPolicy=stop: a kernel OOM kill of ANY process in the unit stops the whole
unit (engine, dashboard and sync too) and Restart=on-failure starts it again.
    - Each heavy job gets RLIMIT_DATA = ZOZOBOX_JOB_MEMORY_MB (384; 0 = none),
      so it fails with MemoryError inside the job instead of tripping the
      cgroup's OOM killer.
    - Each heavy job's start is recorded (<job>_started, <job>_unfinished)
      before it runs. A job that was started twice without ever finishing
      (an OOM restart loop, or two restarts mid-run) is not started again for
      the same nightly slot / within the bootstrap gap / within the mood hour;
      this is logged as an error. One interrupted run is retried.
    - Deadlines: nightly and bootstrap 2 h, mood 30 min, content-status 5 min.
      Past it the job's process group gets SIGTERM, then SIGKILL after 25 s.
    - Heavy jobs run at nice 10 and, when `ionice` exists, IO class idle.

Wall-clock decisions (nightly, catch-up, bootstrap gap, first-start state) wait
until the system clock is synchronized (systemd-timesyncd's
/run/systemd/timesync/synchronized, else the kernel's NTP status): the Pi 3
has no RTC. ZOZOBOX_REQUIRE_TIME_SYNC=0 turns the wait off.

Code on disk: git-deploy rebases the clone before it installs and restarts the
unit, and a failed install or health check leaves the old supervisor running
on new files. The supervisor records the clone's git HEAD at start; once it
differs, it starts no new job (each would import the new code) and logs
`code changed on disk since start`; children that crash are still restarted
so the box keeps working. `git-deploy unblock` + `sync` restarts the unit.

Secrets come from the pi-ops env_file (~/.config/zozobox/env). Without the AI
keys the content and mood jobs are skipped; without the cloud URL/token the
sync child is skipped. Each skip is logged once at start-up. The engine and the
dashboard always run, so the health check stays green.

Paths (when the env vars are unset; systemd's Environment= does not expand ~):
    ZOZOBOX_DATA_DIR   ~/.local/share/zozobox
    ZOZOBOX_CONFIG     ~/.config/zozobox/box.toml
Children run with their working directory (and LG_WD) in the data dir, so
nothing is ever written into the git-deploy clone.

Other env knobs: ZOZOBOX_DASHBOARD_HOST (127.0.0.1), ZOZOBOX_DASHBOARD_PORT
(8791), ZOZOBOX_DASHBOARD_PRINTER (brother), ZOZOBOX_DASHBOARD_SECURE_COOKIES
(1: the dashboard is only reached over HTTPS through tailscale serve),
ZOZOBOX_NIGHTLY_AT (02:30), ZOZOBOX_TZ (IANA zone for the nightly time;
default the system time zone), ZOZOBOX_JOB_MEMORY_MB (384),
ZOZOBOX_REQUIRE_TIME_SYNC (1).

SIGTERM/SIGINT: forwarded to every child, which get up to 25 s to finish
(the engine saves a reply being recorded and finishes a print), then
stragglers are killed. Exit status 0.

Child output goes to this process's stdout (the journal: `journalctl -u
app-zozobox`), each line prefixed with the child's name.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from datetime import time as dtime
from pathlib import Path
from typing import IO, Callable, Mapping, Sequence

from .paths import CONFIG_ENV, DATA_DIR_ENV, config_path_from_env, data_dir_from_env

log = logging.getLogger("zozobox.run")

STATE_FILE = "supervisor-state.json"
AI_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
CONTENT_JOBS = ("nightly", "bootstrap")
HEAVY_JOBS = ("nightly", "bootstrap", "mood")  # never two at once; memory-limited
SYNC_CONFIG_EXIT = 3  # zozobox.sync.__main__.EXIT_CONFIG
REPO_ROOT = Path(__file__).resolve().parents[1]
TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")
PLACEHOLDERS = ("replace-me", "<", ">")


def _truthy(v: str | None, default: bool) -> bool:
    if v is None or not v.strip():
        return default
    return v.strip().lower() not in ("0", "no", "false", "off")


@dataclass
class Settings:
    data_dir: Path
    config: Path
    python: str = sys.executable
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8791
    dashboard_printer: str = "brother"
    secure_cookies: bool = True
    nightly_at: tuple[int, int] = (2, 30)
    tz: tzinfo | None = None  # None = the system time zone
    mood_first_s: float = 300.0
    mood_every_s: float = 3600.0
    bootstrap_first_s: float = 60.0
    bootstrap_check_every_s: float = 6 * 3600.0
    bootstrap_min_gap_s: float = 12 * 3600.0
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60.0
    stable_s: float = 60.0
    config_retry_s: float = 300.0
    stop_grace_s: float = 25.0
    start_stagger_s: float = 3.0  # the dashboard (health check) gets a head start
    tick_s: float = 0.5
    prefix_output: bool = True
    job_nice: int = 10
    job_ionice: bool = True
    job_memory_mb: int = 384  # RLIMIT_DATA for each heavy job; 0 = no limit
    max_unfinished: int = 2   # starts without a finish before a job is held back
    require_time_sync: bool = True
    job_deadline_s: dict[str, float] = field(default_factory=lambda: {
        "nightly": 2 * 3600.0, "bootstrap": 2 * 3600.0, "mood": 1800.0,
        "content-status": 300.0})

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        s = cls(data_dir=data_dir_from_env(env), config=config_path_from_env(env))
        s.dashboard_host = env.get("ZOZOBOX_DASHBOARD_HOST") or s.dashboard_host
        if env.get("ZOZOBOX_DASHBOARD_PORT"):
            s.dashboard_port = int(env["ZOZOBOX_DASHBOARD_PORT"])
        s.dashboard_printer = env.get("ZOZOBOX_DASHBOARD_PRINTER") or s.dashboard_printer
        s.secure_cookies = _truthy(env.get("ZOZOBOX_DASHBOARD_SECURE_COOKIES"), True)
        if env.get("ZOZOBOX_NIGHTLY_AT"):
            hh, _, mm = env["ZOZOBOX_NIGHTLY_AT"].partition(":")
            h, m = int(hh), int(mm or 0)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError(f"ZOZOBOX_NIGHTLY_AT={env['ZOZOBOX_NIGHTLY_AT']!r}: want HH:MM")
            s.nightly_at = (h, m)
        if env.get("ZOZOBOX_TZ"):
            from zoneinfo import ZoneInfo

            s.tz = ZoneInfo(env["ZOZOBOX_TZ"])
        if env.get("ZOZOBOX_JOB_MEMORY_MB"):
            s.job_memory_mb = int(env["ZOZOBOX_JOB_MEMORY_MB"])
            if s.job_memory_mb < 0:
                raise ValueError("ZOZOBOX_JOB_MEMORY_MB must be >= 0")
        s.require_time_sync = _truthy(env.get("ZOZOBOX_REQUIRE_TIME_SYNC"), True)
        return s


# ------------------------------------------------- clock and code on disk ----


def clock_synchronized() -> bool:
    """True once the system clock has been set by NTP (the Pi 3 has no RTC).
    systemd-timesyncd's flag file first, else the kernel's NTP status
    (adjtimex returns TIME_ERROR while STA_UNSYNC is set, which chrony and
    timesyncd clear). Unknown (not Linux, no libc symbol) counts as synced."""
    if TIMESYNC_FLAG.exists():
        return True
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        buf = ctypes.create_string_buffer(1024)  # struct timex, modes = 0: read only
        state = libc.adjtimex(buf)
    except (OSError, AttributeError, TypeError):
        return True
    return state != 5 if state >= 0 else True  # 5 = TIME_ERROR: not synchronized


def git_head(root: Path) -> str | None:
    """The commit checked out in `root` (read from .git, no subprocess), or
    None when it is not a git checkout."""
    try:
        git = root / ".git"
        if git.is_file():  # worktree / submodule: "gitdir: <path>"
            line = git.read_text().strip()
            if not line.startswith("gitdir:"):
                return None
            git = (root / line.split(":", 1)[1].strip()).resolve()
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head.split(":", 1)[1].strip()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text().strip()).resolve()
        for base in (git, common):
            f = base / ref
            if f.is_file():
                return f.read_text().strip() or None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
        return None
    except (OSError, UnicodeDecodeError):
        return None


# ------------------------------------------------------------ secrets ----


def _secret_problem(env: Mapping[str, str], name: str) -> str | None:
    v = (env.get(name) or "").strip()
    if not v:
        return f"{name} is not set"
    if any(p in v.lower() for p in PLACEHOLDERS):
        return f"{name} is still a placeholder"
    return None


def ai_problem(env: Mapping[str, str]) -> str | None:
    """Why the content/mood jobs cannot run (None = keys present)."""
    problems = [p for p in (_secret_problem(env, k) for k in AI_KEYS) if p]
    return "; ".join(problems) or None


def cloud_problem(env: Mapping[str, str]) -> str | None:
    from .sync.__main__ import check_cloud_config

    return check_cloud_config((env.get("ZOZOBOX_CLOUD_URL") or "").strip(),
                              (env.get("ZOZOBOX_BOX_TOKEN") or "").strip())


# ----------------------------------------------------------- children ----


@dataclass
class Spec:
    name: str
    argv: list[str]
    config_exit_codes: frozenset[int] = frozenset()


def default_specs(s: Settings) -> dict[str, Spec]:
    py = s.python
    dash = [py, "-m", "zozobox.dashboard", "--host", s.dashboard_host,
            "--port", str(s.dashboard_port), "--printer", s.dashboard_printer]
    if s.secure_cookies:
        dash.append("--secure-cookies")
    return {
        "engine": Spec("engine", [py, "-m", "zozobox.app"], frozenset({2})),
        "dashboard": Spec("dashboard", dash, frozenset({2})),
        "sync": Spec("sync", [py, "-m", "zozobox.sync", "run"], frozenset({SYNC_CONFIG_EXIT})),
    }


def default_jobs(s: Settings) -> dict[str, list[str]]:
    py = s.python
    return {
        "nightly": [py, "-m", "zozobox.content", "nightly"],
        "bootstrap": [py, "-m", "zozobox.content", "bootstrap"],
        "content-status": [py, "-m", "zozobox.content", "status"],
        "mood": [py, "-m", "zozobox.mood", "reprocess"],
    }


class Proc:
    """One child process, its output pumped to our stdout with a name prefix."""

    def __init__(self, name: str, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path,
                 out: IO[str], out_lock: threading.Lock, prefix: bool,
                 capture_stdout: bool = False, nice: int = 0, data_limit_mb: int = 0):
        self.name = name
        self.captured: IO[bytes] | None = tempfile.TemporaryFile() if capture_stdout else None
        if self.captured is not None:
            stdout: int | IO[bytes] | None = self.captured
            stderr: int | None = subprocess.PIPE if prefix else None
        elif prefix:
            stdout, stderr = subprocess.PIPE, subprocess.STDOUT
        else:
            stdout = stderr = None
        self.popen = subprocess.Popen(list(argv), env=dict(env), cwd=str(cwd), stdin=subprocess.DEVNULL,
                                      stdout=stdout, stderr=stderr, start_new_session=True)
        self.pid = self.popen.pid
        if nice:
            try:
                os.setpriority(os.PRIO_PROCESS, self.pid, nice)
            except OSError:
                pass
        if data_limit_mb > 0:
            # Not preexec_fn (unsafe with our output threads): set it right
            # after the fork. The child's own children (ffmpeg) inherit it.
            try:
                import resource

                lim = data_limit_mb * 1024 * 1024
                resource.prlimit(self.pid, resource.RLIMIT_DATA, (lim, lim))
            except (ImportError, AttributeError, OSError, ValueError) as e:
                log.warning("supervisor: cannot limit %s's memory: %s", name, e)
        self._threads: list[threading.Thread] = []
        for stream in (self.popen.stdout if self.captured is None else None, self.popen.stderr):
            if stream is not None:
                t = threading.Thread(target=self._pump, args=(stream, out, out_lock),
                                     name=f"out-{name}", daemon=True)
                t.start()
                self._threads.append(t)

    def _pump(self, stream: IO[bytes], out: IO[str], lock: threading.Lock) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                with lock:
                    try:
                        out.write(f"{self.name}: {line}\n")
                        out.flush()
                    except (OSError, ValueError):
                        pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def poll(self) -> int | None:
        return self.popen.poll()

    def signal(self, sig: int) -> None:
        try:
            self.popen.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass

    def signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            self.signal(sig)

    def kill_group(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            self.signal(signal.SIGKILL)

    def output(self) -> str:
        if self.captured is None:
            return ""
        self.captured.seek(0)
        return self.captured.read().decode("utf-8", "replace")

    def finish(self, timeout: float = 1.0) -> None:
        for t in self._threads:
            t.join(timeout)

    def close(self) -> None:
        if self.captured is not None:
            self.captured.close()


@dataclass
class Service:
    spec: Spec
    proc: Proc | None = None
    started_at: float = 0.0
    next_start: float = 0.0
    failures: int = 0
    restarts: int = 0
    last_exit: int | None = None


@dataclass
class Job:
    name: str
    proc: Proc
    started_at: float
    started_wall: datetime
    deadline_s: float | None = None
    term_at: float | None = None
    killed: bool = False


# ------------------------------------------------------------ schedule ----


def _at_local(day: date, at: tuple[int, int], tz: tzinfo | None) -> datetime:
    wall = datetime.combine(day, dtime(at[0], at[1]))
    # A naive datetime's astimezone() reads it as system local time; either way
    # the UTC offset is the one in force on that day (DST).
    return wall.replace(tzinfo=tz) if tz is not None else wall.astimezone()


def last_scheduled(now: datetime, at: tuple[int, int], tz: tzinfo | None) -> datetime:
    """The most recent `at` (HH, MM) box-local time at or before `now`."""
    local = now.astimezone(tz) if tz is not None else now.astimezone()
    sched = _at_local(local.date(), at, tz)
    if sched > now:
        sched = _at_local(local.date() - timedelta(days=1), at, tz)
    return sched


def nightly_due(now: datetime, last_run: datetime | None, at: tuple[int, int],
                tz: tzinfo | None) -> bool:
    """True if the most recent scheduled time has passed without a run since
    (the Pi was off, or the run was interrupted)."""
    if last_run is None:
        return False
    return last_run < last_scheduled(now, at, tz)


# ---------------------------------------------------------- supervisor ----


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        *,
        env: Mapping[str, str] | None = None,
        specs: Mapping[str, Spec] | None = None,
        jobs: Mapping[str, Sequence[str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        out: IO[str] | None = None,
        clock_synced: Callable[[], bool] = clock_synchronized,
        code_version: Callable[[], str | None] = lambda: git_head(REPO_ROOT),
    ):
        self.s = settings
        self.base_env = dict(os.environ if env is None else env)
        self.clock = clock
        self.now = now
        self.out = out or sys.stdout
        self.out_lock = threading.Lock()
        self.stopping = False
        self._stop_requested = False  # a plain flag: safe to set from a signal handler
        self.first_check_done = False
        self.clock_synced = clock_synced
        self._synced = False
        self._sync_wait_logged = False
        self.code_version = code_version
        self.code_at_start: str | None = None
        self._code_change_logged = False
        self.bootstrap_wanted = False
        self._held_logged: set[str] = set()
        self.ionice = shutil.which("ionice") if settings.job_ionice else None

        self.ai_problem = ai_problem(self.base_env)
        self.cloud_problem = cloud_problem(self.base_env)
        all_specs = dict(specs) if specs is not None else default_specs(settings)
        self.skipped: dict[str, str] = {}
        if self.cloud_problem and "sync" in all_specs:
            self.skipped["sync"] = self.cloud_problem
            del all_specs["sync"]
        self.services = {n: Service(sp) for n, sp in all_specs.items()}
        self.job_argv = dict(jobs) if jobs is not None else default_jobs(settings)
        if self.ai_problem:
            for n in ("nightly", "bootstrap", "content-status", "mood"):
                if n in self.job_argv:
                    self.skipped.setdefault(n, self.ai_problem)
        self.jobs: dict[str, Job] = {}

        self.state: dict[str, str] = {}
        t0 = self.clock()
        self.next_mood = t0 + settings.mood_first_s
        self.next_bootstrap_check = t0 + settings.bootstrap_first_s

    # -- environment for children ------------------------------------------------
    def child_env(self) -> dict[str, str]:
        env = dict(self.base_env)
        env[DATA_DIR_ENV] = str(self.s.data_dir)
        env[CONFIG_ENV] = str(self.s.config)
        env.setdefault("LG_WD", str(self.s.data_dir))  # lgpio's notify files: not the clone
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _spawn(self, name: str, argv: Sequence[str], *, capture: bool = False,
               nice: int = 0, data_limit_mb: int = 0) -> Proc:
        return Proc(name, argv, env=self.child_env(), cwd=self.s.data_dir, out=self.out,
                    out_lock=self.out_lock, prefix=self.s.prefix_output,
                    capture_stdout=capture, nice=nice, data_limit_mb=data_limit_mb)

    # -- persisted state ------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.s.data_dir / STATE_FILE

    def load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text())
            self.state = {k: v for k, v in data.items() if isinstance(v, str)} \
                if isinstance(data, dict) else {}
        except FileNotFoundError:
            self.state = {}
        except (OSError, ValueError) as e:
            log.warning("supervisor: cannot read %s (%s); starting fresh", self.state_path, e)
            self.state = {}

    def save_state(self) -> None:
        try:
            self.s.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.s.data_dir, prefix=f".{STATE_FILE}.", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(self.state, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.error("supervisor: cannot save %s: %s", self.state_path, e)

    def _state_time(self, key: str) -> datetime | None:
        v = self.state.get(key)
        if not v:
            return None
        try:
            t = datetime.fromisoformat(v)
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    def _unfinished(self, name: str) -> int:
        try:
            return max(int(self.state.get(f"{name}_unfinished") or 0), 0)
        except ValueError:
            return 0

    def _held_back(self, name: str, since: datetime) -> bool:
        """True if `name` was started at least max_unfinished times in a row
        without finishing, the last time at or after `since`. Each unit start
        after an OOM kill (OOMPolicy=stop) or a restart mid-run looks like
        that; one interrupted run is retried, a second is not."""
        started = self._state_time(f"{name}_started")
        return (self._unfinished(name) >= self.s.max_unfinished and started is not None
                and started >= since)

    def _log_held(self, name: str, key: str, until: str) -> None:
        if key in self._held_logged:
            return
        self._held_logged.add(key)
        log.error("supervisor: job %s was started %d times without finishing (last at %s): "
                  "the unit was restarted mid-run, possibly by an out-of-memory kill "
                  "(journalctl -u app-zozobox -g oom). Not starting it again %s.", name,
                  self._unfinished(name), self.state.get(f"{name}_started"), until)

    # -- clock and code ---------------------------------------------------------------
    def time_ok(self) -> bool:
        """Wall-clock decisions wait until NTP has set the clock (no RTC)."""
        if self._synced or not self.s.require_time_sync:
            return True
        try:
            self._synced = bool(self.clock_synced())
        except Exception:  # noqa: BLE001 - never block the box on a probe
            self._synced = True
        if not self._synced:
            if not self._sync_wait_logged:
                self._sync_wait_logged = True
                log.warning("supervisor: the system clock is not synchronized yet (no RTC; "
                            "waiting for NTP): nightly and bootstrap wait for it")
            return False
        if self._sync_wait_logged:
            log.info("supervisor: system clock synchronized")
        self._on_clock_ok()
        return True

    def _on_clock_ok(self) -> None:
        now = self.now()
        last = self._state_time("nightly_last")
        if last is None:
            # First start: the first nightly run is at the next scheduled time,
            # not right now (bootstrap and approvals come first).
            self.state["nightly_last"] = now.isoformat()
            self.save_state()
        elif last > now + timedelta(minutes=5):
            log.warning("supervisor: nightly_last %s is in the future (the clock was wrong "
                        "when it was written); treating it as now", last.isoformat())
            self.state["nightly_last"] = now.isoformat()
            self.save_state()

    def code_changed(self) -> bool:
        """True once the clone's HEAD differs from the one at start: git-deploy
        rebased it, and the unit restart has not happened (yet, or the
        install or health check failed and the deploy is blocked)."""
        if self.code_at_start is None:
            return False
        try:
            cur = self.code_version()
        except Exception:  # noqa: BLE001
            return False
        if cur == self.code_at_start:
            return False
        if not self._code_change_logged:
            self._code_change_logged = True
            log.warning("supervisor: code changed on disk since start (%s -> %s); starting "
                        "no new jobs until git-deploy restarts app-zozobox (a blocked deploy: "
                        "git-deploy unblock daily-panel, then git-deploy sync daily-panel)",
                        (self.code_at_start or "?")[:12], (cur or "?")[:12])
        return True

    # -- start-up -------------------------------------------------------------------
    def start(self) -> None:
        self.s.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.load_state()
        try:
            self.code_at_start = self.code_version()
        except Exception:  # noqa: BLE001
            self.code_at_start = None
        log.info("supervisor: data dir %s, config %s%s", self.s.data_dir, self.s.config,
                 "" if self.s.config.exists() else " (missing: built-in defaults)")
        for name, why in sorted(self.skipped.items()):
            what = "child" if name == "sync" else "job"
            log.warning("supervisor: %s %s skipped: %s (fill in ~/.config/zozobox/env, then "
                        "sudo systemctl restart app-zozobox)", what, name, why)
        # Dashboard first: it is the health check, and a Pi 3 is slow to import.
        t0 = self.clock()
        for name, svc in self.services.items():
            if name == "dashboard":
                self._start_service(svc)
            else:
                svc.next_start = t0 + self.s.start_stagger_s
        self.step()

    def _start_service(self, svc: Service) -> None:
        if svc.restarts and self.code_changed():
            # Keep the box working; the restart picks up the new files.
            log.warning("supervisor: restarting %s from the changed code on disk", svc.spec.name)
        try:
            svc.proc = self._spawn(svc.spec.name, svc.spec.argv)
        except OSError as e:
            log.error("supervisor: cannot start %s: %s", svc.spec.name, e)
            svc.failures += 1
            svc.next_start = self.clock() + self._backoff(svc.failures)
            return
        svc.started_at = self.clock()
        log.info("supervisor: started %s (pid %s)", svc.spec.name, svc.proc.pid)

    def _backoff(self, failures: int) -> float:
        return min(self.s.backoff_base_s * (2 ** max(failures - 1, 0)), self.s.backoff_max_s)

    # -- one step of the loop -------------------------------------------------------
    def step(self) -> None:
        if self.stopping:
            return
        now = self.clock()
        for svc in self.services.values():
            self._check_service(svc, now)
        self._check_jobs(now)
        self._schedule(now)

    def _check_service(self, svc: Service, now: float) -> None:
        name = svc.spec.name
        if svc.proc is not None:
            rc = svc.proc.poll()
            if rc is None:
                if svc.failures and now - svc.started_at >= self.s.stable_s:
                    svc.failures = 0
                return
            svc.proc.finish(0.5)
            svc.proc = None
            svc.last_exit = rc
            ran = now - svc.started_at
            if ran >= self.s.stable_s:
                svc.failures = 0
            if rc in svc.spec.config_exit_codes:
                delay = self.s.config_retry_s
                log.error("supervisor: %s exited %s (configuration or store error, see its "
                          "output above); not restarting it for %.0f s. Fix it, then "
                          "`sudo systemctl restart app-zozobox` to retry at once.",
                          name, rc, delay)
            else:
                svc.failures += 1
                delay = self._backoff(svc.failures)
                log.warning("supervisor: %s exited %s after %.1f s; restart #%d in %.0f s",
                            name, rc, ran, svc.restarts + 1, delay)
            svc.restarts += 1
            svc.next_start = now + delay
            return
        if now >= svc.next_start:
            self._start_service(svc)

    def heavy_busy(self) -> bool:
        return any(j in self.jobs for j in HEAVY_JOBS)

    def _start_job(self, name: str, *, capture: bool = False) -> bool:
        if name in self.jobs or name in self.skipped or name not in self.job_argv:
            return False
        if name in HEAVY_JOBS and self.heavy_busy():
            return False  # one heavy job at a time (memory; content's own lock)
        if self.code_changed():
            return False
        heavy = name in HEAVY_JOBS
        argv = list(self.job_argv[name])
        if heavy and self.ionice:
            argv = [self.ionice, "-c3", *argv]  # IO class idle (execs: same pid)
        try:
            proc = self._spawn(name, argv, capture=capture,
                               nice=self.s.job_nice if heavy else 0,
                               data_limit_mb=self.s.job_memory_mb if heavy else 0)
        except OSError as e:
            log.error("supervisor: cannot start job %s: %s", name, e)
            return False
        self.jobs[name] = Job(name, proc, self.clock(), self.now(),
                              deadline_s=self.s.job_deadline_s.get(name))
        if heavy:
            self.state[f"{name}_started"] = self.now().isoformat()
            self.state[f"{name}_unfinished"] = str(self._unfinished(name) + 1)
            self.save_state()
        log.info("supervisor: job %s started (pid %s)", name, proc.pid)
        return True

    def _check_jobs(self, now: float) -> None:
        for name, job in list(self.jobs.items()):
            rc = job.proc.poll()
            if rc is None:
                self._enforce_deadline(job, now)
                continue
            job.proc.finish(0.5)  # the output pumps drain first
            output = job.proc.output()
            job.proc.close()
            del self.jobs[name]
            self._on_job_exit(job, rc, output)

    def _enforce_deadline(self, job: Job, now: float) -> None:
        if job.deadline_s is None:
            return
        if job.term_at is None:
            if now - job.started_at >= job.deadline_s:
                log.error("supervisor: job %s still running after %.0f s (its limit); "
                          "stopping it", job.name, now - job.started_at)
                job.term_at = now
                job.proc.signal_group(signal.SIGTERM)
        elif not job.killed and now - job.term_at >= self.s.stop_grace_s:
            log.error("supervisor: job %s ignored SIGTERM for %.0f s; killing it",
                      job.name, self.s.stop_grace_s)
            job.killed = True
            job.proc.kill_group()

    def _on_job_exit(self, job: Job, rc: int, output: str) -> None:
        name = job.name
        took = self.clock() - job.started_at
        if name in HEAVY_JOBS:
            # Finished (whatever the status, a deadline kill included): the
            # supervisor saw it end, so it is not an interrupted run.
            self.state[f"{name}_unfinished"] = "0"
            self.save_state()
        if name == "nightly":
            # Any finished run counts (exit 1 = some job failed or the spend
            # cap: flagged on the dashboard, not retried until tomorrow).
            self.state["nightly_last"] = self.now().isoformat()
            self.save_state()
            log.log(logging.INFO if rc == 0 else logging.WARNING,
                    "supervisor: nightly content finished (exit %s, %.0f s)", rc, took)
        elif name == "bootstrap":
            self.state["bootstrap_last"] = self.now().isoformat()
            self.save_state()
            log.log(logging.INFO if rc == 0 else logging.WARNING,
                    "supervisor: content bootstrap finished (exit %s, %.0f s); approve the "
                    "phrases on the dashboard", rc, took)
        elif name == "mood":
            if rc not in (0, 2):  # 2 = stopped early (offline/spend cap): not a failure
                log.warning("supervisor: mood reprocess exited %s", rc)
        elif name == "content-status":
            self._on_content_status(rc, output)

    def _on_content_status(self, rc: int, output: str) -> None:
        self.first_check_done = True
        if rc != 0:
            log.warning("supervisor: content status exited %s; bootstrap check skipped", rc)
            return
        try:
            st = json.loads(output)
            missing = set(st.get("phrases_missing") or [])
            pending = set(st.get("phrases_pending") or [])
        except (ValueError, AttributeError, TypeError):
            log.warning("supervisor: content status output unreadable; bootstrap check skipped")
            return
        needed = sorted(missing - pending)
        if not needed:
            return
        last = self._state_time("bootstrap_last")
        if last is not None and (self.now() - last).total_seconds() < self.s.bootstrap_min_gap_s:
            log.info("supervisor: %d phrase(s) missing (%s), but bootstrap last ran at %s; "
                     "waiting", len(needed), ", ".join(needed), last.isoformat())
            return
        gap = timedelta(seconds=self.s.bootstrap_min_gap_s)
        if self._held_back("bootstrap", self.now() - gap):
            started = self._state_time("bootstrap_started")
            self._log_held("bootstrap", f"bootstrap@{self.state.get('bootstrap_started')}",
                           f"before {(started + gap).isoformat() if started else 'later'}")
            return
        log.info("supervisor: %d fixed phrase(s) missing (%s): running the content bootstrap",
                 len(needed), ", ".join(needed))
        if not self._start_job("bootstrap") and self.heavy_busy():
            self.bootstrap_wanted = True  # starts when the running heavy job ends

    def _schedule(self, now: float) -> None:
        time_ok = self.time_ok()
        # The first phrases check (and a bootstrap it starts) goes before a
        # catch-up nightly run.
        if time_ok and self.first_check_done and nightly_due(
                self.now(), self._state_time("nightly_last"), self.s.nightly_at, self.s.tz):
            slot = last_scheduled(self.now(), self.s.nightly_at, self.s.tz)
            if self._held_back("nightly", slot):
                self._log_held("nightly", f"nightly@{slot.isoformat()}",
                               "until the next scheduled time")
            else:
                self._start_job("nightly")
        if self.bootstrap_wanted and not self.heavy_busy():
            self.bootstrap_wanted = False
            self._start_job("bootstrap")
        if now >= self.next_mood and not self.heavy_busy():
            self.next_mood = now + self.s.mood_every_s
            since = self.now() - timedelta(seconds=self.s.mood_every_s)
            if "mood" in self.job_argv and "mood" not in self.skipped and \
                    self._held_back("mood", since):
                self._log_held("mood", f"mood@{self.state.get('mood_started')}",
                               "within the hour")
            else:
                self._start_job("mood")
        if time_ok and now >= self.next_bootstrap_check and \
                not any(j in self.jobs for j in CONTENT_JOBS):
            self.next_bootstrap_check = now + self.s.bootstrap_check_every_s
            self._start_job("content-status", capture=True)

    # -- running and stopping -------------------------------------------------------
    def request_stop(self, *_: object) -> None:
        self._stop_requested = True

    def run(self) -> int:
        self.start()
        try:
            while not self._stop_requested:
                try:
                    self.step()
                except Exception:  # never let one bad step take the box down
                    log.exception("supervisor: step failed")
                if not self._stop_requested:
                    time.sleep(self.s.tick_s)
        finally:
            self.shutdown()
        return 0

    def running(self) -> list[Proc]:
        procs = [svc.proc for svc in self.services.values() if svc.proc is not None]
        return procs + [j.proc for j in self.jobs.values()]

    def shutdown(self) -> None:
        self.stopping = True
        procs = self.running()
        if procs:
            log.info("supervisor: stopping %s", ", ".join(p.name for p in procs))
        for p in procs:
            p.signal(signal.SIGTERM)
        deadline = time.monotonic() + self.s.stop_grace_s
        while time.monotonic() < deadline and any(p.poll() is None for p in procs):
            time.sleep(0.05)
        for p in procs:
            if p.poll() is None:
                log.warning("supervisor: %s did not stop in %.0f s; killing it",
                            p.name, self.s.stop_grace_s)
                p.kill_group()
        for p in procs:
            try:
                p.popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            p.finish(1.0)
            p.close()
        for svc in self.services.values():
            svc.proc = None
        self.jobs.clear()
        log.info("supervisor: stopped")

    # -- --plan ---------------------------------------------------------------------
    def plan(self) -> str:
        lines = [f"data dir: {self.s.data_dir}", f"config:   {self.s.config}"]
        for name, svc in self.services.items():
            lines.append(f"child {name}: {' '.join(svc.spec.argv)}")
        for name, argv in self.job_argv.items():
            what = f"skipped ({self.skipped[name]})" if name in self.skipped else " ".join(argv)
            lines.append(f"job {name}: {what}")
        if "sync" in self.skipped:
            lines.append(f"child sync: skipped ({self.skipped['sync']})")
        h, m = self.s.nightly_at
        lines.append(f"nightly at {h:02d}:{m:02d} "
                     f"{getattr(self.s.tz, 'key', None) or 'system local time'}")
        return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zozobox.run",
                                 description="Zozo box supervisor (the pi-ops unit)")
    ap.add_argument("--plan", action="store_true", help="print what would run, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
    except (ValueError, KeyError) as e:
        print(f"zozobox.run: bad setting: {e}", file=sys.stderr)
        return 2
    sup = Supervisor(settings)
    if args.plan:
        print(sup.plan())
        return 0
    if threading.current_thread() is threading.main_thread():
        for s in (signal.SIGTERM, signal.SIGINT):
            signal.signal(s, sup.request_stop)
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
