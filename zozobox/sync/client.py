"""Pi <-> cloud sync (PLAN M8, protocol in docs/SYNC.md).

    store = Store(data_dir, recover_outbox=True)   # only this process recovers
    client = SyncClient(store, os.environ["ZOZOBOX_CLOUD_URL"], os.environ["ZOZOBOX_BOX_TOKEN"])
    client.run_once()          # one pass
    client.run(stop_event)     # loop with offline backoff

One pass:
1. config: when quiet hours / timezone / review delay differ from what was
   last queued, queue control/config.json in the outbox.
2. announce: each new live Zozo reply gets a control/zozo/<rec_id>.json
   {action: "new"} command queued in the outbox (after its audio, which the
   engine queued when it saved the take).
3. upload: first every pending withhold command (control/zozo/<id>-withhold.json),
   ignoring its backoff, so a withhold is never stuck behind other uploads;
   then drain the rest of the outbox. Files go up as raw bytes, JSON records
   as JSON, both with PUT /api/objects/<remote_key>. A "new" command whose
   reply was withheld (or purged) since it was queued is dropped unsent.
   Items claimed but not tried because the box went offline (or the token
   was refused) go back to the queue without a backoff penalty.
4. grandma: fetch new grandma messages, download, loudness-normalize, and put
   them in the store's grandma slot (live; the previous one is superseded).
   Ids already imported are remembered (state + the store's event log), so a
   message the parent purged never comes back. A message that keeps failing
   for a reason retrying cannot fix (4xx, bad key, empty body) is skipped
   after GRANDMA_MAX_TRIES passes, so it cannot block newer ones.
5. status: mark Zozo replies the cloud has published.

Nothing here raises out of run_once(): errors are logged and reported, and
the next pass tries again.
"""

from __future__ import annotations

import logging
import mimetypes
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..store import Slot, Store
from ..store.content import check_rel_path
from ..store.db import ts_str
from ..store.outbox import INFLIGHT, PENDING
from ..store.recordings import RecStatus
from .api import CloudApi, CloudError, GrandmaMessage, iso_utc
from .state import SyncState, load_state, save_state, state_path
from .transport import Transport

log = logging.getLogger(__name__)

CONFIG_KEY = "control/config.json"
GRANDMA_PREFIX = "recordings/grandma/"
ZOZO_PREFIX = "recordings/zozo/"
WITHHOLD_LIKE = "control/zozo/%-withhold.json"
GRANDMA_MAX_TRIES = 3  # passes a permanently failing grandma message is retried
MAX_BACKOFF_EXP = 30

# normalizer(src, dst): write a loudness-normalized WAV of src to dst; raise on failure.
Normalizer = Callable[[Path, Path], Any]


def zozo_new_key(rec_id: str) -> str:
    return f"control/zozo/{rec_id}.json"


def zozo_withhold_key(rec_id: str) -> str:
    """The key M9's dashboard queues (with zozo_withhold_record) to withhold a reply."""
    return f"control/zozo/{rec_id}-withhold.json"


def zozo_withhold_record(rec_id: str) -> dict[str, Any]:
    return {"action": "withhold", "rec_id": rec_id}


def enqueue_withhold(store: Store, rec_id: str) -> None:
    """Helper for the dashboard: queue the withhold command (it must also call
    store.recordings.withhold(rec_id) itself)."""
    store.outbox.enqueue(zozo_withhold_key(rec_id), record=zozo_withhold_record(rec_id),
                         content_type="application/json")


def config_record(store: Store) -> dict[str, Any]:
    s = store.settings.as_dict()
    return {
        "quiet_hours": s["quiet_hours"],
        "timezone": s["timezone"],
        "review_delay_s": int(s["review_delay_s"]),
    }


def default_normalizer() -> Normalizer | None:
    """ffmpeg two-pass loudnorm to -16 LUFS (SPEC 5), or None without ffmpeg."""
    if shutil.which("ffmpeg") is None:
        return None
    from ..audio.loudness import normalize

    return lambda src, dst: normalize(src, dst)


class GrandmaImportError(CloudError):
    """A grandma message that retrying cannot fix (bad key, empty body)."""

    def __init__(self, message: str):
        super().__init__(message, status=422)


def _permanent(e: CloudError) -> bool:
    return isinstance(e, GrandmaImportError) or (not e.retryable and not e.auth)


def _is_new_command(key: str) -> bool:
    return key.startswith("control/zozo/") and key.endswith(".json") \
        and not key.endswith("-withhold.json")


@dataclass
class SyncReport:
    online: bool = True
    config_queued: bool = False
    announced: list[str] = field(default_factory=list)
    uploaded: int = 0
    failed: int = 0
    skipped_missing: int = 0
    released: int = 0
    dropped_withheld: list[str] = field(default_factory=list)
    imported: list[str] = field(default_factory=list)
    grandma_skipped: list[str] = field(default_factory=list)
    published: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.online and not self.errors


_REC_ID_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _valid_rec_id(rec_id: str) -> bool:
    return 0 < len(rec_id) <= 100 and set(rec_id) <= _REC_ID_OK


class SyncClient:
    def __init__(
        self,
        store: Store,
        base_url: str,
        token: str,
        clock: Callable[[], datetime] | None = None,
        *,
        transport: Transport | None = None,
        normalizer: Normalizer | None | str = "auto",
        batch_size: int = 10,
        max_uploads_per_run: int = 200,
        grandma_page: int = 50,
        state_file: str | Path | None = None,
        timeout_s: float = 30.0,
    ):
        self.store = store
        self.api = CloudApi(base_url, token, transport, timeout_s=timeout_s)
        self.clock = clock or store.now
        self.normalizer: Normalizer | None = (
            default_normalizer() if normalizer == "auto" else normalizer  # type: ignore[assignment]
        )
        self.batch_size = batch_size
        self.max_uploads_per_run = max_uploads_per_run
        self.grandma_page = grandma_page
        self.state_file = Path(state_file) if state_file else state_path(store.data_dir)
        self.state: SyncState = load_state(self.state_file)
        self._lock = threading.Lock()
        self._event_ids: set[str] | None = None  # grandma ids seen in the event log

    def __repr__(self) -> str:
        return f"SyncClient({self.api.base_url!r})"

    # -- one pass ---------------------------------------------------------------
    def run_once(self) -> SyncReport:
        with self._lock:
            rep = SyncReport()
            for name, step in (
                ("config", self._push_config),
                ("announce", self._announce_zozo),
                ("upload", self._drain_outbox),
                ("grandma", self._import_grandma),
                ("status", self._zozo_status),
            ):
                if not rep.online and name in ("grandma", "status"):
                    break  # no point trying more network calls this pass
                try:
                    step(rep)
                except CloudError as e:
                    if e.offline:
                        rep.online = False
                    rep.errors.append(f"{name}: {e}")
                    log.warning("sync: %s: %s", name, e)
                except Exception as e:  # never crash the sync process
                    rep.errors.append(f"{name}: {e!r}")
                    log.exception("sync: %s step failed", name)
            self._record_status(rep)
            return rep

    def _save(self) -> None:
        try:
            save_state(self.state_file, self.state)
        except Exception:
            log.exception("sync: could not save %s", self.state_file)

    def _record_status(self, rep: SyncReport) -> None:
        st = self.state.status
        now = iso_utc(self.clock())
        st.last_run_at = now
        st.online = rep.online
        st.uploaded_total += rep.uploaded
        st.imported_total += len(rep.imported)
        if rep.ok:
            st.last_ok_at = now
            st.last_error = None
        else:
            st.last_error = "; ".join(rep.errors)[:500] or None
        self._save()

    # -- 1. config --------------------------------------------------------------
    def _push_config(self, rep: SyncReport) -> None:
        cfg = config_record(self.store)
        if cfg == self.state.last_config:
            return
        self.store.outbox.enqueue(CONFIG_KEY, record=cfg, content_type="application/json")
        self.state.last_config = cfg
        self._save()
        rep.config_queued = True
        log.info("sync: queued config %s", cfg)

    # -- 2. announce Zozo replies -------------------------------------------------
    def _announce_zozo(self, rep: SyncReport) -> None:
        changed = False
        for rec in reversed(self.store.recordings.history(Slot.ZOZO, limit=20)):
            if self.state.is_announced(rec.id):
                continue
            if rec.status is not RecStatus.LIVE:
                # Withheld (the dashboard queues its own withhold) or already
                # replaced by a newer take: never announce it.
                self.state.add_announced(rec.id)
                changed = True
                continue
            if not _valid_rec_id(rec.id) or not rec.media.startswith(ZOZO_PREFIX):
                log.warning("sync: cannot announce recording %r (%s)", rec.id, rec.media)
                self.state.add_announced(rec.id)
                changed = True
                continue
            record = {
                "action": "new",
                "rec_id": rec.id,
                "media_key": rec.media,
                "recorded_at": iso_utc(rec.created_at),
                "duration_s": rec.duration_s,
            }
            self.store.outbox.enqueue(zozo_new_key(rec.id), record=record,
                                      content_type="application/json")
            self.state.add_announced(rec.id)
            changed = True
            rep.announced.append(rec.id)
            log.info("sync: queued Zozo reply %s for review/publish", rec.id)
        if changed:
            self._save()

    # -- 3. outbox ----------------------------------------------------------------
    def _drain_outbox(self, rep: SyncReport) -> None:
        self._send_withholds(rep)
        if not rep.online:
            return
        handled = 0
        while handled < self.max_uploads_per_run:
            batch = self.store.outbox.claim(min(self.batch_size, self.max_uploads_per_run - handled))
            if not batch:
                return
            for n, item in enumerate(batch):
                handled += 1
                if not self._upload(item, rep):
                    self._release_all(batch[n + 1:], rep)
                    return

    def _send_withholds(self, rep: SyncReport) -> None:
        """Withholds go first and ignore their backoff: the cloud must learn
        about them before it can publish (SPEC 2.2.4)."""
        try:
            rows = self.store.db.query(
                "SELECT id FROM outbox WHERE status=? AND remote_key LIKE ? ORDER BY id",
                (PENDING, WITHHOLD_LIKE),
            )
        except Exception:
            log.exception("sync: could not list withhold commands")
            return
        for r in rows:
            if not self._lease(r["id"]):
                continue
            try:
                item = self.store.outbox.get(r["id"])
            except Exception:
                log.exception("sync: could not read outbox item %s", r["id"])
                continue
            if not self._upload(item, rep):
                return

    def _lease(self, item_id: int) -> bool:
        try:
            with self.store.db.tx() as c:
                return c.execute(
                    "UPDATE outbox SET status=?, claimed_at=? WHERE id=? AND status=?",
                    (INFLIGHT, ts_str(self.clock()), item_id, PENDING),
                ).rowcount == 1
        except Exception:
            log.exception("sync: could not lease outbox item %s", item_id)
            return False

    def _release_all(self, items, rep: SyncReport) -> None:
        """Back to the queue, due now, attempts unchanged: they were never tried."""
        for it in items:
            try:
                with self.store.db.tx() as c:
                    c.execute(
                        "UPDATE outbox SET status=?, claimed_at=NULL WHERE id=? AND status=?",
                        (PENDING, it.id, INFLIGHT),
                    )
                rep.released += 1
            except Exception:
                log.exception("sync: could not release outbox item %s", it.id)

    def _new_command_is_stale(self, item) -> bool:
        """A "new" command for a reply the parent withheld (or purged) since
        it was queued must never reach the cloud."""
        rec_id = item.remote_key[len("control/zozo/"):-len(".json")]
        rec = self.store.recordings.find(rec_id)
        return rec is None or rec.status is RecStatus.WITHHELD

    def _upload(self, item, rep: SyncReport) -> bool:
        """Try one claimed item. False means stop this pass (offline or auth)."""
        try:
            if _is_new_command(item.remote_key) and self._new_command_is_stale(item):
                log.info("sync: %s: reply withheld or purged; not announcing it", item.remote_key)
                self.store.outbox.mark_done(item.id)
                rep.dropped_withheld.append(item.remote_key)
                return True
            if item.is_file:
                path = item.local_path
                assert path is not None
                try:
                    data = path.read_bytes()
                except FileNotFoundError:
                    # Purged or lost: it can never upload. Close it out loudly.
                    log.warning("sync: %s: local file %s is gone; dropping", item.remote_key, path)
                    self.store.outbox.mark_done(item.id)
                    rep.skipped_missing += 1
                    return True
                ctype = item.content_type or mimetypes.guess_type(path.name)[0] \
                    or "application/octet-stream"
                self.api.put_object(item.remote_key, data, ctype)
            else:
                self.api.put_json(item.remote_key, item.record)
            self.store.outbox.mark_done(item.id)
            rep.uploaded += 1
            return True
        except CloudError as e:
            self._fail(item.id, str(e))
            rep.failed += 1
            if e.offline:
                rep.online = False
                rep.errors.append(f"upload: {e}")
                return False
            if e.auth:
                rep.errors.append(f"upload: {e}")
                return False
            log.warning("sync: upload of %s failed: %s", item.remote_key, e)
            if not e.retryable:
                rep.errors.append(f"upload {item.remote_key}: {e}")
            return True
        except Exception as e:
            log.exception("sync: upload of %s failed", item.remote_key)
            self._fail(item.id, repr(e))
            rep.failed += 1
            return True

    def _fail(self, item_id: int, err: str) -> None:
        try:
            self.store.outbox.mark_failed(item_id, err)
        except Exception:
            log.exception("sync: could not mark outbox item %s failed", item_id)

    # -- 4. grandma's messages ------------------------------------------------------
    def _import_grandma(self, rep: SyncReport) -> None:
        # `since` is inclusive, so the newest known message comes back each
        # time; remembered ids make the import idempotent, even after a purge.
        for _ in range(100):  # pages
            before = self.state.grandma_since
            msgs = self.api.grandma_messages(before, self.grandma_page)
            for m in msgs:
                if not self._already_imported(m.id):
                    try:
                        self._import_one(m)
                    except CloudError as e:
                        if not _permanent(e) or not self._give_up_on(m.id, e, rep):
                            raise  # stop here, keep the watermark; retry next pass
                    else:
                        self.state.add_grandma_imported(m.id)
                        self.state.grandma_failures.pop(m.id, None)
                        rep.imported.append(m.id)
                        self._save()
                if m.recorded_at_raw != self.state.grandma_since:
                    self.state.grandma_since = m.recorded_at_raw
                    self._save()
            if len(msgs) < self.grandma_page or self.state.grandma_since == before:
                return

    def _already_imported(self, rec_id: str) -> bool:
        if self.state.is_grandma_imported(rec_id) or rec_id in self.state.grandma_skipped:
            return True
        if self.store.recordings.find(rec_id) is not None:
            self.state.add_grandma_imported(rec_id)
            return True
        # Tombstones that survive both a purge and a lost sync_state.json:
        # every import logged a "recording" event with source "cloud".
        if self._event_ids is None:
            self._event_ids = set()
            try:
                for ev in self.store.events.query(types=["recording"], limit=None):
                    p = ev.payload if isinstance(ev.payload, dict) else {}
                    if p.get("source") == "cloud" and isinstance(p.get("rec_id"), str):
                        self._event_ids.add(p["rec_id"])
            except Exception:
                log.exception("sync: could not read the event log for imported ids")
        if rec_id in self._event_ids:
            self.state.add_grandma_imported(rec_id)
            return True
        return False

    def _give_up_on(self, rec_id: str, e: CloudError, rep: SyncReport) -> bool:
        """Count a permanent failure; True once the message is skipped for good."""
        n = self.state.grandma_failures.get(rec_id, 0) + 1
        if n < GRANDMA_MAX_TRIES:
            self.state.grandma_failures[rec_id] = n
            self._save()
            return False
        self.state.grandma_failures.pop(rec_id, None)
        self.state.add_grandma_skipped(rec_id)
        self._save()
        log.error("sync: skipping grandma message %s after %d tries: %s", rec_id, n, e)
        rep.grandma_skipped.append(rec_id)
        rep.errors.append(f"grandma: skipped {rec_id}: {e}")
        return True

    def _import_one(self, m: GrandmaMessage) -> None:
        if not m.key.startswith(GRANDMA_PREFIX):
            raise GrandmaImportError(f"grandma message {m.id}: unexpected key {m.key!r}")
        try:
            rel = check_rel_path(m.key)
        except Exception as e:
            raise GrandmaImportError(f"grandma message {m.id}: bad key {m.key!r}: {e}") from e  # media/recordings/grandma/YYYY/MM/DD/<id>.wav
        data = self.api.get_object(m.key)
        if not data:
            raise GrandmaImportError(f"grandma message {m.id}: empty download")
        data = self._normalized(data, m.id)
        self.store.write_media(rel, data)
        rec = self.store.recordings.put(
            Slot.GRANDMA, rel, duration_s=m.duration_s, created_at=m.recorded_at, rec_id=m.id,
            meta={"source": "cloud", "key": m.key},
        )
        try:
            self.store.events.append("recording", {
                "slot": Slot.GRANDMA.value, "saved": True, "rec_id": rec.id,
                "duration_s": m.duration_s, "source": "cloud", "live": rec.live,
            }, media=rel)
        except Exception:
            log.exception("sync: could not log grandma message event")
        log.info("sync: imported grandma message %s (%s)", m.id, "live" if rec.live else "older")

    def _normalized(self, data: bytes, rec_id: str) -> bytes:
        if self.normalizer is None:
            return data
        with tempfile.TemporaryDirectory(prefix="zozobox-sync-") as td:
            src, dst = Path(td) / "in.wav", Path(td) / "out.wav"
            src.write_bytes(data)
            try:
                self.normalizer(src, dst)
                out = dst.read_bytes()
                if out:
                    return out
                log.warning("sync: normalizing %s gave nothing; keeping the original", rec_id)
            except Exception as e:
                log.warning("sync: normalizing %s failed (%s); keeping the original", rec_id, e)
        return data

    # -- 5. Zozo publish status -------------------------------------------------------
    def _zozo_status(self, rep: SyncReport) -> None:
        for s in self.api.zozo_messages():
            if s.status != "published" or s.published_at is None:
                continue
            rec = self.store.recordings.find(s.rec_id)
            if rec is None or rec.slot is not Slot.ZOZO or rec.published_at is not None:
                continue
            self.store.recordings.mark_published(s.rec_id, at=s.published_at)
            rep.published.append(s.rec_id)

    # -- loop -----------------------------------------------------------------------
    def run(
        self,
        stop: threading.Event | None = None,
        *,
        interval_s: float = 30.0,
        max_backoff_s: float = 600.0,
        max_passes: int | None = None,
        wait: Callable[[float], Any] | None = None,
    ) -> None:
        """Loop until `stop` is set: a pass every `interval_s`, backing off
        (doubling up to `max_backoff_s`) while offline or failing."""
        stop = stop or threading.Event()
        wait = wait or stop.wait
        failures = 0
        passes = 0
        while not stop.is_set():
            try:
                rep = self.run_once()
                ok = rep.ok
            except Exception:  # pragma: no cover - run_once guards itself
                log.exception("sync: pass failed")
                ok = False
            failures = 0 if ok else failures + 1
            passes += 1
            if max_passes is not None and passes >= max_passes:
                return
            delay = interval_s if failures == 0 else \
                min(interval_s * (2 ** min(failures, MAX_BACKOFF_EXP)), max_backoff_s)
            wait(delay)


__all__ = [
    "GrandmaImportError", "SyncClient", "SyncReport", "config_record", "enqueue_withhold", "zozo_new_key",
    "zozo_withhold_key", "zozo_withhold_record", "default_normalizer",
]
