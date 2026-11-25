"""CLI: python -m zozobox.dashboard --data-dir DIR [--host 0.0.0.0] [--port 8080]

    --printer none           reprints always fail politely (default)
    --printer brother        the Brother QL-800 behind the cross-process printer
                             lock (<data-dir>/.printer.lock), shared with the
                             engine, which drives the same USB device. This is
                             what deploy/systemd/zozobox-dashboard.service uses.
                             `locked` is an alias.
    --printer pkg.mod:func   call func() for the Printer
    --printer fake[:DIR]     FakePrinter, saving PNGs to DIR if given
    --secure-cookies         set the Secure flag (only when served over HTTPS)
    --config PATH            box config for `brother` ([printer] identifier/model/
                             label/backend, [app].printer_lock_timeout_s); default
                             $ZOZOBOX_CONFIG or /etc/zozobox/box.toml, the same
                             file the engine reads (missing = defaults)
    --open-setup             no setup token (local development only)

First-run setup: while no parent password exists, the dashboard writes a
one-time token to <data-dir>/dashboard-setup-token (mode 0600) and logs the
setup path; /setup only works with ?token=<token>, so nobody else on the LAN
can claim the parent account first. The file is removed once a password
exists.

Env fallbacks: ZOZOBOX_DATA_DIR, ZOZOBOX_DASHBOARD_HOST, ZOZOBOX_DASHBOARD_PORT,
ZOZOBOX_DASHBOARD_PRINTER, ZOZOBOX_CONFIG.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Sequence

DEFAULT_DATA_DIR = "/var/lib/zozobox"
SETUP_TOKEN_FILE = "dashboard-setup-token"

log = logging.getLogger("zozobox.dashboard")


class NoPrinter:
    def print_image(self, img: Any) -> None:
        from zozobox.printer import PrinterError

        raise PrinterError("no printer configured for the dashboard")


def make_printer(spec: str, data_dir: str | None = None, config: str | None = None) -> Any:
    """`config`: box.toml path for `brother` (None = $ZOZOBOX_CONFIG or
    /etc/zozobox/box.toml). Raises ConfigError on a bad file."""
    if spec == "none":
        return NoPrinter()
    if spec in ("brother", "locked"):
        from zozobox.boxconfig import load_app_config
        from zozobox.printer import make_locked_brother

        cfg = load_app_config(config, required=bool(config))
        return make_locked_brother(
            data_dir or os.environ.get("ZOZOBOX_DATA_DIR", DEFAULT_DATA_DIR), cfg.printer,
            retry_s=cfg.app.printer_retry_s, lock_timeout_s=cfg.app.printer_lock_timeout_s)
    if spec == "fake" or spec.startswith("fake:"):
        from zozobox.printer import FakePrinter

        return FakePrinter(spec[5:] or None)
    if ":" in spec:
        mod, _, attr = spec.partition(":")
        return getattr(importlib.import_module(mod), attr)()
    raise ValueError(f"unknown printer {spec!r}")


def setup_token(data_dir: str | os.PathLike) -> str | None:
    """The one-time token for /setup while no parent password exists (kept
    across restarts in <data-dir>/dashboard-setup-token, mode 0600), or None
    once a password is set (the token file is then removed)."""
    from .app import AUTH_FILE_NAME

    d = Path(data_dir)
    path = d / SETUP_TOKEN_FILE
    if (d / AUTH_FILE_NAME).exists():
        path.unlink(missing_ok=True)
        return None
    try:
        token = path.read_text().strip()
    except FileNotFoundError:
        token = ""
    if len(token) < 16:
        import secrets

        token = secrets.token_urlsafe(18)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
    os.chmod(path, 0o600)
    log.warning("dashboard: no parent password yet. Open http://<box>:<port>/setup?token=%s "
                "(the token is also in %s)", token, path)
    return token


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zozobox.dashboard",
                                 description="Zozo box parent dashboard")
    ap.add_argument("--data-dir", default=os.environ.get("ZOZOBOX_DATA_DIR", DEFAULT_DATA_DIR))
    ap.add_argument("--host", default=os.environ.get("ZOZOBOX_DASHBOARD_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("ZOZOBOX_DASHBOARD_PORT", "8080")))
    ap.add_argument("--printer", default=os.environ.get("ZOZOBOX_DASHBOARD_PRINTER", "none"))
    ap.add_argument("--secure-cookies", action="store_true")
    ap.add_argument("--config", default=None, help="box config TOML for --printer brother")
    ap.add_argument("--open-setup", action="store_true",
                    help="no setup token for the first password (local development only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from zozobox.store import Store

    from .app import DashboardConfig, create_app

    if args.printer == "none":
        log.warning("dashboard: no printer configured (--printer); reprints are disabled")
    try:
        printer = make_printer(args.printer, args.data_dir, args.config)
    except Exception as e:
        print(f"dashboard: cannot set up printer {args.printer!r}: {e}", file=sys.stderr)
        return 2
    try:
        store = Store(args.data_dir)  # never recover_outbox: the sync client owns leases
    except Exception as e:
        print(f"dashboard: cannot open store at {args.data_dir}: {e}", file=sys.stderr)
        return 2
    try:
        import uvicorn

        token = None if args.open_setup else setup_token(args.data_dir)
        app = create_app(store, printer, config=DashboardConfig(
            cookie_secure=args.secure_cookies, setup_token=token,
            setup_token_file=Path(args.data_dir) / SETUP_TOKEN_FILE if token else None))
        uvicorn.run(app, host=args.host, port=args.port, log_level="info" if args.verbose
                    else "warning", proxy_headers=False, server_header=False)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
