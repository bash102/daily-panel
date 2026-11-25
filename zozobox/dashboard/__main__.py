"""CLI: python -m zozobox.dashboard --data-dir DIR [--host 0.0.0.0] [--port 8080]

    --printer none           reprints always fail politely (default)
    --printer pkg.mod:func   call func() for the Printer. Production should use
                             M10's cross-process locked printer here: the
                             engine drives the same USB QL-800.
    --printer brother        a direct, UNLOCKED Brother QL-800 (brother_ql).
                             Only when the engine isn't printing: two processes
                             writing to the device at once garble prints.
    --printer fake[:DIR]     FakePrinter, saving PNGs to DIR if given
    --secure-cookies         set the Secure flag (only when served over HTTPS)

Env fallbacks: ZOZOBOX_DATA_DIR, ZOZOBOX_DASHBOARD_HOST, ZOZOBOX_DASHBOARD_PORT,
ZOZOBOX_DASHBOARD_PRINTER.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from typing import Any, Sequence

DEFAULT_DATA_DIR = "/var/lib/zozobox"

log = logging.getLogger("zozobox.dashboard")


class NoPrinter:
    def print_image(self, img: Any) -> None:
        from zozobox.printer import PrinterError

        raise PrinterError("no printer configured for the dashboard")


def make_printer(spec: str) -> Any:
    if spec == "none":
        return NoPrinter()
    if spec == "brother":
        from zozobox.printer import BrotherQLPrinter

        return BrotherQLPrinter()
    if spec == "fake" or spec.startswith("fake:"):
        from zozobox.printer import FakePrinter

        return FakePrinter(spec[5:] or None)
    if ":" in spec:
        mod, _, attr = spec.partition(":")
        return getattr(importlib.import_module(mod), attr)()
    raise ValueError(f"unknown printer {spec!r}")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zozobox.dashboard",
                                 description="Zozo box parent dashboard")
    ap.add_argument("--data-dir", default=os.environ.get("ZOZOBOX_DATA_DIR", DEFAULT_DATA_DIR))
    ap.add_argument("--host", default=os.environ.get("ZOZOBOX_DASHBOARD_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("ZOZOBOX_DASHBOARD_PORT", "8080")))
    ap.add_argument("--printer", default=os.environ.get("ZOZOBOX_DASHBOARD_PRINTER", "none"))
    ap.add_argument("--secure-cookies", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from zozobox.store import Store

    from .app import DashboardConfig, create_app

    if args.printer == "none":
        log.warning("dashboard: no printer configured (--printer); reprints are disabled")
    elif args.printer == "brother":
        log.warning("dashboard: using an unlocked Brother printer; it can collide with the "
                    "engine's prints. Use a locked printer (--printer pkg.mod:func) in production")
    try:
        printer = make_printer(args.printer)
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

        app = create_app(store, printer, config=DashboardConfig(cookie_secure=args.secure_cookies))
        uvicorn.run(app, host=args.host, port=args.port, log_level="info" if args.verbose
                    else "warning", proxy_headers=False, server_header=False)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
