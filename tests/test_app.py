import io
import threading
import types
from pathlib import Path

from zozobox import app as app_mod
from zozobox.app import App, Backends, OfflineClassifier, key_reader, main
from zozobox.audio import FakeAudioIn, FakeAudioOut
from zozobox.boxconfig import DEFAULT_SHUTDOWN, AppConfig
from zozobox.buttons import FakeButtonBackend, ParentChord, Press
from zozobox.config import Btn
from zozobox.engine import FakeMoodClassifier
from zozobox.leds import FakeLedBackend
from zozobox.printer import FakePrinter, PrinterError


def cfg_for(tmp_path, **app_kw) -> AppConfig:
    import dataclasses

    cfg = AppConfig(data_dir=tmp_path / "data")
    if app_kw:
        cfg = dataclasses.replace(cfg, app=dataclasses.replace(cfg.app, **app_kw))
    return cfg


def injected(cfg):
    out = FakeAudioOut(auto_finish=True)
    return Backends(buttons=FakeButtonBackend(cfg.box.buttons.pins), leds=FakeLedBackend(),
                    audio=(out, FakeAudioIn(muter=out)), printer=FakePrinter(),
                    classifier=FakeMoodClassifier())


class Runs:
    def __init__(self, code=0, stderr=""):
        self.calls = []
        self.code, self.stderr = code, stderr

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return types.SimpleNamespace(returncode=self.code, stderr=self.stderr)


def test_fake_build_and_close(tmp_path):
    a = App(cfg_for(tmp_path), fake=True, start_threads=False).build()
    assert a.health == {"leds": "fake", "buttons": "fake", "audio": "fake", "printer": "fake"}
    assert a.engine.settings.click == tmp_path / "data" / "chimes" / "click.wav"
    assert a.engine.settings.beep.is_file()
    assert (tmp_path / "data" / "soundboard").is_dir()
    led_backend = a.leds.backend
    a.close()
    a.close()  # idempotent
    assert led_backend.closed and set(led_backend.duty.values()) == {0}


def test_hardware_failures_degrade_and_printer_is_retried(tmp_path, monkeypatch):
    import zozobox.audio
    import zozobox.hw.pi as pi
    import zozobox.mood

    def boom(*a, **k):
        raise OSError("no such device")

    monkeypatch.setattr(pi, "GpioButtonBackend", boom)
    monkeypatch.setattr(pi, "Pca9685LedBackend", boom)
    monkeypatch.setattr(zozobox.audio, "create_alsa_audio", boom)
    monkeypatch.setattr(zozobox.mood.OpenAIClaudeMoodClassifier, "from_store",
                        classmethod(lambda cls, store: boom()))
    printer_up = {"ok": False}
    fp = FakePrinter()

    def factory_for(cfg):
        def make():
            if not printer_up["ok"]:
                raise PrinterError("no USB device 04f9:209b")
            return fp
        return make

    monkeypatch.setattr(app_mod, "brother_factory", factory_for)
    t = [0.0]
    a = App(cfg_for(tmp_path, printer_retry_s=60), clock=lambda: t[0], start_threads=False)
    a.build()
    try:
        for part in ("leds", "buttons", "audio", "mood_classifier", "printer"):
            assert a.health[part].startswith("failed"), part
        assert isinstance(a.audio_out, FakeAudioOut)
        assert isinstance(a.engine.mood_classifier, OfflineClassifier)
        assert a.engine.mood_classifier.classify(Path("x")).offline
        events = a.store.events.query("error", limit=None)
        comps = {e.payload["component"] for e in events if e.payload.get("kind") == "hw_init_failed"}
        assert comps == {"leds", "buttons", "audio", "mood_classifier", "printer"}
        # The engine still works without a printer, and the printer comes back.
        a.engine.handle(Press(Btn.YELLOW, t[0]))  # soundboard: no clips, no crash
        printer_up["ok"] = True
        a.maintain()
        assert not a.printer_supervisor.available  # not due yet
        t[0] = 61
        a.maintain()
        assert a.printer_supervisor.available and a.health["printer"] == "ok"
        from PIL import Image
        a.printer.print_image(Image.new("1", (696, 10), 1))
        assert fp.count == 1
        assert (tmp_path / "data" / ".printer.lock").exists()
    finally:
        a.close()


def test_parent_chord_chimes_blinks_and_powers_off(tmp_path):
    cfg = cfg_for(tmp_path)
    runs = Runs()
    b = injected(cfg)
    a = App(cfg, backends=b, start_threads=False, run_command=runs, sleep=lambda s: None).build()
    try:
        writes_before = b.leds.writes
        a.engine.handle(ParentChord(0.0))
        for t in a.chord_threads:
            t.join(5)
        assert runs.calls == [list(DEFAULT_SHUTDOWN)]
        beeps = [p for p in b.audio[0].plays if p.path.name == "beep.wav"]
        assert len(beeps) == 1  # never synced -> "offline" = one beep
        assert b.leds.writes - writes_before >= 8  # 4 blinks on 4 channels
        assert a.store.events.query("parent_shutdown", limit=None)
        assert a.store.events.query("parent_chord", limit=None)
    finally:
        a.close()


def test_parent_chord_online_two_beeps_and_failure_logged(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    runs = Runs(code=1, stderr="sudo: a password is required")
    b = injected(cfg)
    import zozobox.sync
    from datetime import datetime, timezone

    from zozobox.sync.api import iso_utc
    fresh = iso_utc(datetime.now(timezone.utc))
    monkeypatch.setattr(zozobox.sync, "load_status",
                        lambda d: types.SimpleNamespace(online=True, last_run_at=fresh))
    a = App(cfg, backends=b, start_threads=False, run_command=runs, sleep=lambda s: None).build()
    try:
        a.on_parent_chord(ParentChord(0.0))
        a.chord_threads[-1].join(5)
        assert len([p for p in b.audio[0].plays if p.path.name == "beep.wav"]) == 2
        errs = [e for e in a.store.events.query("error", limit=None)
                if e.payload.get("kind") == "shutdown_failed"]
        assert errs and "password" in errs[0].payload["error"]
        a.on_parent_chord(ParentChord(1.0))  # can try again after a failure
        a.chord_threads[-1].join(5)
        assert len(runs.calls) == 2
    finally:
        a.close()


def test_parent_chord_in_fake_mode_does_not_power_off(tmp_path):
    runs = Runs()
    a = App(cfg_for(tmp_path), fake=True, start_threads=False, run_command=runs,
            sleep=lambda s: None).build()
    try:
        a.on_parent_chord(ParentChord(0.0))
        a.chord_threads[-1].join(5)
        assert runs.calls == [] and a.shutdown_runs == [list(DEFAULT_SHUTDOWN)]
    finally:
        a.close()


def test_key_reader_presses_and_quits(tmp_path):
    a = App(cfg_for(tmp_path), fake=True, start_threads=False).build()
    try:
        stop = threading.Event()
        key_reader(a, stop, io.StringIO("g\nstory\nchord\nwhat\nq\n"))
        assert stop.is_set()
        evs = []
        while not a.buttons.events.empty():
            evs.append(a.buttons.events.get())
        assert any(isinstance(e, Press) and e.button is Btn.GREEN for e in evs)
        assert any(isinstance(e, ParentChord) for e in evs)
    finally:
        a.close()


def test_main_runs_until_stopped(tmp_path):
    stop = threading.Event()
    rc = []
    t = threading.Thread(target=lambda: rc.append(
        main(["--fake", "--no-keys", "--data-dir", str(tmp_path / "d")], stop=stop)))
    t.start()
    import time
    end = time.monotonic() + 10
    while not (tmp_path / "d" / "zozobox.db").exists() and time.monotonic() < end:
        time.sleep(0.02)
    stop.set()
    t.join(15)
    assert rc == [0]


def test_main_config_errors(tmp_path, capsys):
    bad = tmp_path / "box.toml"
    bad.write_text("[leds]\ninvrt = true\n")
    assert main(["--config", str(bad), "--check-config"]) == 2
    assert "invrt" in capsys.readouterr().err
    assert main(["--config", str(tmp_path / "missing.toml"), "--check-config"]) == 2


def test_check_config_prints(tmp_path, capsys):
    good = tmp_path / "box.toml"
    good.write_text('[audio.handset]\ncard = "POP"\n')
    assert main(["--config", str(good), "--check-config", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "card='POP'" in out and "systemctl poweroff" in out


def test_startup_failure_returns_1(tmp_path, capsys):
    f = tmp_path / "file"
    f.write_text("x")
    assert main(["--fake", "--no-keys", "--data-dir", str(f)]) == 1
    assert "startup failed" in capsys.readouterr().err


def test_buttons_and_leds_are_retried_until_the_hardware_comes_up(tmp_path, monkeypatch):
    """A GPIO chip / I2C bus that is not ready at boot must not leave the box
    deaf (no parent chord) until someone restarts the service."""
    import zozobox.audio
    import zozobox.hw.pi as pi

    up = {"ok": False}
    leds = FakeLedBackend()
    buttons = FakeButtonBackend(list(Btn))

    def gpio(cfg):
        if not up["ok"]:
            raise OSError("gpiochip0 busy")
        return buttons

    def pca(cfg):
        if not up["ok"]:
            raise OSError(121, "Remote I/O error")
        return leds

    monkeypatch.setattr(pi, "GpioButtonBackend", gpio)
    monkeypatch.setattr(pi, "Pca9685LedBackend", pca)
    monkeypatch.setattr(zozobox.audio, "create_alsa_audio", lambda cfg: app_mod._fake_audio())
    monkeypatch.setattr(app_mod, "brother_factory", lambda cfg: (lambda: FakePrinter()))
    t = [0.0]
    a = App(cfg_for(tmp_path, hw_retry_s=30), clock=lambda: t[0], start_threads=False,
            backends=Backends(classifier=FakeMoodClassifier()))
    a.build()
    try:
        assert a.health["buttons"].startswith("failed") and "(retrying)" in a.health["buttons"]
        assert a.health["leds"].startswith("failed")
        a.buttons.poll()
        assert a.buttons.events.empty()  # inert, but no crash
        up["ok"] = True
        t[0] = 10
        a.maintain()
        assert a.health["buttons"].startswith("failed")  # not due yet
        t[0] = 31
        a.maintain()
        assert a.health["buttons"] == "ok" and a.health["leds"] == "ok"
        assert a.retrying["buttons"].available and a.retrying["leds"].available
        # Edges now reach the arbiter through the real backend.
        buttons.set(Btn.YELLOW, True)
        t[0] = 31.2
        buttons.set(Btn.YELLOW, False)
        ev = a.buttons.events.get_nowait()
        assert isinstance(ev, Press) and ev.button is Btn.YELLOW
        # Every LED channel is written once the driver is there.
        a.leds.render()
        assert set(leds.duty) == set(a.cfg.box.leds.channels.values())
        rec = [e for e in a.store.events.query("hw_recovered", limit=None)]
        assert {e.payload["component"] for e in rec} == {"buttons", "leds"}
    finally:
        a.close()
    assert leds.closed


def test_network_chime_ignores_a_stale_sync_status(tmp_path):
    """online=True from a sync that stopped hours ago must not chime 'online'."""
    from datetime import datetime, timedelta, timezone

    from zozobox.sync.api import iso_utc
    from zozobox.sync.state import SyncState, save_state, state_path

    cfg = cfg_for(tmp_path)
    a = App(cfg, backends=injected(cfg), start_threads=False, sleep=lambda s: None).build()
    try:
        now = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
        assert not a.network_online(now)  # never synced
        st = SyncState()
        st.status.online = True
        st.status.last_run_at = iso_utc(now - timedelta(seconds=40))
        save_state(state_path(cfg.data_dir), st)
        assert a.network_online(now)
        assert not a.network_online(now + timedelta(hours=3))  # sync stopped long ago
        st.status.online = False
        save_state(state_path(cfg.data_dir), st)
        assert not a.network_online(now)
    finally:
        a.close()
