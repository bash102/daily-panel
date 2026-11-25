"""CLI: python -m zozobox.sync {once,run} [--data-dir DIR] [--interval S] [-v]

Drains the upload outbox to the cloud worker, pushes config changes and
imports grandma's new messages (docs/SYNC.md).

Environment:
    ZOZOBOX_CLOUD_URL   worker base URL (its PUBLIC_BASE_URL), https:// only;
                        plain http only for localhost (tests/development)
    ZOZOBOX_BOX_TOKEN   bearer token (the worker's BOX_TOKEN secret)
    ZOZOBOX_DATA_DIR    store location (default /var/lib/zozobox)

This must be the only process that opens the store with recover_outbox=True.
Exit codes: 0 ok, 1 usage/config/store error (missing, placeholder or
non-https cloud settings), 2 `once` finished with errors
(offline or rejected uploads; they stay queued).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from typing import Callable, Sequence

CLOUD_URL_ENV = "ZOZOBOX_CLOUD_URL"
TOKEN_ENV = "ZOZOBOX_BOX_TOKEN"
DATA_DIR_ENV = "ZOZOBOX_DATA_DIR"
DEFAULT_DATA_DIR = "/var/lib/zozobox"


PLACEHOLDERS = ("replace-me", "example.workers.dev", "<", ">")


def check_cloud_config(url: str, token: str) -> str | None:
    """Why the cloud settings are unusable (None = fine). The box token is
    only ever sent to a real https worker URL, never to a template value."""
    if not url or not token:
        return f"set {CLOUD_URL_ENV} and {TOKEN_ENV}"
    for name, val in ((CLOUD_URL_ENV, url), (TOKEN_ENV, token)):
        if any(p in val.lower() for p in PLACEHOLDERS):
            return f"{name} is still a placeholder (edit /etc/zozobox/secrets.env)"
    from urllib.parse import urlsplit

    try:
        u = urlsplit(url)
        host = u.hostname or ""
    except ValueError:
        return f"{CLOUD_URL_ENV} is not a valid URL"
    if not host or u.username or u.password:
        return f"{CLOUD_URL_ENV} is not a valid URL"
    if u.scheme == "https":
        return None
    if u.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"):
        return None
    return f"{CLOUD_URL_ENV} must be an https:// URL"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m zozobox.sync", description=__doc__.split("\n")[0])
    p.add_argument("cmd", choices=("once", "run"), help="one pass, or loop until stopped")
    p.add_argument("--data-dir", default=os.environ.get(DATA_DIR_ENV, DEFAULT_DATA_DIR))
    p.add_argument("--interval", type=float, default=30.0, help="seconds between passes (run)")
    p.add_argument("--max-backoff", type=float, default=600.0, help="max seconds between passes when offline")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(
    argv: Sequence[str] | None = None,
    *,
    make_store: Callable | None = None,
    make_client: Callable | None = None,
    stop: threading.Event | None = None,
) -> int:
    """make_store(data_dir) and make_client(store, url, token) are injectable for tests."""
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = os.environ.get(CLOUD_URL_ENV, "").strip()
    token = os.environ.get(TOKEN_ENV, "").strip()
    problem = check_cloud_config(url, token)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1

    if make_store is None:
        from ..store import Store

        def make_store(data_dir):
            return Store(data_dir, recover_outbox=True)
    if make_client is None:
        from .client import SyncClient

        def make_client(store, url, token):
            return SyncClient(store, url, token)

    try:
        store = make_store(args.data_dir)
    except Exception as e:
        print(f"error: cannot open store: {e}", file=sys.stderr)
        return 1
    try:
        try:
            client = make_client(store, url, token)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.cmd == "once":
            rep = client.run_once()
            print(f"uploaded={rep.uploaded} failed={rep.failed} announced={len(rep.announced)} "
                  f"imported={len(rep.imported)} published={len(rep.published)} "
                  f"config_queued={rep.config_queued} online={rep.online}")
            for e in rep.errors:
                print(f"error: {e}", file=sys.stderr)
            return 0 if rep.ok else 2
        stop = stop or threading.Event()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
        client.run(stop, interval_s=args.interval, max_backoff_s=args.max_backoff)
        return 0
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
