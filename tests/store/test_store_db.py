import sqlite3
import threading

import pytest

from zozobox.store import SCHEMA_VERSION, Store, StoreError
from zozobox.store.db import Database, parse_ts, ts_str
from zozobox.store.content import Kind


def test_creates_db_and_media_dir(tmp_path, clock):
    s = Store(tmp_path / "d", clock=clock)
    assert (tmp_path / "d" / "zozobox.db").exists()
    assert (tmp_path / "d" / "media").is_dir()
    assert s.db.version == SCHEMA_VERSION
    assert s.db.query_one("PRAGMA journal_mode")[0] == "wal"
    s.close()


def test_reopen_keeps_data_and_does_not_remigrate(tmp_path, clock):
    with Store(tmp_path, clock=clock) as s:
        s.settings.volume = 0.25
        s.events.append("press", {"button": "red"})
    with Store(tmp_path, clock=clock) as s:
        assert s.settings.volume == 0.25
        assert s.events.count("press") == 1
        assert s.db.version == SCHEMA_VERSION


def test_forward_migration_from_older_version(tmp_path):
    steps = [["CREATE TABLE a (x INTEGER)"]]
    db = Database(tmp_path / "m.db", migrations=steps)
    db.conn.execute("INSERT INTO a VALUES (1)")
    db.close()
    steps2 = steps + [["ALTER TABLE a ADD COLUMN y TEXT DEFAULT 'z'"], ["CREATE TABLE b (q)"]]
    db = Database(tmp_path / "m.db", migrations=steps2)
    assert db.version == 3
    assert tuple(db.query_one("SELECT x, y FROM a")) == (1, "z")
    db.close()


def test_failed_migration_rolls_back(tmp_path):
    steps = [["CREATE TABLE a (x)"], ["CREATE TABLE b (x)", "THIS IS NOT SQL"]]
    with pytest.raises(sqlite3.Error):
        Database(tmp_path / "m.db", migrations=steps)
    db = Database(tmp_path / "m.db", migrations=steps[:1])
    assert db.version == 1
    assert db.query_one("SELECT name FROM sqlite_master WHERE name='b'") is None


def test_newer_schema_refused(tmp_path):
    db = Database(tmp_path / "m.db", migrations=[["CREATE TABLE a (x)"], ["CREATE TABLE b (x)"]])
    db.close()
    with pytest.raises(StoreError):
        Database(tmp_path / "m.db", migrations=[["CREATE TABLE a (x)"]])


def test_tx_rolls_back_on_error(store):
    with pytest.raises(RuntimeError):
        with store.db.tx() as c:
            c.execute("INSERT INTO daily(day, data) VALUES ('2020-01-01', '{}')")
            raise RuntimeError
    assert store.db.query_one("SELECT COUNT(*) FROM daily")[0] == 0


def test_ts_roundtrip_and_sorting(clock):
    a = clock()
    b = clock.advance(microseconds=1)
    assert parse_ts(ts_str(a)) == a
    assert ts_str(a) < ts_str(b)


def test_concurrent_writers_from_threads(store):
    errors = []

    def worker(n):
        try:
            for i in range(50):
                store.events.append("press", {"n": n, "i": i})
                store.content.add(Kind.STORY, meta={"n": n})
                store.daily.mark_printed("letter")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert store.events.count("press") == 200
    assert len(store.content.list(limit=1000)) == 200


def test_float_clock_is_accepted(tmp_path):
    with Store(tmp_path, clock=lambda: 1_700_000_000.0) as s:
        assert s.now().year == 2023
        assert s.now().tzinfo is not None
