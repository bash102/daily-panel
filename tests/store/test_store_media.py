import pytest

from zozobox.store import StoreError


def test_write_and_import_media(store, tmp_path):
    p = store.write_media("a/b/c.bin", b"123")
    assert p.read_bytes() == b"123"
    assert not list(p.parent.glob(".*.tmp"))
    src = tmp_path / "src.opus"
    src.write_bytes(b"opus")
    rel = store.import_media(src, "rec/x.opus")
    assert rel == "rec/x.opus" and store.media_path(rel).read_bytes() == b"opus"
    assert src.exists()
    store.import_media(src, "rec/y.opus", move=True)
    assert not src.exists()
    assert store.rel_media(store.media_path("rec/y.opus")) == "rec/y.opus"


def test_media_path_cannot_escape(store, tmp_path):
    with pytest.raises(StoreError):
        store.media_path("../zozobox.db")
    with pytest.raises(StoreError):
        store.rel_media(tmp_path / "elsewhere.txt")


def test_disk_usage(store):
    store.write_media("x.bin", b"0" * 1000)
    u = store.disk_usage()
    assert u["media_bytes"] == 1000 and u["db_bytes"] > 0 and u["free_bytes"] > 0
