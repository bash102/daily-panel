"""Parent dashboard web app (SPEC 8), FastAPI + Jinja2, server-rendered.

    app = create_app(store, printer)          # tests: fastapi.testclient.TestClient(app)
    python -m zozobox.dashboard --data-dir ~/.local/share/zozobox --host 127.0.0.1 --port 8791

It runs as its own process with its own Store connection. It never talks to
the cloud: changes that the cloud must know about (withholding Zozo's
message) go into the store outbox as control records (docs/SYNC.md), and the
sync client delivers them.
"""


import hmac
import io
import json
import logging
import math
import re
import secrets
import threading
import time
import zoneinfo
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit

from ..store import RecStatus, Slot, Store, StoreError
from ..store.settings import SPECS, SettingsError
from . import queries as Q
from .auth import (
    AUTH_FILE_NAME,
    SCRYPT_N,
    LoginLimiter,
    PasswordFile,
    Session,
    SessionCodec,
    check_new_password,
    new_session,
)
from .templates import CSS, JS, TEMPLATES

log = logging.getLogger(__name__)

COOKIE_NAME = "zozobox_session"
CONFIRM_WORD = "PURGE"
WITHHOLD_EVENT = Q.WITHHOLD_EVENT
REPRINT_FAILED_EVENT = "error"

NAV = [
    ("/", "Home"),
    ("/approvals", "Approvals"),
    ("/recordings", "Recordings"),
    ("/archive", "Archive"),
    ("/mood", "Mood"),
    ("/stats", "Stats"),
    ("/reprint", "Reprint"),
    ("/settings", "Settings"),
]

# Flash messages are chosen by code so nothing user-supplied is reflected.
FLASH: dict[str, tuple[str, bool]] = {
    "approved": ("Approved. It can be served now.", False),
    "rejected": ("Rejected. It won't be served.", False),
    "withheld_zozo": ("Withheld. Grandma won't get this message; the cloud is told on the next sync.", False),
    "withheld_grandma": ("Withheld. Zozo won't hear this message.", False),
    "withheld": ("Withheld.", False),
    "purged": ("Purged permanently from the box.", False),
    "purge_confirm": (f"Not purged: type {CONFIRM_WORD} to confirm.", True),
    "reprinted": ("Sent to the printer.", False),
    "print_failed": ("The printer couldn't print that. Check that it's switched on, has paper "
                     "and the lid is closed, then try again.", True),
    "print_missing": ("That print's image file is missing from the box, so it can't be reprinted.", True),
    "print_quiet": ("It's quiet hours, so the printer is resting. Tick “print anyway” to print now.", True),
    "print_unlogged": ("Printed, but the reprint couldn't be added to the archive.", True),
    "saved": ("Settings saved.", False),
    "password_changed": ("Password changed. Other devices are logged out.", False),
    "not_found": ("That item no longer exists.", True),
    "error": ("Something went wrong; it has been logged.", True),
}

_REC_ID = re.compile(r"^[A-Za-z0-9_]{1,100}$")  # docs/SYNC.md rec_id rule


@dataclass
class DashboardConfig:
    auth_file: Path | None = None            # default: <data_dir>/dashboard_auth.json
    cookie_secure: bool = False              # True when served over HTTPS only
    session_max_age_s: float = 30 * 86400
    login_max_failures: int = 5              # per client, per window
    login_max_global_failures: int = 20      # all clients, per window
    login_window_s: float = 300.0
    scrypt_n: int = SCRYPT_N
    archive_page_size: int = 200
    max_concurrent_kdf: int = 2              # scrypt calls at once (~16 MiB each)
    # When set, /setup (choosing the first parent password) needs this token
    # (?token=... or the form field), so nobody else on the LAN can claim the
    # dashboard before the parent does. The CLI generates one (see
    # zozobox.dashboard.__main__); None = open setup (tests, local dev).
    setup_token: str | None = None
    setup_token_file: Path | None = None     # removed once the password is set


LOOPBACK = ("127.0.0.1", "::1", "localhost")


def login_client_key(peer: str | None, headers) -> str:
    """Who a login attempt is counted against (LoginLimiter's per-client limit).

    Under pi-ops the dashboard binds 127.0.0.1 and is reached only through
    `tailscale serve`, so every request's socket peer is loopback. Keying on it
    would make the per-client limit one shared bucket: one parent's typos would
    lock every parent out. For a loopback peer, the Tailscale identity that
    serve adds is used instead: Tailscale-User-Login (a user's device; serve
    replaces any copy the client sent), else the last X-Forwarded-For entry
    (the one serve appended: the tailnet address of a tagged device). Without
    either it stays the shared loopback bucket. A non-loopback peer (a LAN
    bind, tests) is keyed on its address and the headers are ignored, since
    any client could send them. The global limit applies either way.
    """
    if not peer:
        return "unknown"
    if peer not in LOOPBACK and not peer.startswith("127."):
        return peer
    login = (headers.get("tailscale-user-login") or "").strip()
    if login and len(login) <= 256:
        return f"ts:{login.lower()}"
    xff = [p.strip() for p in (headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    if xff and len(xff[-1]) <= 64:
        return f"fwd:{xff[-1]}"
    return peer


class _NeedAuth(Exception):
    def __init__(self, next_path: str = "/"):
        self.next_path = next_path


class _BadCSRF(Exception):
    pass


class _NotFound(Exception):
    pass


def safe_next(target: str | None) -> str:
    """Only same-site absolute paths are allowed as a post-login redirect."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or any(ord(ch) < 32 for ch in target):
        return "/"
    return target


def resolve_media(media_dir: Path, rel: str) -> Path:
    """Map a URL path to a file under media_dir, refusing anything that could
    escape it (.., absolute paths, backslashes, NUL, hidden/temp files,
    symlinks pointing outside) or that isn't a known media type."""
    if not rel or len(rel) > 512 or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise _NotFound()
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") or p.startswith("~") for p in parts):
        raise _NotFound()
    ext = Path(parts[-1]).suffix.lower()
    if ext not in Q.MEDIA_TYPES:
        raise _NotFound()
    base = media_dir.resolve()
    try:
        path = (base / rel).resolve(strict=True)
        path.relative_to(base)
    except (OSError, ValueError, RuntimeError):
        raise _NotFound() from None
    if not path.is_file():
        raise _NotFound()
    return path


def _fmt_size(n: Any) -> str:
    try:
        f = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(f) < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def _fmt_remaining(s: Any) -> str:
    try:
        s = float(s)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return "review window over"
    m = int(math.ceil(s / 60))
    h, m = divmod(m, 60)
    if h and not m:
        return f"{h} h left"
    return (f"{h} h " if h else "") + f"{m} min left"


def create_app(
    store: Store,
    printer: Any,
    *,
    config: DashboardConfig | None = None,
    clock: Callable[[], float] = time.time,
) -> Any:
    """Build the dashboard app. `printer` implements zozobox.printer.Printer
    (print_image(img), raising PrinterError); `clock` returns Unix seconds and
    drives session expiry and login rate limiting (store times come from the
    store's own clock)."""
    from fastapi import Depends, FastAPI, Form, Request
    from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
    from jinja2 import DictLoader, Environment, select_autoescape

    cfg = config or DashboardConfig()
    pw = PasswordFile(cfg.auth_file or Path(store.data_dir) / AUTH_FILE_NAME, scrypt_n=cfg.scrypt_n)
    boot_secret = secrets.token_bytes(32)  # signs anonymous sessions before setup
    limiter = LoginLimiter(clock, cfg.login_max_failures, cfg.login_max_global_failures,
                           cfg.login_window_s)
    print_lock = threading.Lock()
    kdf_slots = threading.BoundedSemaphore(max(1, cfg.max_concurrent_kdf))
    zones_cache: list[str] = []

    env = Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(default=True),
                      trim_blocks=True, lstrip_blocks=True)

    def f_local(dt: Any, fmt: str = "%a %b %d %H:%M") -> str:
        if not isinstance(dt, datetime):
            return ""
        try:
            return store.settings.local(dt).strftime(fmt)
        except Exception:
            return dt.strftime(fmt)

    env.filters.update(
        local=f_local,
        size=_fmt_size,
        remaining=_fmt_remaining,
        media_kind=Q.media_kind,
        media_url=lambda rel: "/media/" + quote(str(rel), safe="/"),
        tojson_pretty=lambda v: json.dumps(v, indent=2, ensure_ascii=False, default=str),
    )

    app = FastAPI(title="Zozo box dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.printer = printer
    app.state.passwords = pw
    app.state.limiter = limiter

    def kdf(fn: Callable[..., Any], *args: Any) -> Any:
        """Run a scrypt-backed call with bounded concurrency (memory on a Pi 3)."""
        with kdf_slots:
            return fn(*args)

    # -- sessions -------------------------------------------------------------
    def secret() -> bytes:
        return pw.secret() or boot_secret

    def load_session(request: Request) -> Session:
        s = getattr(request.state, "session", None)
        if s is not None:
            return s
        now = clock()
        d = SessionCodec.decode(request.cookies.get(COOKIE_NAME), secret())
        s = None
        if d is not None:
            iat, csrf = d.get("iat"), d.get("csrf")
            if (isinstance(iat, (int, float)) and isinstance(csrf, str) and len(csrf) >= 16
                    and -300 <= now - iat <= cfg.session_max_age_s):
                s = Session(csrf=csrf, auth=bool(d.get("auth")), gen=str(d.get("gen") or ""),
                            iat=float(iat))
                gen = pw.generation()
                if s.auth and (gen is None or not hmac.compare_digest(s.gen, gen)):
                    s = None
        if s is None:
            s = new_session(now)
        request.state.session = s
        return s

    def start_session(request: Request, *, auth: bool) -> Session:
        s = new_session(clock(), auth=auth, gen=(pw.generation() or "") if auth else "")
        request.state.session = s
        return s

    def client_key(request: Request) -> str:
        return login_client_key(request.client.host if request.client else None,
                                request.headers)

    # -- dependencies -----------------------------------------------------------
    def authed(request: Request) -> Session:
        s = load_session(request)
        if not s.auth or not pw.exists():
            q = request.url.query
            raise _NeedAuth(request.url.path + (f"?{q}" if q else ""))
        return s

    def csrf_ok(request: Request, csrf_token: str = Form("")) -> Session:
        s = load_session(request)
        origin = request.headers.get("origin")
        if origin and origin != "null":
            host = request.headers.get("host", "")
            if urlsplit(origin).netloc.lower() != host.lower():
                raise _BadCSRF()
        if not csrf_token or not hmac.compare_digest(csrf_token.encode(), s.csrf.encode()):
            raise _BadCSRF()
        return s

    def authed_csrf(request: Request, s: Session = Depends(authed),
                    _c: Session = Depends(csrf_ok)) -> Session:
        return s

    # -- rendering --------------------------------------------------------------
    def render(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        s = load_session(request)
        code = request.query_params.get("msg")
        if "flash" not in ctx and code in FLASH:
            ctx["flash"], ctx["flash_bad"] = FLASH[code]
        nav_pending = 0
        if s.auth:
            nav_pending = Q.safe(lambda: len(Q.pending_approvals(store)), 0, "approvals")
        html = env.get_template(name).render(
            csrf=s.csrf, authed=s.auth, nav=NAV, active=ctx.pop("active", None),
            nav_pending=nav_pending, **ctx,
        )
        return HTMLResponse(html, status_code=status)

    def redirect(path: str, msg: str | None = None, **params: Any) -> RedirectResponse:
        q = {k: v for k, v in params.items() if v not in (None, "")}
        if msg:
            q["msg"] = msg
        return RedirectResponse(path + (("?" + urlencode(q)) if q else ""), status_code=303)

    # -- middleware / errors ------------------------------------------------------
    @app.middleware("http")
    async def headers_and_cookie(request: Request, call_next):
        response = await call_next(request)
        s = getattr(request.state, "session", None)
        if s is not None and s.dirty:
            token = SessionCodec.encode(s.payload(), secret())
            response.set_cookie(COOKIE_NAME, token, max_age=int(cfg.session_max_age_s),
                                httponly=True, samesite="lax", secure=cfg.cookie_secure,
                                path="/")
        h = response.headers
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; img-src 'self' data:; media-src 'self'; "
                     "style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; "
                     "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(_NeedAuth)
    async def need_auth(request: Request, exc: _NeedAuth):
        if not pw.exists():
            return RedirectResponse("/setup", status_code=303)
        nxt = safe_next(exc.next_path)
        return RedirectResponse("/login" + (f"?{urlencode({'next': nxt})}" if nxt != "/" else ""),
                                status_code=303)

    @app.exception_handler(_BadCSRF)
    async def bad_csrf(request: Request, exc: _BadCSRF):
        return render(request, "error.html", 403, heading="Form expired",
                      message="That form was out of date or came from somewhere else. "
                              "Go back, reload the page and try again.")

    @app.exception_handler(_NotFound)
    async def not_found(request: Request, exc: _NotFound):
        return render(request, "error.html", 404, heading="Not found",
                      message="That page or file doesn't exist.")

    @app.exception_handler(404)
    async def http_404(request: Request, exc: Exception):
        return render(request, "error.html", 404, heading="Not found",
                      message="That page or file doesn't exist.")

    @app.exception_handler(Exception)
    async def crashed(request: Request, exc: Exception):
        log.exception("dashboard: error handling %s %s", request.method, request.url.path)
        return HTMLResponse("<!doctype html><title>Error</title><p>Something went wrong; it has "
                            "been logged. <a href=\"/\">Back</a></p>", status_code=500)

    # -- static -----------------------------------------------------------------
    @app.get("/static/style.css", include_in_schema=False)
    def style_css():
        return Response(CSS, media_type="text/css",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/static/app.js", include_in_schema=False)
    def app_js():
        return Response(JS, media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    # -- setup / login / logout -----------------------------------------------------
    def _setup_token_ok(token: str) -> bool:
        if not cfg.setup_token:
            return True
        return hmac.compare_digest(token.encode(), cfg.setup_token.encode())

    def _setup_locked(request: Request):
        return render(request, "setup_locked.html", 403, title="Set up")

    @app.get("/setup")
    def setup_page(request: Request, token: str = ""):
        if pw.exists():
            return redirect("/login")
        if not _setup_token_ok(token):
            return _setup_locked(request)
        return render(request, "setup.html", title="Set up", setup_token=token)

    @app.post("/setup")
    def setup_submit(request: Request, _c: Session = Depends(csrf_ok),
                     password: str = Form(""), confirm: str = Form(""),
                     setup_token: str = Form("")):
        if pw.exists():
            return redirect("/login")
        if not _setup_token_ok(setup_token):
            return _setup_locked(request)
        err = check_new_password(password, confirm)
        if err:
            return render(request, "setup.html", 400, title="Set up", error=err,
                          setup_token=setup_token)
        if not kdf(pw.create, password):
            return redirect("/login")
        log.info("dashboard: parent password set")
        if cfg.setup_token_file is not None:
            try:
                cfg.setup_token_file.unlink(missing_ok=True)
            except OSError:
                log.warning("dashboard: could not remove %s", cfg.setup_token_file)
        start_session(request, auth=True)
        return redirect("/")

    @app.get("/login")
    def login_page(request: Request, next: str = "/"):
        if not pw.exists():
            return redirect("/setup")
        if load_session(request).auth:
            return redirect(safe_next(next))
        return render(request, "login.html", title="Log in", next=safe_next(next))

    def _too_many(request: Request, wait: float, template: str, **ctx):
        mins = max(1, int(math.ceil(wait / 60)))
        resp = render(request, template, 429,
                      error=f"Too many wrong passwords. Try again in about {mins} min.", **ctx)
        resp.headers["Retry-After"] = str(int(math.ceil(wait)))
        return resp

    @app.post("/login")
    def login_submit(request: Request, _c: Session = Depends(csrf_ok),
                     password: str = Form(""), next: str = Form("/")):
        if not pw.exists():
            return redirect("/setup")
        nxt = safe_next(next)
        who = client_key(request)
        wait, token = limiter.reserve(who)  # counted before the slow check
        if wait > 0:
            return _too_many(request, wait, "login.html", title="Log in", next=nxt)
        if not kdf(pw.verify, password):
            log.warning("dashboard: failed login from %s", who)
            return render(request, "login.html", 401, title="Log in", next=nxt,
                          error="That password isn't right.")
        limiter.success(who, token)
        start_session(request, auth=True)
        return RedirectResponse(nxt, status_code=303)

    @app.post("/logout")
    def logout(request: Request, _c: Session = Depends(csrf_ok)):
        start_session(request, auth=False)
        return redirect("/login")

    # -- home -------------------------------------------------------------------------
    @app.get("/")
    def home(request: Request, _s: Session = Depends(authed)):
        return render(request, "home.html", active="/", title="Home", h=Q.home(store))

    # -- approvals --------------------------------------------------------------------
    @app.get("/approvals")
    def approvals(request: Request, _s: Session = Depends(authed)):
        items = Q.safe(lambda: Q.pending_approvals(store), [], "approvals")
        until = Q.safe(lambda: store.settings.approval_required_until, None, "settings")
        return render(request, "approvals.html", active="/approvals", title="Approvals",
                      items=items, approval_until=until)

    @app.post("/approvals/{item_id}/{action}")
    def approval_action(request: Request, item_id: str, action: str,
                        _s: Session = Depends(authed_csrf)):
        if action not in ("approve", "reject"):
            raise _NotFound()
        try:
            item = store.content.get(item_id)
        except StoreError:
            return redirect("/approvals", "not_found")
        if action == "approve":
            if item.safety.value == "failed":
                return redirect("/approvals", "error")
            store.content.approve(item_id)
        else:
            store.content.reject(item_id)
        _log_event(f"dashboard_{action}", {"content_id": item_id, "kind": item.kind.value})
        return redirect("/approvals", "approved" if action == "approve" else "rejected")

    def _log_event(type_: str, payload: dict[str, Any]) -> None:
        try:
            store.events.append(type_, payload)
        except Exception:
            log.exception("dashboard: could not log %s", type_)

    # -- archive ----------------------------------------------------------------------
    @app.get("/archive")
    def archive(request: Request, kind: str = "story", day: str = "",
                _s: Session = Depends(authed)):
        if kind not in Q.ARCHIVE_KINDS:
            kind = "story"
        d = Q.parse_day(day)
        items = Q.safe(lambda: Q.archive(store, kind, d, cfg.archive_page_size), [], "archive")
        return render(request, "archive.html", active="/archive", title="Archive", kind=kind,
                      kinds=Q.ARCHIVE_KINDS, content_kinds=Q.CONTENT_KINDS,
                      day=d.isoformat() if d else "", items=items)

    @app.get("/archive/item/{item_id}")
    def archive_item(request: Request, item_id: str, _s: Session = Depends(authed)):
        item = store.content.find(item_id)
        if item is None:
            raise _NotFound()
        return render(request, "item.html", active="/archive", title="Archive", item=item)

    @app.post("/archive/purge")
    def purge(request: Request, _s: Session = Depends(authed_csrf), target: str = Form(""),
              item_id: str = Form(""), confirm: str = Form("")):
        back_kind = "recordings" if target == "recording" else None
        if target == "content":
            item = store.content.find(item_id)
            if item is None:
                return redirect("/archive", "not_found")
            back_kind = item.kind.value
            if confirm.strip() != CONFIRM_WORD:
                return redirect(f"/archive/item/{quote(item_id)}", "purge_confirm")
            store.content.purge(item_id, confirm=True)
        elif target == "recording":
            rec = store.recordings.find(item_id)
            if rec is None:
                return redirect("/archive", "not_found", kind="recordings")
            if confirm.strip() != CONFIRM_WORD:
                return redirect("/archive", "purge_confirm", kind="recordings")
            if rec.slot is Slot.ZOZO and rec.status is not RecStatus.WITHHELD:
                # The cloud may already have it (pending or published): tell it
                # to withhold before the box forgets the message. If that
                # can't be queued, don't purge (the parent can retry).
                try:
                    withhold_recording(store, item_id)
                except Exception:
                    log.exception("dashboard: could not withhold %s before purging", item_id)
                    return redirect("/archive", "error", kind="recordings")
            store.recordings.purge(item_id, confirm=True)
        else:
            raise _NotFound()
        log.info("dashboard: purged %s %s", target, item_id)
        _log_event("dashboard_purge", {"target": target, "id": item_id})
        return redirect("/archive", "purged", kind=back_kind)

    # -- recordings -------------------------------------------------------------------
    @app.get("/recordings")
    def recordings(request: Request, _s: Session = Depends(authed)):
        zozo = Q.safe(lambda: Q.review_info(store, store.recordings.live(Slot.ZOZO)), None,
                      "zozo message")
        grandma = Q.safe(lambda: store.recordings.live(Slot.GRANDMA), None, "grandma message")
        history = Q.safe(lambda: store.recordings.history(limit=200), [], "recording history")
        return render(request, "recordings.html", active="/recordings", title="Recordings",
                      zozo=zozo, grandma=grandma, history=history)

    @app.post("/recordings/{rec_id}/withhold")
    def withhold(request: Request, rec_id: str, _s: Session = Depends(authed_csrf)):
        if not _REC_ID.match(rec_id):
            return redirect("/recordings", "not_found")
        try:
            rec = withhold_recording(store, rec_id)
        except StoreError:
            return redirect("/recordings", "not_found")
        return redirect("/recordings",
                        "withheld_zozo" if rec.slot is Slot.ZOZO else "withheld_grandma")

    # -- mood / stats -----------------------------------------------------------------
    def _days(v: str, default: int) -> int:
        try:
            return max(1, min(int(v), 366))
        except (TypeError, ValueError):
            return default

    @app.get("/mood")
    def mood(request: Request, days: str = "30", _s: Session = Depends(authed)):
        n = _days(days, 30)
        rows, summary = Q.safe(lambda: Q.mood_log(store, n), ([], []), "mood log")
        return render(request, "mood.html", active="/mood", title="Mood log", rows=rows,
                      summary=summary, days=n)

    @app.get("/stats")
    def stats(request: Request, days: str = "14", _s: Session = Depends(authed)):
        n = _days(days, 14)
        rows, cols = Q.safe(lambda: Q.stats(store, n), ([], []), "stats")
        return render(request, "stats.html", active="/stats", title="Stats", rows=rows,
                      cols=cols, days=n)

    # -- reprint ----------------------------------------------------------------------
    @app.get("/reprint")
    def reprint_page(request: Request, day: str = "", _s: Session = Depends(authed)):
        d = Q.parse_day(day)
        items = Q.safe(lambda: store.events.query("print", day_from=d, day_to=d, limit=100,
                                                  newest_first=True), [], "prints")
        quiet = Q.safe(store.is_quiet, False, "quiet hours")
        return render(request, "reprint.html", active="/reprint", title="Reprint", items=items,
                      day=d.isoformat() if d else "", quiet=quiet)

    @app.post("/reprint/{event_id}")
    def reprint(request: Request, event_id: int, _s: Session = Depends(authed_csrf),
                force: str = Form("")):
        msg = reprint_event(store, printer, event_id, force=bool(force), lock=print_lock)
        return redirect("/reprint", msg)

    # -- settings ---------------------------------------------------------------------
    def zones() -> list[str]:
        if not zones_cache:
            zones_cache.extend(sorted(zoneinfo.available_timezones()))
        return zones_cache

    def settings_form() -> dict[str, Any]:
        s = store.settings
        loc = s.location
        q = s.quiet_hours
        until = s.approval_required_until
        return {
            "quiet_start": q.start.strftime("%H:%M"), "quiet_end": q.end.strftime("%H:%M"),
            "timezone": s.timezone, "day_start": s.day_start.strftime("%H:%M"),
            "location_name": loc.name if loc else "",
            "location_lat": f"{loc.lat:g}" if loc else "",
            "location_lon": f"{loc.lon:g}" if loc else "",
            "rec_cap_s": f"{s.rec_cap_s:g}", "volume": f"{s.volume:g}",
            "spend_cap_usd": f"{s.spend_cap_usd:g}",
            "review_delay_min": f"{s.review_delay_s / 60:g}",
            "approval_required_until": until.isoformat() if until else "",
        }

    def render_settings(request: Request, form: dict[str, Any], errors: list[str],
                        status: int = 200):
        zl = zones()
        if form.get("timezone") and form["timezone"] not in zl:
            zl = [form["timezone"], *zl]
        return render(request, "settings.html", status, active="/settings", title="Settings",
                      f=form, errors=errors, zones=zl)

    @app.get("/settings")
    def settings_page(request: Request, _s: Session = Depends(authed)):
        return render_settings(request, settings_form(), [])

    @app.post("/settings")
    async def settings_submit(request: Request, _s: Session = Depends(authed_csrf)):
        form = {k: str(v) for k, v in (await request.form()).items() if k != "csrf_token"}
        values, errors = parse_settings_form(form)
        if not errors:
            try:
                before = store.settings.as_dict()
                store.settings.update(values)
                after = store.settings.as_dict()
                changed = sorted(k for k in after if before.get(k) != after.get(k))
                if changed:
                    _log_event("settings_changed", {"keys": changed})
                return redirect("/settings", "saved")
            except SettingsError as e:
                errors.append(str(e))
        shown = settings_form()
        shown.update({k: v for k, v in form.items() if k in shown})
        return render_settings(request, shown, errors, 400)

    @app.post("/settings/password")
    def change_password(request: Request, _s: Session = Depends(authed_csrf),
                        current: str = Form(""), password: str = Form(""),
                        confirm: str = Form("")):
        who = client_key(request)
        wait, token = limiter.reserve(who)  # counted before the slow check
        if wait > 0:
            return render_settings(request, settings_form(),
                                   ["Too many wrong passwords. Try again later."], 429)
        if not kdf(pw.verify, current):
            return render_settings(request, settings_form(),
                                   ["Your current password isn't right."], 400)
        limiter.success(who, token)
        err = check_new_password(password, confirm)
        if err:
            return render_settings(request, settings_form(), [err], 400)
        kdf(pw.change, password)
        start_session(request, auth=True)
        log.info("dashboard: parent password changed")
        return redirect("/settings", "password_changed")

    # -- media ------------------------------------------------------------------------
    @app.get("/media/{rel:path}")
    def media(rel: str, _s: Session = Depends(authed)):
        path = resolve_media(store.media_dir, rel)
        return FileResponse(path, media_type=Q.MEDIA_TYPES[path.suffix.lower()],
                            headers={"Cache-Control": "private, max-age=3600",
                                     "Content-Disposition": "inline"})

    return app


# -- actions (also usable without HTTP) ---------------------------------------------


def withhold_recording(store: Store, rec_id: str):
    """Parent delete of a voice message. Zozo's message is withheld locally
    AND a control record goes into the outbox so the cloud never publishes it
    (or unpublishes it): control/zozo/<rec_id>-withhold.json (docs/SYNC.md).
    Grandma's messages are withheld locally only. Raises StoreError if unknown."""
    rec = store.recordings.get(rec_id)
    with store.db.tx():
        rec = store.recordings.withhold(rec_id)
        if rec.slot is Slot.ZOZO:
            store.outbox.enqueue(
                f"control/zozo/{rec_id}-withhold.json",
                record={"action": "withhold", "rec_id": rec_id},
                content_type="application/json",
            )
        store.events.append(WITHHOLD_EVENT, {"rec_id": rec_id, "slot": rec.slot.value})
    log.info("dashboard: withheld %s %s", rec.slot.value, rec_id)
    return rec


def reprint_event(store: Store, printer: Any, event_id: int, *, force: bool = False,
                  lock: "threading.Lock | None" = None) -> str:
    """Reprint the archived PNG of a print event. Returns a FLASH code.
    Reprints never touch daily print state; the reprint is archived as its
    own print event (reprint=True)."""
    from PIL import Image

    try:
        ev = store.events.get(int(event_id))
    except (StoreError, ValueError, TypeError):
        return "not_found"
    if ev.type != "print" or not ev.media:
        return "not_found"
    if not force and store.is_quiet():
        return "print_quiet"
    try:
        data = store.media_path(ev.media).read_bytes()
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        log.exception("dashboard: cannot load print image %s", ev.media)
        return "print_missing"
    try:
        if lock is not None:
            with lock:
                printer.print_image(img)
        else:
            printer.print_image(img)
    except Exception as e:  # PrinterError or anything else from the backend
        log.warning("dashboard: reprint of event %s failed: %s", event_id, e)
        try:
            store.events.append("error", {"kind": "reprint_failed", "event_id": ev.id,
                                          "error": f"{type(e).__name__}: {e}"[:300]})
        except Exception:
            log.exception("dashboard: could not log reprint failure")
        return "print_failed"
    try:
        extra = {"source_event": ev.id}
        for k in ("letter", "mood"):
            if k in ev.payload:
                extra[k] = ev.payload[k]
        store.events.record_print(str(ev.payload.get("item") or "print"), data, reprint=True,
                                  content_id=ev.payload.get("content_id"), extra=extra)
    except Exception:
        log.exception("dashboard: reprint printed but could not be archived")
        return "print_unlogged"
    return "reprinted"


SETTINGS_LABELS = {
    "quiet_hours": "Quiet hours", "timezone": "Time zone", "day_start": "Day start",
    "location": "Weather location", "rec_cap_s": "Recording cap", "volume": "Volume",
    "spend_cap_usd": "Spend cap", "review_delay_s": "Review delay",
    "approval_required_until": "Approval required until",
}


def parse_settings_form(form: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Turn the settings form into store values. Each value is validated with
    the store's own setting specs, so the rules (e.g. rec cap 1..300 s) live
    in one place. Returns (values, error messages)."""
    errors: list[str] = []
    values: dict[str, Any] = {}

    def g(name: str) -> str:
        return (form.get(name) or "").strip()

    def number(name: str, label: str) -> float | None:
        raw = g(name)
        try:
            f = float(raw)
        except ValueError:
            errors.append(f"{label}: enter a number.")
            return None
        if not math.isfinite(f):
            errors.append(f"{label}: enter a number.")
            return None
        return f

    raw: dict[str, Any] = {
        "quiet_hours": {"start": g("quiet_start"), "end": g("quiet_end")},
        "timezone": g("timezone"),
        "day_start": g("day_start"),
        "approval_required_until": g("approval_required_until") or None,
    }
    for key, field, label in (("rec_cap_s", "rec_cap_s", "Recording cap"),
                              ("volume", "volume", "Volume"),
                              ("spend_cap_usd", "spend_cap_usd", "Spend cap")):
        v = number(field, label)
        if v is not None:
            raw[key] = v
    minutes = number("review_delay_min", "Review delay")
    if minutes is not None:
        raw["review_delay_s"] = int(round(minutes * 60))
    name, lat, lon = g("location_name"), g("location_lat"), g("location_lon")
    if not (name or lat or lon):
        raw["location"] = None
    else:
        la = number("location_lat", "Latitude")
        lo = number("location_lon", "Longitude")
        if la is not None and lo is not None:
            raw["location"] = {"name": name, "lat": la, "lon": lo}
    for key, v in raw.items():
        try:
            values[key] = SPECS[key].parse(v)
        except (SettingsError, ValueError, TypeError) as e:
            errors.append(f"{SETTINGS_LABELS.get(key, key)}: {e}")
    return values, errors
