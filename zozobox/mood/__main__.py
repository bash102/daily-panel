"""CLI: python -m zozobox.mood reprocess [--data-dir DIR] [--limit N] [--dry-run]

Recognizes mood takes that were recorded while offline. The data dir comes
from --data-dir or ZOZOBOX_DATA_DIR. API keys come from OPENAI_API_KEY and
ANTHROPIC_API_KEY. Exit code: 0 done, 1 bad usage/store error, 2 stopped
early (offline or spend cap) with takes left pending.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Callable, Sequence

DATA_DIR_ENV = "ZOZOBOX_DATA_DIR"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m zozobox.mood", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reprocess", help="recognize pending (offline) mood takes")
    r.add_argument("--data-dir", default=os.environ.get(DATA_DIR_ENV),
                   help=f"store data dir (default ${DATA_DIR_ENV})")
    r.add_argument("--limit", type=int, default=None, help="process at most N takes")
    r.add_argument("--dry-run", action="store_true", help="only count pending takes")
    r.add_argument("-v", "--verbose", action="store_true")
    return p


def main(
    argv: Sequence[str] | None = None,
    *,
    make_store: Callable | None = None,
    make_classifier: Callable | None = None,
) -> int:
    """make_store(data_dir) / make_classifier(store) are injectable for tests."""
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.data_dir:
        print(f"error: pass --data-dir or set {DATA_DIR_ENV}", file=sys.stderr)
        return 1

    from .classifier import OpenAIClaudeMoodClassifier
    from .reprocess import PURPOSE, reprocess_pending

    if make_store is None:
        from ..store import Store
        make_store = Store
    if make_classifier is None:
        def make_classifier(store):
            return OpenAIClaudeMoodClassifier.from_store(
                store, stt_timeout_s=30.0, claude_timeout_s=60.0,
                purpose_suffix="_offline", deadline_s=None,
            )
    try:
        store = make_store(args.data_dir)
    except Exception as e:
        print(f"error: cannot open store: {e}", file=sys.stderr)
        return 1
    try:
        clf = None if args.dry_run else make_classifier(store)
        rep = reprocess_pending(store, clf, limit=args.limit, dry_run=args.dry_run)
    except Exception as e:  # pragma: no cover - reprocess_pending guards itself
        logging.getLogger(__name__).exception("mood reprocess failed")
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(json.dumps(rep.as_dict()))
    return 2 if rep.stopped else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
