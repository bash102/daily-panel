"""Integration harness: a fake worker HTTP server on localhost (the
docs/SYNC.md protocol, served by zozobox.sync.fake.FakeCloud over real HTTP),
and a shared fake clock for the store (datetime) and engine (seconds)."""

from __future__ import annotations

import http.server
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from zozobox.engine import PauseStep
from zozobox.sync.fake import FakeCloud

NY = ZoneInfo("America/New_York")


class Clock:
    def __init__(self, *local):
        self.t = datetime(*local, tzinfo=NY).astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def mono(self) -> float:
        return self.t.timestamp()

    def advance(self, seconds: float = 0, **kw) -> None:
        self.t += timedelta(seconds=seconds, **kw)

    def set_local(self, *local) -> None:
        self.t = datetime(*local, tzinfo=NY).astimezone(timezone.utc)


def copy_normalizer(src: Path, dst: Path) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return {}


class FakeWorkerServer:
    """FakeCloud's request handling behind a real ThreadingHTTPServer."""

    def __init__(self, token: str = "box-token"):
        cloud_box: dict = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _do(self):
                cloud: FakeCloud = cloud_box["cloud"]
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else None
                with cloud_box["lock"]:
                    resp = cloud.request(self.command, cloud.base_url + self.path,
                                         headers=dict(self.headers.items()), body=body)
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp.body)))
                self.end_headers()
                self.wfile.write(resp.body)

            do_GET = do_PUT = do_POST = do_DELETE = _do

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self.httpd.server_address
        self.base_url = f"http://{host}:{port}"
        self.cloud = FakeCloud(token=token, base_url=self.base_url)
        cloud_box.update(cloud=self.cloud, lock=threading.Lock())
        self.lock = cloud_box["lock"]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def worker(monkeypatch):
    # The sync client's urllib transport honors *_proxy: never proxy localhost.
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    srv = FakeWorkerServer()
    yield srv
    srv.close()


def step_through(engine, audio, clock, *, until=None, timeout: float = 10.0) -> None:
    """Let the running experience play out: finish sounds, skip pauses, wait
    for worker threads (recording, classify, print)."""
    end = time.monotonic() + timeout
    until = until or (lambda: not engine.busy)
    while not until():
        if time.monotonic() > end:
            raise AssertionError(f"timed out; current={engine.current}")
        cur = engine.current
        if cur is not None and isinstance(cur.step, PauseStep):
            clock.advance(cur.step.seconds)
        for p in list(audio.plays):
            if not p.playback.done:
                p.proc.finish()
        engine.tick()
        time.sleep(0.001)
