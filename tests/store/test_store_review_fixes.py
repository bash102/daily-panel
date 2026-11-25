"""Regression tests for review findings: transaction failure handling,
concurrent first-open migration, weather supersede/day rules, atomic print
logging, durable media writes and Store open cleanup."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date

import pytest

from zozobox.store import Store, StoreError
from zozobox.store import db as dbmod
from zozobox.store import store as storemod
from zozobox.store.content import Approval, Safety
from zozobox.store.db import Database

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class FailingConn:
    """Wraps a sqlite3.Connection; makes chosen statements fail."""

    def __init__(self, real: sqlite3.Connection):
        self._real = real
        self.fail_commit = 0
        self.auto_rollback_on_commit = False

    def execute(self, sql, *args):
        if sql.strip().upper() == "COMMIT" and self.fail_commit:
            self.fail_commit -= 1
            if self.auto_rollback_on_commit:
                self._real.execute("ROLLBACK")
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _count(path, table):
    c = sqlite3.connect(str(path))
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


# -- finding 1: failed COMMIT ------------------------------------------------
@pytest.mark.parametrize("auto_rollback", [False, True])
def test_failed_commit_rolls_back_and_later_writes_commit(store, auto_rollback):
    proxy = FailingConn(store.db.conn)
    proxy.fail_commit = 1
    proxy.auto_rollback_on_commit = auto_rollback
    store.db.conn = proxy
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        store.events.append("press", {"n": 1})
    assert not store.db.conn.in_transaction
    store.events.append("press", {"n": 2})
    dbfile = store.data_dir / storemod.DB_NAME
    assert _count(dbfile, "events") == 1  # visible to another connection
    # The write lock is not held: another Store can open and write.
    with Store(store.data_dir, clock=store.clock) as other:
        other.events.append("press", {"n": 3})
    assert _count(dbfile, "events") == 2


def test_stray_open_transaction_is_rolled_back_not_joined(store):
    store.db.conn.execute("BEGIN IMMEDIATE")  # e.g. left over by a crash path
    store.events.append("press")
    assert not store.db.conn.in_transaction
    assert _count(store.data_dir / storemod.DB_NAME, "events") == 1


def test_nested_tx_still_joins(store):
    with store.db.tx():
        store.events.append("press", {"n": 1})
        with store.db.tx():
            store.events.append("press", {"n": 2})
        assert store.db.conn.in_transaction
    assert store.events.count("press") == 2
    with pytest.raises(RuntimeError):
        with store.db.tx():
            store.events.append("press", {"n": 3})
            raise RuntimeError("boom")
    assert store.events.count("press") == 2


# -- finding 2: auto-rollback must not mask the original error ---------------
def test_original_error_survives_sqlite_auto_rollback(store):
    class Original(Exception):
        pass

    with pytest.raises(Original):
        with store.db.tx() as c:
            c.execute("INSERT INTO events(ts, day, type) VALUES ('x','y','z')")
            c.execute("ROLLBACK")  # what SQLite does itself on SQLITE_FULL / IOERR
            raise Original()
    assert not store.db.conn.in_transaction
    store.events.append("press")
    assert store.events.count("press") == 1


# -- finding 3: concurrent first-open migration ------------------------------
def test_second_opener_inside_migration_window(tmp_path):
    path = tmp_path / "z.db"
    opened = []

    class Racy(Database):
        def tx(self):
            if not opened:  # another process opens between version read and BEGIN
                opened.append(Database(path))
            return super().tx()

    db = Racy(path)
    try:
        assert db.version == dbmod.SCHEMA_VERSION
        assert opened[0].version == dbmod.SCHEMA_VERSION
    finally:
        db.close()
        opened[0].close()


def test_concurrent_first_open_threads(tmp_path):
    path = tmp_path / "z.db"
    errors, dbs = [], []
    barrier = threading.Barrier(4)

    def run():
        barrier.wait()
        try:
            dbs.append(Database(path))
        except Exception as e:  # pragma: no cover - the failure being guarded
            errors.append(e)

    ts = [threading.Thread(target=run) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    for d in dbs:
        d.close()
    assert errors == []
    assert len(dbs) == 4


def test_newer_schema_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "z.db"
    Database(path, migrations=[["CREATE TABLE t(x)"], ["CREATE TABLE u(x)"]]).close()
    closed = []
    orig = Database.close
    monkeypatch.setattr(Database, "close", lambda self: (closed.append(1), orig(self)))
    with pytest.raises(StoreError, match="schema v2"):
        Database(path, migrations=[["CREATE TABLE t(x)"]])
    assert closed == [1]


# -- finding 4: pending/failed weather refresh never displaces servable weather
def test_pending_weather_refresh_keeps_approved_item(store):
    assert store.settings.approval_required(store.box_day())
    d = date(2026, 3, 10)
    first = store.content.add("weather", for_date=d, safety=Safety.PASSED)
    store.content.approve(first.id)
    assert store.content.current_for_today("weather", d).id == first.id
    second = store.content.add("weather", for_date=d, safety=Safety.PASSED)
    assert second.approval is Approval.PENDING
    assert not store.content.get(first.id).superseded
    assert store.content.current_for_today("weather", d).id == first.id
    assert store.content.needs_generation("weather", d) is False
    # Once the refresh is approved it takes over.
    store.content.approve(second.id)
    assert store.content.get(first.id).superseded_by == second.id
    assert store.content.current_for_today("weather", d).id == second.id


def test_failed_weather_refresh_keeps_good_item(store):
    d = date(2026, 3, 10)
    good = store.content.add("weather", for_date=d, safety=Safety.PASSED, approval=Approval.AUTO)
    bad = store.content.add("weather", for_date=d, approval=Approval.AUTO)  # safety pending
    assert store.content.current_for_today("weather", d).id == good.id
    store.content.set_safety(bad.id, Safety.FAILED, "nope")
    assert not store.content.get(good.id).superseded
    assert store.content.current_for_today("weather", d).id == good.id


def test_servable_weather_refresh_supersedes_older(store):
    d = date(2026, 3, 10)
    a = store.content.add("weather", for_date=d, safety=Safety.PASSED, approval=Approval.AUTO)
    b = store.content.add("weather", for_date=d, safety=Safety.PASSED, approval=Approval.AUTO)
    assert store.content.get(a.id).superseded_by == b.id
    assert store.content.current_for_today("weather", d).id == b.id


def test_approving_older_weather_does_not_supersede_newer(store):
    d = date(2026, 3, 10)
    a = store.content.add("weather", for_date=d, safety=Safety.PASSED)
    b = store.content.add("weather", for_date=d, safety=Safety.PASSED)
    store.content.approve(b.id)
    assert store.content.get(a.id).superseded
    store.content.approve(a.id)  # a is superseded, stays so; b unaffected
    assert not store.content.get(b.id).superseded
    assert store.content.current_for_today("weather", d).id == b.id


# -- finding 5: weather added at night is for the coming morning --------------
def test_weather_added_before_day_start_is_for_the_morning(store, clock):
    clock.set_local(2026, 4, 20, 1, 0)
    assert store.box_day() == date(2026, 4, 19)
    assert store.weather_day() == date(2026, 4, 20)
    assert store.content.needs_generation("weather") is True
    w = store.content.add("weather", safety=Safety.PASSED, approval=Approval.AUTO)
    assert w.for_date == date(2026, 4, 20)
    assert store.content.needs_generation("weather") is False
    clock.set_local(2026, 4, 20, 7, 0)
    assert store.content.current_for_today("weather").id == w.id


def test_weather_added_during_the_day_is_for_today(store, clock):
    clock.set_local(2026, 4, 20, 7, 30)
    w = store.content.add("weather", safety=Safety.PASSED, approval=Approval.AUTO)
    assert w.for_date == date(2026, 4, 20)


# -- finding 6: print event and its upload commit together --------------------
def test_print_not_logged_when_enqueue_fails(store, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store.outbox, "enqueue", boom)
    with pytest.raises(sqlite3.OperationalError):
        store.events.record_print("letter", PNG)
    assert store.events.count("print") == 0
    assert list((store.media_dir).rglob("*.png")) == []  # no orphaned copy


def test_print_and_outbox_row_commit_together(store):
    ev = store.events.record_print("letter", PNG)
    assert store.outbox.stats().pending == 1
    assert store.outbox.pending()[0].remote_key == ev.media


# -- finding 7: durable, collision-free media writes --------------------------
def test_write_media_fsyncs_directory(store, monkeypatch):
    seen = []
    monkeypatch.setattr(storemod, "_fsync_dir", lambda p: seen.append(p))
    dest = store.write_media("a/b.png", PNG)
    assert seen == [dest.parent]
    src = store.data_dir / "src.bin"
    src.write_bytes(b"x")
    store.import_media(src, "a/c.bin")
    assert seen[-1] == dest.parent


def test_fsync_dir_real_directory(tmp_path):
    storemod._fsync_dir(tmp_path)  # must not raise
    storemod._fsync_dir(tmp_path / "missing")


def test_concurrent_writers_same_destination(store):
    errors = []
    barrier = threading.Barrier(8)

    def run(i):
        barrier.wait()
        try:
            for _ in range(20):
                store.write_media("same.png", PNG + bytes([i]))
        except Exception as e:
            errors.append(e)

    ts = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert [p.name for p in store.media_dir.iterdir()] == ["same.png"]  # no temp leftovers


def test_write_media_cleans_temp_on_failure(store, monkeypatch):
    def bad_replace(a, b):
        raise OSError("nope")

    monkeypatch.setattr(storemod.os, "replace", bad_replace)
    with pytest.raises(OSError):
        store.write_media("x.png", PNG)
    assert list(store.media_dir.iterdir()) == []


# -- finding 8: Store open cleanup and outbox lease stealing ------------------
def test_store_open_failure_closes_db(tmp_path, clock, monkeypatch):
    closed = []
    orig = Database.close
    monkeypatch.setattr(Database, "close", lambda self: (closed.append(1), orig(self)))

    def boom(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storemod.Settings, "init_defaults", boom)
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path, clock=clock)
    assert closed == [1]


def test_second_process_does_not_steal_leases(tmp_path, clock):
    with Store(tmp_path, clock=clock) as sync:
        sync.outbox.enqueue("a", record=1)
        [item] = sync.outbox.claim()
        with Store(tmp_path, clock=clock) as dashboard:
            assert dashboard.outbox.stats().inflight == 1
        assert sync.outbox.claim() == []  # nothing to double-upload
        sync.outbox.mark_done(item.id)


def test_recover_only_expired(store, clock):
    store.outbox.enqueue("a", record=1)
    store.outbox.claim()
    assert store.outbox.recover(only_expired=True) == 0
    clock.advance(seconds=store.outbox.lease_s + 1)
    assert store.outbox.recover(only_expired=True) == 1
    assert store.outbox.stats().inflight == 0

