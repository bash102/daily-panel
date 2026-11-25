"""The sync client's own small persistent state, as JSON next to the DB.

<data_dir>/sync_state.json holds:
- last_config: the config last queued for control/config.json
- announced: Zozo recording ids already queued as control/zozo/<id>.json (bounded)
- grandma_since: recorded_at watermark for GET /api/grandma/messages
- grandma_imported: grandma message ids already imported (bounded); with the
  store's "recording" events they stop a purged message from coming back
- grandma_skipped / grandma_failures: messages given up on after repeated
  permanent failures, and the failure counts of those still being retried
- status: last run summary (for the dashboard: load_status(data_dir))

Written atomically (temp file + rename). A missing or corrupt file starts
fresh: the worst case is re-sending idempotent commands.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATE_FILE = "sync_state.json"
MAX_ANNOUNCED = 500
MAX_GRANDMA_IDS = 2000


@dataclass
class SyncStatus:
    last_run_at: str | None = None
    last_ok_at: str | None = None
    online: bool | None = None
    last_error: str | None = None
    uploaded_total: int = 0
    imported_total: int = 0


@dataclass
class SyncState:
    last_config: dict[str, Any] | None = None
    announced: list[str] = field(default_factory=list)
    grandma_since: str | None = None
    grandma_imported: list[str] = field(default_factory=list)
    grandma_skipped: list[str] = field(default_factory=list)
    grandma_failures: dict[str, int] = field(default_factory=dict)
    status: SyncStatus = field(default_factory=SyncStatus)

    def is_announced(self, rec_id: str) -> bool:
        return rec_id in self.announced

    def add_announced(self, rec_id: str) -> None:
        if rec_id not in self.announced:
            self.announced.append(rec_id)
            del self.announced[:-MAX_ANNOUNCED]

    def is_grandma_imported(self, rec_id: str) -> bool:
        return rec_id in self.grandma_imported

    def add_grandma_imported(self, rec_id: str) -> None:
        if rec_id not in self.grandma_imported:
            self.grandma_imported.append(rec_id)
            del self.grandma_imported[:-MAX_GRANDMA_IDS]

    def add_grandma_skipped(self, rec_id: str) -> None:
        if rec_id not in self.grandma_skipped:
            self.grandma_skipped.append(rec_id)
            del self.grandma_skipped[:-MAX_GRANDMA_IDS]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SyncState":
        st = d.get("status") if isinstance(d.get("status"), dict) else {}
        known = SyncStatus.__dataclass_fields__
        return cls(
            last_config=d.get("last_config") if isinstance(d.get("last_config"), dict) else None,
            announced=[x for x in d.get("announced", []) if isinstance(x, str)][-MAX_ANNOUNCED:],
            grandma_since=d.get("grandma_since") if isinstance(d.get("grandma_since"), str) else None,
            grandma_imported=_str_list(d.get("grandma_imported")),
            grandma_skipped=_str_list(d.get("grandma_skipped")),
            grandma_failures={k: v for k, v in (d.get("grandma_failures") or {}).items()
                              if isinstance(k, str) and isinstance(v, int)}
            if isinstance(d.get("grandma_failures"), dict) else {},
            status=SyncStatus(**{k: v for k, v in st.items() if k in known}),
        )


def _str_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, str)][-MAX_GRANDMA_IDS:]


def state_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / STATE_FILE


def load_state(path: str | Path) -> SyncState:
    try:
        d = json.loads(Path(path).read_text("utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not an object")
        return SyncState.from_dict(d)
    except FileNotFoundError:
        return SyncState()
    except Exception as e:
        log.warning("sync: state file %s unreadable (%s); starting fresh", path, e)
        return SyncState()


def save_state(path: str | Path, state: SyncState) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_status(data_dir: str | Path) -> SyncStatus:
    """Last sync summary for the dashboard (read-only; safe from any process)."""
    return load_state(state_path(data_dir)).status
