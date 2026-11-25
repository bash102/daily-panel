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
    assert main(["once"]) == 3
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

    assert main(["once"], make_store=broken) == 3
    assert "cannot open store" in capsys.readouterr().err


def test_default_normalizer_without_ffmpeg(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert default_normalizer() is None
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    assert callable(default_normalizer())


@pytest.mark.parametrize("url,token", [
    ("replace-me", TOKEN),
    ("https://zozobox.example.workers.dev", TOKEN),
    ("https://<worker host>", TOKEN),
    ("http://cloud.test", TOKEN),            # never send the token in clear text
    ("ftp://cloud.test", TOKEN),
    ("https://", TOKEN),
    ("cloud.test", TOKEN),
    ("https://user:pw@cloud.test", TOKEN),
    ("https://cloud.test", "replace-me"),
])
def test_placeholder_or_unsafe_cloud_settings_exit_config_status(monkeypatch, tmp_path, capsys, url, token):
    monkeypatch.setenv("ZOZOBOX_CLOUD_URL", url)
    monkeypatch.setenv("ZOZOBOX_BOX_TOKEN", token)
    called = []
    rc = main(["once", "--data-dir", str(tmp_path)],
              make_store=lambda d: called.append(d), make_client=lambda *a: called.append(a))
    assert rc == 3 and called == []
    err = capsys.readouterr().err
    assert "ZOZOBOX_" in err and TOKEN not in err


def test_check_cloud_config_accepts_https_and_local_http():
    from zozobox.sync.__main__ import check_cloud_config

    assert check_cloud_config("https://zozobox.someone.workers.dev", TOKEN) is None
    assert check_cloud_config("http://127.0.0.1:8787", TOKEN) is None
    assert check_cloud_config("http://localhost:8787/", TOKEN) is None


def test_an_unexpected_crash_is_not_the_config_status(env):
    """An uncaught exception exits 1 (Python's default), which the supervisor
    restarts with backoff; only EXIT_CONFIG (3) means 'fix the config'."""
    from zozobox.sync.__main__ import EXIT_CONFIG

    class Boom(Exception):
        pass

    def client(*a):
        class C:
            def run(self, *a, **k):
                raise Boom("bug")
        return C()

    with pytest.raises(Boom):
        main(["run"], make_client=client, stop=threading.Event())
    assert EXIT_CONFIG not in (0, 1, 2)
