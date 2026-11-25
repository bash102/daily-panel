"""Parent dashboard (PLAN M9, SPEC 8): a FastAPI app in its own process.

    from zozobox.dashboard import create_app
    app = create_app(store, printer)     # printer: zozobox.printer.Printer

CLI: python -m zozobox.dashboard --data-dir DIR --host 127.0.0.1 --port 8791
FastAPI/Jinja2 are imported lazily inside create_app.
"""

from .app import (
    COOKIE_NAME,
    DashboardConfig,
    create_app,
    parse_settings_form,
    reprint_event,
    resolve_media,
    withhold_recording,
)
from .auth import AUTH_FILE_NAME, LoginLimiter, PasswordFile, SessionCodec

__all__ = [
    "COOKIE_NAME", "DashboardConfig", "create_app", "parse_settings_form", "reprint_event",
    "resolve_media", "withhold_recording",
    "AUTH_FILE_NAME", "LoginLimiter", "PasswordFile", "SessionCodec",
]
