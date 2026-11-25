"""FakeCloud: an in-memory twin of the worker's Pi API (docs/SYNC.md) that
implements the `Transport` protocol, for tests of the sync client (and of
anything else that wants a cloud without a network).

    cloud = FakeCloud(token="t")
    client = SyncClient(store, cloud.base_url, "t", transport=cloud)
    cloud.add_grandma("RE1", b"RIFF...", "2026-11-25T15:00:00.000Z", 12.0)
    cloud.offline = True          # every request raises TransportError
    cloud.fail_next(503)          # next request answers 503
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .api import iso_utc, parse_iso
from .transport import HttpResponse, TransportError

PUT_PREFIXES = ("recordings/zozo/", "prints/", "mood/", "content/", "phrases/", "events/",
                "logs/", "control/")
GET_PREFIXES = PUT_PREFIXES + ("recordings/grandma/",)
_SAFE = re.compile(r"^[A-Za-z0-9._~+=@,\-/]+$")


def safe_key(key: str) -> bool:
    if not key or len(key) > 512 or key.startswith("/") or key.endswith("/") or not _SAFE.match(key):
        return False
    return all(seg not in ("", ".", "..") for seg in key.split("/"))


@dataclass
class FakeRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes | None


@dataclass
class FakeZozo:
    rec_id: str
    status: str = "pending"
    media_key: str | None = None
    recorded_at: str | None = None
    published_at: str | None = None


@dataclass
class FakeCloud:
    token: str = "box-token"
    base_url: str = "https://cloud.test"
    offline: bool = False
    objects: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    config: dict[str, Any] | None = None
    zozo: dict[str, FakeZozo] = field(default_factory=dict)
    grandma: list[dict[str, Any]] = field(default_factory=list)
    requests: list[FakeRequest] = field(default_factory=list)
    _fail: list[int] = field(default_factory=list)

    # -- test knobs ----------------------------------------------------------------
    def fail_next(self, *statuses: int) -> None:
        """The next len(statuses) requests answer with these HTTP statuses."""
        self._fail.extend(statuses)

    def add_grandma(self, rec_id: str, audio: bytes, recorded_at: str | datetime,
                    duration_s: float | None = 10.0, key: str | None = None) -> dict[str, Any]:
        ts = recorded_at if isinstance(recorded_at, str) else iso_utc(recorded_at)
        dt = parse_iso(ts)
        assert dt is not None
        key = key or f"recordings/grandma/{dt:%Y/%m/%d}/{rec_id}.wav"
        self.objects[key] = (audio, "audio/wav")
        m = {"id": rec_id, "key": key, "recorded_at": ts, "duration_s": duration_s}
        self.grandma.append(m)
        self.grandma.sort(key=lambda x: (x["recorded_at"], x["id"]))
        return m

    def publish(self, rec_id: str, at: str) -> None:
        z = self.zozo[rec_id]
        if z.status == "pending":
            z.status, z.published_at = "published", at

    def puts(self, prefix: str = "") -> list[str]:
        return [r.path[len("/api/objects/"):] for r in self.requests
                if r.method == "PUT" and r.path.startswith("/api/objects/" + prefix)]

    def json_object(self, key: str) -> Any:
        return json.loads(self.objects[key][0].decode())

    # -- Transport --------------------------------------------------------------------
    def request(self, method, url, *, headers=None, body=None, timeout=30.0) -> HttpResponse:
        if self.offline:
            raise TransportError(f"{method} {url.split('?')[0]}: network unreachable")
        if not url.startswith(self.base_url + "/"):
            raise TransportError(f"unknown host for {url}")
        u = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(u.path)
        query = dict(urllib.parse.parse_qsl(u.query))
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        self.requests.append(FakeRequest(method, path, query, hdrs, body))
        if self._fail:
            return self._json({"error": "injected"}, self._fail.pop(0))
        if hdrs.get("authorization") != f"Bearer {self.token}":
            return self._json({"error": "unauthorized"}, 401)
        if path.startswith("/api/objects/"):
            key = path[len("/api/objects/"):]
            if method == "PUT":
                return self._put(key, body or b"", hdrs.get("content-type", "application/octet-stream"))
            if method == "GET":
                if not (safe_key(key) and key.startswith(GET_PREFIXES)):
                    return self._json({"error": "bad key"}, 400)
                if key not in self.objects:
                    return self._json({"error": "not found"}, 404)
                data, ctype = self.objects[key]
                return HttpResponse(200, data, {"content-type": ctype})
            return self._json({"error": "method"}, 405)
        if method != "GET":
            return self._json({"error": "method"}, 405)
        if path == "/api/grandma/messages":
            since = query.get("since")
            if since is not None and parse_iso(since) is None:
                return self._json({"error": "bad since"}, 400)
            limit = int(query.get("limit", "50"))
            since_n = iso_utc(parse_iso(since)) if since else ""
            rows = [m for m in self.grandma if iso_utc(parse_iso(m["recorded_at"])) >= since_n]
            return self._json(rows[:limit])
        if path == "/api/zozo/messages":
            return self._json([{"rec_id": z.rec_id, "status": z.status, "recorded_at": z.recorded_at,
                                "publish_at": None, "published_at": z.published_at}
                               for z in self.zozo.values()])
        if path == "/api/health":
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    # -- internals -------------------------------------------------------------------
    @staticmethod
    def _json(data: Any, status: int = 200) -> HttpResponse:
        return HttpResponse(status, json.dumps(data).encode(), {"content-type": "application/json"})

    def _put(self, key: str, body: bytes, ctype: str) -> HttpResponse:
        if not (safe_key(key) and key.startswith(PUT_PREFIXES)):
            return self._json({"error": "bad key"}, 400)
        result: Any = None
        if key.startswith("control/"):
            try:
                obj = json.loads(body.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._json({"error": "bad json"}, 422)
            m = re.match(r"^control/zozo/([A-Za-z0-9_]{1,100})(-withhold)?\.json$", key)
            if key == "control/config.json":
                if not isinstance(obj, dict) or {"quiet_hours", "timezone", "review_delay_s"} - set(obj):
                    return self._json({"error": "bad config"}, 422)
                self.config = obj
                result = "config"
            elif m:
                rec_id, withhold = m.group(1), bool(m.group(2))
                if not isinstance(obj, dict) or obj.get("rec_id") != rec_id or \
                        obj.get("action") != ("withhold" if withhold else "new"):
                    return self._json({"error": "bad command"}, 422)
                z = self.zozo.setdefault(rec_id, FakeZozo(rec_id))
                if withhold:
                    z.status = "withheld"
                    result = "withheld"
                else:
                    if parse_iso(obj.get("recorded_at")) is None or \
                            not str(obj.get("media_key", "")).startswith("recordings/zozo/"):
                        return self._json({"error": "bad command"}, 422)
                    z.media_key, z.recorded_at = obj["media_key"], obj["recorded_at"]
                    result = "withheld" if z.status == "withheld" else "registered"
        self.objects[key] = (body, ctype)
        out: dict[str, Any] = {"ok": True, "key": key}
        if result is not None:
            out.update(applied=True, result=result)
        return self._json(out)


def fake_normalizer(calls: list | None = None):
    """A stand-in for the ffmpeg loudness normalizer: prefixes b"NORM:"."""
    def norm(src, dst):
        if calls is not None:
            calls.append(src)
        dst.write_bytes(b"NORM:" + src.read_bytes())
    return norm
