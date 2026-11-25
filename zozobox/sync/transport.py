"""HTTP transport for the sync client: a tiny Protocol with a stdlib (urllib)
implementation. Tests use `zozobox.sync.fake.FakeCloud`, which implements the
same Protocol in memory, so no test touches the network."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Protocol


class TransportError(Exception):
    """The request never got an HTTP answer (offline, DNS, timeout, TLS...)."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        """Return the HTTP response (any status). Raise TransportError when
        there is no response at all."""
        ...


class UrllibTransport:
    """stdlib transport. Honors the usual *_proxy environment variables."""

    def __init__(self, max_body_bytes: int = 64 * 1024 * 1024):
        self.max_body_bytes = max_body_bytes

    def request(self, method, url, *, headers=None, body=None, timeout=30.0) -> HttpResponse:
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(self.max_body_bytes + 1)
                if len(data) > self.max_body_bytes:
                    raise TransportError(f"{method} {_strip(url)}: response too large")
                return HttpResponse(r.status, data, {k.lower(): v for k, v in r.headers.items()})
        except urllib.error.HTTPError as e:
            try:
                data = e.read(64 * 1024)
            except Exception:
                data = b""
            return HttpResponse(e.code, data, {k.lower(): v for k, v in (e.headers or {}).items()})
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise TransportError(f"{method} {_strip(url)}: {getattr(e, 'reason', e)}") from e


def _strip(url: str) -> str:
    """URL without its query string, for log lines."""
    return url.split("?", 1)[0]
