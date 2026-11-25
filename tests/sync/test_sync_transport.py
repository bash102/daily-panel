"""UrllibTransport + CloudApi against a local in-process HTTP server (no real network)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from zozobox.sync import CloudApi, CloudError, TransportError, UrllibTransport, iso_utc, parse_iso
from zozobox.sync.api import quote_key


class Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):  # quiet
        pass

    def _send(self, status, body=b"", ctype="application/json"):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if self.headers.get("authorization") != "Bearer tok":
            self._send(401, b'{"error":"unauthorized"}')
            return False
        return True

    def do_PUT(self):
        n = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(n)
        Handler.seen.append(("PUT", self.path, self.headers.get("content-type"), body))
        if not self._auth():
            return
        self._send(200, json.dumps({"ok": True, "bytes": len(body)}).encode())

    def do_GET(self):
        Handler.seen.append(("GET", self.path, None, None))
        if not self._auth():
            return
        if self.path.startswith("/api/grandma/messages"):
            self._send(200, json.dumps([
                {"id": "RE1", "key": "recordings/grandma/2026/11/25/RE1.wav",
                 "recorded_at": "2026-11-25T12:00:00.000Z", "duration_s": 3},
                {"id": "bad"},  # skipped
            ]).encode())
        elif self.path.startswith("/api/objects/recordings/grandma/"):
            self._send(200, b"RIFFdata", "audio/wav")
        elif self.path == "/api/zozo/messages":
            self._send(200, b"not json")
        elif self.path == "/api/boom":
            self._send(500, b"oops")
        else:
            self._send(404, b'{"error":"not found"}')


@pytest.fixture
def server(monkeypatch):
    for v in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    Handler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_put_and_get_roundtrip(server):
    api = CloudApi(server, "tok")
    assert api.put_object("prints/2026/a b.png", b"\x89PNG", "image/png") == {"ok": True, "bytes": 4}
    method, path, ctype, body = Handler.seen[-1]
    assert (method, path, ctype, body) == ("PUT", "/api/objects/prints/2026/a%20b.png", "image/png", b"\x89PNG")
    assert api.put_json("control/config.json", {"a": 1})["ok"]
    assert Handler.seen[-1][3] == b'{"a": 1}'
    assert api.get_object("recordings/grandma/2026/11/25/RE1.wav") == b"RIFFdata"


def test_grandma_messages_parsing(server):
    msgs = CloudApi(server, "tok").grandma_messages("2026-11-25T00:00:00.000Z", 10)
    assert [m.id for m in msgs] == ["RE1"]
    assert msgs[0].duration_s == 3.0
    assert "since=2026-11-25T00%3A00%3A00.000Z" in Handler.seen[-1][1]


def test_http_errors(server):
    api = CloudApi(server, "tok")
    with pytest.raises(CloudError) as e:
        api.get_object("prints/missing.png")
    assert e.value.status == 404 and not e.value.retryable and not e.value.offline
    with pytest.raises(CloudError) as e:
        api.zozo_messages()
    assert "bad JSON" in str(e.value)
    with pytest.raises(CloudError) as e:
        CloudApi(server, "nope").health()
    assert e.value.auth
    r = UrllibTransport().request("GET", server + "/api/boom", headers={"Authorization": "Bearer tok"})
    assert r.status == 500 and r.body == b"oops"


def test_offline_is_a_transport_error(server):
    dead = server.rsplit(":", 1)[0] + ":1"  # nothing listens on port 1
    with pytest.raises(TransportError):
        UrllibTransport().request("GET", dead + "/api/health", timeout=2)
    with pytest.raises(CloudError) as e:
        CloudApi(dead, "tok", timeout_s=2).health()
    assert e.value.offline and e.value.retryable


def test_retryable_classification():
    assert CloudError("x", status=503).retryable
    assert CloudError("x", status=429).retryable
    assert not CloudError("x", status=422).retryable
    assert CloudError("x", offline=True).retryable


def test_iso_helpers():
    from datetime import datetime, timedelta, timezone

    assert iso_utc(datetime(2026, 11, 25, 10, 0, 0, 123999, tzinfo=timezone(timedelta(hours=-5)))) == \
        "2026-11-25T15:00:00.123Z"
    assert iso_utc(datetime(2026, 11, 25, 10)) == "2026-11-25T10:00:00.000Z"  # naive = UTC
    assert parse_iso("2026-11-25T15:00:00.123Z") == datetime(2026, 11, 25, 15, 0, 0, 123000, tzinfo=timezone.utc)
    assert parse_iso("2026-11-25T15:00:00") is None
    assert parse_iso(None) is None
    assert quote_key("a/b c/d.wav") == "a/b%20c/d.wav"
