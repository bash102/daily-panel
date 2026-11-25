"""Dashboard authentication (SPEC 8): one parent password, signed session
cookies, CSRF tokens and login rate limiting.

- `PasswordFile`: the scrypt hash (random salt) plus the cookie-signing secret,
  stored as JSON in the data dir with 0600 permissions. Changing the password
  rotates the secret, which logs out every other session.
- `SessionCodec`: HMAC-SHA256 signed cookie payloads. Every visitor gets a
  session (anonymous until login) so login/setup forms carry a CSRF token too.
- `LoginLimiter`: failed-login throttle per client and overall, with an
  injected clock.

Only the standard library is used (hashlib.scrypt, hmac, secrets).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

AUTH_FILE_NAME = "dashboard_auth.json"
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 256

# scrypt cost: ~16 MiB, roughly 0.1-0.3 s on a Pi 3.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024


class AuthError(Exception):
    pass


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def hash_password(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )


def check_new_password(password: str, confirm: str) -> str | None:
    """Return an error message, or None when the new password is acceptable."""
    if password != confirm:
        return "The two passwords don't match."
    if len(password) < PASSWORD_MIN_LEN:
        return f"Use at least {PASSWORD_MIN_LEN} characters."
    if len(password) > PASSWORD_MAX_LEN:
        return f"Use at most {PASSWORD_MAX_LEN} characters."
    return None


@dataclass(frozen=True)
class _Record:
    n: int
    r: int
    p: int
    salt: bytes
    hash: bytes
    secret: bytes

    @property
    def generation(self) -> str:
        """Short tag that changes with every password change (in the cookie)."""
        return hashlib.sha256(b"gen" + self.salt + self.hash).hexdigest()[:16]


class PasswordFile:
    """The parent password hash and session secret, in one 0600 JSON file."""

    def __init__(self, path: str | Path, *, scrypt_n: int = SCRYPT_N):
        self.path = Path(path)
        self.scrypt_n = scrypt_n
        self._lock = threading.Lock()
        self._cache: tuple[tuple[int, int], _Record] | None = None

    # -- reading --------------------------------------------------------------
    def exists(self) -> bool:
        return self._load() is not None

    def _load(self) -> _Record | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._cache = None
            return None
        except OSError:
            log.exception("dashboard: cannot stat %s", self.path)
            return None
        key = (st.st_mtime_ns, st.st_size)
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        if st.st_mode & 0o077:
            try:
                os.chmod(self.path, 0o600)
                log.warning("dashboard: tightened permissions on %s to 0600", self.path)
            except OSError:
                log.exception("dashboard: cannot chmod %s", self.path)
        try:
            d = json.loads(self.path.read_text("utf-8"))
            rec = _Record(
                n=int(d["n"]), r=int(d["r"]), p=int(d["p"]),
                salt=bytes.fromhex(d["salt"]), hash=bytes.fromhex(d["hash"]),
                secret=bytes.fromhex(d["secret"]),
            )
            if d.get("kdf") != "scrypt" or len(rec.secret) < 32 or len(rec.salt) < 16:
                raise ValueError("unexpected auth file contents")
        except Exception:
            # A corrupt file must not unlock the dashboard: treat it as present
            # but unusable (no password matches), so setup can't be re-run by
            # a stranger either. The owner deletes the file to reset.
            log.error("dashboard: auth file %s is unreadable; login disabled", self.path)
            rec = _Record(SCRYPT_N, SCRYPT_R, SCRYPT_P, b"\0" * 16, b"", secrets.token_bytes(32))
        self._cache = (key, rec)
        return rec

    def secret(self) -> bytes | None:
        rec = self._load()
        return rec.secret if rec else None

    def generation(self) -> str | None:
        rec = self._load()
        return rec.generation if rec else None

    def verify(self, password: str) -> bool:
        rec = self._load()
        if rec is None or not rec.hash or len(password) > PASSWORD_MAX_LEN:
            return False
        try:
            got = hash_password(password, rec.salt, rec.n, rec.r, rec.p)
        except (ValueError, MemoryError):
            log.exception("dashboard: scrypt failed")
            return False
        return hmac.compare_digest(got, rec.hash)

    # -- writing --------------------------------------------------------------
    def _record_json(self, password: str) -> str:
        salt = secrets.token_bytes(16)
        h = hash_password(password, salt, self.scrypt_n, SCRYPT_R, SCRYPT_P)
        return json.dumps({
            "v": 1, "kdf": "scrypt", "n": self.scrypt_n, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": salt.hex(), "hash": h.hex(), "secret": secrets.token_bytes(32).hex(),
        })

    def create(self, password: str) -> bool:
        """First-run setup. Returns False if a password already exists (race-safe:
        the file is created with O_EXCL)."""
        data = self._record_json(password).encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return False
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(self.path, 0o600)
            self._cache = None
        return True

    def change(self, password: str) -> None:
        """Replace the password (and the session secret) atomically."""
        data = self._record_json(password).encode("utf-8")
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".auth.", suffix=".tmp")
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, data)
                os.fsync(fd)
                os.close(fd)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._cache = None


@dataclass
class Session:
    csrf: str
    auth: bool = False
    gen: str = ""
    iat: float = 0.0
    dirty: bool = False  # needs a Set-Cookie on the response
    clear: bool = False

    def payload(self) -> dict[str, Any]:
        return {"v": 1, "csrf": self.csrf, "auth": self.auth, "gen": self.gen, "iat": int(self.iat)}


def new_session(now: float, *, auth: bool = False, gen: str = "") -> Session:
    return Session(csrf=secrets.token_urlsafe(32), auth=auth, gen=gen, iat=now, dirty=True)


class SessionCodec:
    """Signs and verifies cookie payloads with HMAC-SHA256."""

    @staticmethod
    def encode(payload: dict[str, Any], secret: bytes) -> str:
        body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        sig = _b64e(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{sig}"

    @staticmethod
    def decode(token: str | None, secret: bytes) -> dict[str, Any] | None:
        if not token or len(token) > 4096 or token.count(".") != 1:
            return None
        body, sig = token.split(".")
        try:
            want = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(want, _b64d(sig)):
                return None
            d = json.loads(_b64d(body))
        except Exception:
            return None
        return d if isinstance(d, dict) else None


@dataclass
class LoginLimiter:
    """Failed-login throttle: at most `max_failures` per client and
    `max_global_failures` overall within `window_s` seconds."""

    clock: Callable[[], float]
    max_failures: int = 5
    max_global_failures: int = 20
    window_s: float = 300.0
    _per: dict[str, deque] = field(default_factory=dict)
    _all: deque = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _prune(self, q: deque, now: float) -> None:
        while q and q[0] <= now - self.window_s:
            q.popleft()

    def retry_after(self, client: str) -> float:
        """Seconds until this client may try again (0 = allowed now)."""
        now = self.clock()
        with self._lock:
            waits = [0.0]
            q = self._per.get(client)
            if q is not None:
                self._prune(q, now)
                if not q:
                    self._per.pop(client, None)
                elif len(q) >= self.max_failures:
                    waits.append(q[0] + self.window_s - now)
            self._prune(self._all, now)
            if len(self._all) >= self.max_global_failures:
                waits.append(self._all[0] + self.window_s - now)
            return max(waits)

    def _record(self, client: str, now: float) -> None:
        self._per.setdefault(client, deque()).append(now)
        self._all.append(now)
        if len(self._per) > 10_000:  # bound memory against address spraying
            oldest = min(self._per, key=lambda k: self._per[k][-1] if self._per[k] else 0)
            self._per.pop(oldest, None)

    def reserve(self, client: str) -> tuple[float, float | None]:
        """Check-and-count in one step, before the (slow) password check.
        Returns (wait, token): wait > 0 means refused (token None). Otherwise
        the attempt is already counted as a failure, so concurrent requests
        can't all slip past the check; call success(client, token) if the
        password turns out right to take it back."""
        now = self.clock()
        with self._lock:
            waits = [0.0]
            q = self._per.get(client)
            if q is not None:
                self._prune(q, now)
                if len(q) >= self.max_failures:
                    waits.append(q[0] + self.window_s - now)
            self._prune(self._all, now)
            if len(self._all) >= self.max_global_failures:
                waits.append(self._all[0] + self.window_s - now)
            wait = max(waits)
            if wait > 0:
                return wait, None
            self._record(client, now)
            return 0.0, now

    def failure(self, client: str) -> None:
        """Count a failure that wasn't reserved (reserve() already counts)."""
        now = self.clock()
        with self._lock:
            self._record(client, now)

    def success(self, client: str, token: float | None = None) -> None:
        """Clear this client's failures, and take back a reserved attempt."""
        with self._lock:
            self._per.pop(client, None)
            if token is not None:
                try:
                    self._all.remove(token)
                except ValueError:
                    pass
