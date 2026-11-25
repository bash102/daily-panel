"""CLI: python -m zozobox.content {nightly,bootstrap,weather,status}

    --data-dir DIR   store location (default $ZOZOBOX_DATA_DIR or /var/lib/zozobox)

Exit status: 0 when every job ended generated/skipped/fallback (or status
ran), 1 when some job failed, timed out or hit the spend cap, 2 on usage or
store errors. Output is JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Sequence

DEFAULT_DATA_DIR = "/var/lib/zozobox"
OK = {"generated", "skipped", "fallback", "ready"}


def _bad(results: Any) -> bool:
    if isinstance(results, dict):
        if results.get("locked"):
            return True
        return any(_bad(v) for v in results.values())
    return isinstance(results, str) and results not in OK


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zozobox.content",
                                 description="Zozo box nightly content pipeline")
    ap.add_argument("command", choices=["nightly", "bootstrap", "weather", "status"])
    ap.add_argument("--data-dir", default=os.environ.get("ZOZOBOX_DATA_DIR", DEFAULT_DATA_DIR))
    ap.add_argument("--force", action="store_true",
                    help="weather: refresh even if today's exists; bootstrap: redo phrases")
    ap.add_argument("--job", action="append", choices=["weather", "story", "letter", "coloring"],
                    help="nightly: run only these jobs (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from zozobox.store import Store

    from .pipeline import (NIGHTLY_JOBS, Clients, ContentConfig, Pipeline, run_bootstrap,
                           status)

    try:
        store = Store(args.data_dir)
    except Exception as e:
        print(json.dumps({"error": f"cannot open store: {type(e).__name__}: {e}"}))
        return 2
    try:
        if args.command == "status":
            out: Any = status(store)
            print(json.dumps(out, indent=2, default=str))
            return 0
        config = ContentConfig.from_env()
        clients = Clients.from_env(config)
        if args.command == "bootstrap":
            out = run_bootstrap(store, clients, config=config, force_phrases=args.force)
        elif args.command == "weather":
            out = Pipeline(store, clients, config=config).run_nightly(
                jobs=("weather",), force_weather=args.force)
        else:
            jobs = tuple(args.job) if args.job else NIGHTLY_JOBS
            out = Pipeline(store, clients, config=config).run_nightly(jobs=jobs)
        print(json.dumps(out, indent=2, default=str))
        return 1 if _bad(out) else 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
