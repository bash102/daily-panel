import threading

import pytest

from zozobox.store import Store
from zozobox.sync import SyncClient
from zozobox.sync.__main__ import main
from zozobox.sync.client import default_normalizer
from zozobox.sync.fake import FakeCloud

TOKEN = "box-token-for-tests"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZOZOBOX_CLOUD_URL", "https://cloud.test")
    monkeypatch.setenv("ZOZOBOX_BOX_TOKEN", TOKEN)
    monkeypatch.setenv("ZOZOBOX_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_once_uploads_and_exits_zero(env, capsys):
    cloud = FakeCloud(token=TOKEN)
    opened = {}

    def make_store(d):
        opened["store"] = s = Store(d, recover_outbox=True)
        return s

    def make_client(store, url, token):
        assert url == "https://cloud.test" and token == TOKEN
        return SyncClient(store, url, token, transport=cloud, normalizer=None)

    assert main(["once"], make_store=make_store, make_client=make_client) == 0
    assert "config_queued=True" in capsys.readouterr().out
    assert "control/config.json" in cloud.objects


def test_once_offline_exits_two(env, capsys):
    cloud = FakeCloud(token=TOKEN, offline=True)
    rc = main(["once"], make_client=lambda s, u, t: SyncClient(s, u, t, transport=cloud, normalizer=None))
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_missing_env(monkeypatch, capsys):
    monkeypatch.delenv("ZOZOBOX_CLOUD_URL", raising=False)
    monkeypatch.setenv("ZOZOBOX_BOX_TOKEN", TOKEN)
    assert main(["once"]) == 1
    err = capsys.readouterr().err
    assert "ZOZOBOX_CLOUD_URL" in err and TOKEN not in err


def test_run_stops_on_event(env):
    cloud = FakeCloud(token=TOKEN)
    stop = threading.Event()
    passes = []

    class Client(SyncClient):
        def run_once(self):
            passes.append(1)
            stop.set()
            return super().run_once()

    rc = main(["run", "--interval", "0.01"], stop=stop,
              make_client=lambda s, u, t: Client(s, u, t, transport=cloud, normalizer=None))
    assert rc == 0 and passes == [1]


def test_bad_store(env, capsys):
    def broken(d):
        raise OSError("disk gone")

    assert main(["once"], make_store=broken) == 1
    assert "cannot open store" in capsys.readouterr().err


def test_default_normalizer_without_ffmpeg(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert default_normalizer() is None
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    assert callable(default_normalizer())
