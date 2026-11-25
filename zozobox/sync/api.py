"""Typed wrapper over the worker's Pi API (docs/SYNC.md).

Every call sends "Authorization: Bearer <token>". Errors raise CloudError,
which says whether retrying could help (`retryable`) and whether the box is
offline (`offline`: no HTTP answer at all).
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .transport import HttpResponse, Transport, TransportError, UrllibTransport


class CloudError(Exception):
    def __init__(self, message: str, *, status: int | None = None, offline: bool = False):
        super().__init__(message)
        self.status = status
        self.offline = offline

    @property
    def auth(self) -> bool:
        return self.status in (401, 403)

    @property
    def retryable(self) -> bool:
        """True for offline, 408, 429 and 5xx; False for other 4xx (bad request)."""
        if self.offline or self.status is None:
            return True
        return self.status in (408, 429) or self.status >= 500


@dataclass(frozen=True)
class GrandmaMessage:
    id: str
    key: str
    recorded_at: datetime
    recorded_at_raw: str
    duration_s: float | None


@dataclass(frozen=True)
class ZozoStatus:
    rec_id: str
    status: str  # pending | published | withheld | superseded
    published_at: datetime | None
    publish_at: datetime | None


def iso_utc(dt: datetime) -> str:
    """The wire format for timestamps: UTC, millisecond precision, 'Z'."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso(s: Any) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def quote_key(key: str) -> str:
    """Percent-encode a key for the URL path, keeping the slashes."""
    return urllib.parse.quote(key, safe="/")


class CloudApi:
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: Transport | None = None,
        *,
        timeout_s: float = 30.0,
        upload_timeout_s: float = 120.0,
    ):
        if not base_url:
            raise ValueError("base_url is required (ZOZOBOX_CLOUD_URL)")
        if not token:
            raise ValueError("token is required (ZOZOBOX_BOX_TOKEN)")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.transport = transport or UrllibTransport()
        self.timeout_s = timeout_s
        self.upload_timeout_s = upload_timeout_s

    def __repr__(self) -> str:  # never show the token
        return f"CloudApi({self.base_url!r})"

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": "zozobox-sync/1"}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            r = self.transport.request(
                method, self.base_url + path, headers=headers, body=body,
                timeout=timeout or self.timeout_s,
            )
        except TransportError as e:
            raise CloudError(str(e), offline=True) from e
        if not r.ok:
            detail = r.body[:200].decode("utf-8", "replace")
            raise CloudError(f"{method} {path.split('?')[0]}: HTTP {r.status} {detail}", status=r.status)
        return r

    def _json(self, r: HttpResponse) -> Any:
        try:
            return json.loads(r.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise CloudError(f"bad JSON from the cloud: {e}", status=r.status) from e

    # -- objects ----------------------------------------------------------------
    def put_object(self, key: str, data: bytes, content_type: str) -> dict:
        r = self._call("PUT", "/api/objects/" + quote_key(key), body=data,
                       content_type=content_type, timeout=self.upload_timeout_s)
        return self._json(r) if r.body else {}

    def put_json(self, key: str, record: Any) -> dict:
        return self.put_object(key, json.dumps(record).encode("utf-8"), "application/json")

    def get_object(self, key: str) -> bytes:
        return self._call("GET", "/api/objects/" + quote_key(key),
                          timeout=self.upload_timeout_s).body

    # -- queries ----------------------------------------------------------------
    def grandma_messages(self, since: str | None = None, limit: int = 50) -> list[GrandmaMessage]:
        q = {"limit": str(limit)}
        if since:
            q["since"] = since
        data = self._json(self._call("GET", "/api/grandma/messages?" + urllib.parse.urlencode(q)))
        if not isinstance(data, list):
            raise CloudError("grandma messages: expected a list")
        out = []
        for m in data:
            if not isinstance(m, dict):
                continue
            ts = parse_iso(m.get("recorded_at"))
            mid, key = m.get("id"), m.get("key")
            if ts is None or not isinstance(mid, str) or not isinstance(key, str):
                continue
            dur = m.get("duration_s")
            out.append(GrandmaMessage(mid, key, ts, m["recorded_at"],
                                      float(dur) if isinstance(dur, (int, float)) else None))
        return out

    def zozo_messages(self) -> list[ZozoStatus]:
        data = self._json(self._call("GET", "/api/zozo/messages"))
        out = []
        for m in data if isinstance(data, list) else []:
            if isinstance(m, dict) and isinstance(m.get("rec_id"), str):
                out.append(ZozoStatus(m["rec_id"], str(m.get("status")),
                                      parse_iso(m.get("published_at")), parse_iso(m.get("publish_at"))))
        return out

    def health(self) -> dict:
        return self._json(self._call("GET", "/api/health"))
