"""The box's local store (PLAN M4): one SQLite DB plus a media directory.

    store = Store(data_dir)            # <data_dir>/zozobox.db, <data_dir>/media/
    store.settings / .content / .daily / .events / .outbox / .recordings

`clock` returns the current time as an aware datetime (naive = UTC; a float
is taken as a Unix timestamp). Everything time-dependent goes through it, so
tests can move the box across days and quiet hours.

Put data_dir on persistent storage: the root filesystem is an overlay (SPEC 1).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from .content import ContentItem, ContentRepo, check_rel_path
from .daily import DailyState
from .db import Database, StoreError, to_utc
from .events import Event, EventLog
from .outbox import Outbox
from .recordings import RecordingSlots
from .settings import Settings

log = logging.getLogger(__name__)

DB_NAME = "zozobox.db"
MEDIA_DIR = "media"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _wrap_clock(clock: Callable[[], datetime | float]) -> Callable[[], datetime]:
    def now() -> datetime:
        t = clock()
        if isinstance(t, (int, float)):
            return datetime.fromtimestamp(t, timezone.utc)
        return to_utc(t)

    return now


def _unlink_quietly(path: str | Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename into it is durable (no-op where the OS
    cannot open directories, e.g. Windows)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        log.warning("store: could not fsync directory %s", path)
    finally:
        os.close(fd)


class Store:
    def __init__(
        self,
        data_dir: str | Path,
        clock: Callable[[], datetime | float] = _utc_now,
        *,
        upload_prints: bool = True,
        outbox_backoff_base_s: float = 30.0,
        outbox_backoff_max_s: float = 3600.0,
        recover_outbox: bool = False,
    ):
        """recover_outbox=True returns every in-flight upload claim to the queue
        at open. Only the process that runs the sync client should pass it
        (once, at startup): other processes (dashboard, engine) opening the
        store must not steal its live leases. Claims a dead process left
        behind are picked up again anyway once their lease expires."""
        self.data_dir = Path(data_dir)
        self.media_dir = self.data_dir / MEDIA_DIR
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.clock = _wrap_clock(clock)
        self.upload_prints = upload_prints
        self.db = Database(self.data_dir / DB_NAME)
        try:
            self._open(recover_outbox, outbox_backoff_base_s, outbox_backoff_max_s)
        except BaseException:
            self.db.close()
            raise

    def _open(self, recover_outbox: bool, backoff_base_s: float, backoff_max_s: float) -> None:
        self.settings = Settings(self.db, self.clock)
        self.settings.init_defaults()
        self.outbox = Outbox(
            self.db,
            self.clock,
            backoff_base_s=backoff_base_s,
            backoff_max_s=backoff_max_s,
        )
        if recover_outbox:
            recovered = self.outbox.recover()
            if recovered:
                log.info("store: returned %d interrupted uploads to the queue", recovered)
        self.content = ContentRepo(
            self.db,
            self.clock,
            self.box_day,
            self.settings.approval_required,
            on_purge=self._purge_content_media,
            local=self.settings.local,
        )
        self.daily = DailyState(self.db, self.box_day)
        self.events = EventLog(
            self.db,
            self.clock,
            self.box_day,
            self.settings.local,
            self.write_media,
            self.media_exists,
            on_print=self._print_logged,
            remove_media=self._remove_media,
        )
        self.recordings = RecordingSlots(
            self.db, self.clock, on_purge=lambda r: self._remove_media(r.media)
        )

    # -- time -----------------------------------------------------------------
    def now(self) -> datetime:
        return self.clock()

    def box_day(self, dt: datetime | None = None) -> date:
        return self.settings.box_day(dt or self.clock())

    def weather_day(self, dt: datetime | None = None) -> date:
        """The date weather fetched at `dt` is for (see ContentRepo.weather_day)."""
        return self.content.weather_day(dt or self.clock())

    def is_quiet(self, dt: datetime | None = None) -> bool:
        return self.settings.is_quiet(dt or self.clock())

    # -- media ----------------------------------------------------------------
    def media_path(self, rel: str) -> Path:
        """Absolute path for a media-relative path (validated not to escape)."""
        return self.media_dir / check_rel_path(rel)

    def media_exists(self, rel: str) -> bool:
        return self.media_path(rel).exists()

    def rel_media(self, path: str | Path) -> str:
        """Media-relative form of an absolute path inside the media dir."""
        try:
            return Path(path).resolve().relative_to(self.media_dir.resolve()).as_posix()
        except ValueError:
            raise StoreError(f"{path} is not inside {self.media_dir}") from None

    def write_media(self, rel: str, data: bytes) -> Path:
        """Durably write bytes to media/<rel>: unique temp file, fsync, rename,
        then fsync the directory so the rename survives a power cut before
        the DB row that points at it is committed."""
        dest = self.media_path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except BaseException:
            _unlink_quietly(tmp)
            raise
        _fsync_dir(dest.parent)
        return dest

    def import_media(self, src: str | Path, rel: str, *, move: bool = False) -> str:
        """Copy (or move) a file into media/<rel>; returns the relative path."""
        dest = self.media_path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(src), dest)
        else:
            fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, dest)
            except BaseException:
                _unlink_quietly(tmp)
                raise
        _fsync_dir(dest.parent)
        return check_rel_path(rel)

    def disk_usage(self) -> dict[str, int]:
        """Bytes used by the media dir and DB files, plus free space (dashboard)."""
        media = sum(p.stat().st_size for p in self.media_dir.rglob("*") if p.is_file())
        dbb = sum(
            p.stat().st_size for p in self.data_dir.glob(DB_NAME + "*") if p.is_file()
        )
        free = shutil.disk_usage(self.data_dir).free
        return {"media_bytes": media, "db_bytes": dbb, "free_bytes": free}

    def _remove_media(self, rel: str) -> None:
        try:
            self.media_path(rel).unlink(missing_ok=True)
        except (OSError, StoreError):
            log.exception("store: could not remove media %s", rel)

    def _purge_content_media(self, item: ContentItem) -> None:
        for rel in item.media.values():
            self._remove_media(rel)

    def _print_logged(self, ev: Event, rel: str) -> None:
        if self.upload_prints:
            self.outbox.enqueue(rel, path=self.media_path(rel), content_type="image/png")

    # -- lifecycle ------------------------------------------------------------
    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["Store", "StoreError"]
