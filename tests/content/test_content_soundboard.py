import json

from zozobox.content import Clients, FakeClaude, FakeMedia, FakeWeather, Pipeline, run_bootstrap
from zozobox.content import __main__ as cli
from zozobox.content.soundboard import STARTER_CLIPS, bootstrap_soundboard
from zozobox.engine import StoreSoundboard
from zozobox.store import Kind, Store


def test_starter_pack_texts_are_short_and_gentle():
    assert 12 <= len(STARTER_CLIPS) <= 20
    banned = ("scary", "monster", "blood", "kill", "die", "hurt", "cry", "afraid")
    for slug, text in STARTER_CLIPS.items():
        assert slug.replace("_", "").isalnum()
        assert len(text.split()) <= 20, slug
        assert not any(b in text.lower().split() for b in banned), slug


def items(store):
    return store.content.list(Kind.SOUNDBOARD, include_withheld=False,
                              include_superseded=False, limit=1000)


def test_bootstrap_soundboard_generates_once_as_gated_items(store, clients, clock, media):
    p = Pipeline(store, clients, clock)
    out = bootstrap_soundboard(p)
    assert set(out) == set(STARTER_CLIPS) and set(out.values()) == {"generated"}
    its = items(store)
    assert {it.meta["key"] for it in its} == set(STARTER_CLIPS)
    for it in its:
        assert it.safety.value == "passed" and it.approval.value == "pending"  # first month
        assert it.media["audio"].startswith("soundboard/")
        assert store.media_exists(it.media["audio"])
    assert media.tts_texts == list(STARTER_CLIPS.values())
    spend = store.events.query("spend", limit=None)
    assert any(e.payload.get("purpose") == "soundboard_tts" for e in spend)
    # They are in the parent's approval queue, and nothing plays before approval.
    assert len([i for i in store.content.pending_approval() if i.kind is Kind.SOUNDBOARD]) \
        == len(STARTER_CLIPS)
    assert StoreSoundboard(store).clips() == []
    again = bootstrap_soundboard(p)
    assert set(again.values()) == {"skipped"} and len(media.tts_texts) == len(STARTER_CLIPS)


def test_only_approved_starter_clips_play(store, clients, clock, tmp_path):
    bootstrap_soundboard(Pipeline(store, clients, clock))
    cow = next(i for i in items(store) if i.meta["key"] == "cow")
    duck = next(i for i in items(store) if i.meta["key"] == "duck")
    store.content.approve(cow.id)
    store.content.reject(duck.id)
    sb_dir = tmp_path / "sb"
    (sb_dir / "imported").mkdir(parents=True)
    (sb_dir / "imported" / "moo.wav").write_bytes(b"RIFF")  # owner-imported: no gate
    (sb_dir / "starter").mkdir()
    (sb_dir / "starter" / "old.wav").write_bytes(b"RIFF")   # legacy ungated TTS: ignored
    clips = StoreSoundboard(store, sb_dir).clips()
    assert clips == sorted([store.media_path(cow.media["audio"]), sb_dir / "imported" / "moo.wav"])
    store.content.withhold(cow.id)  # a parent deletes it later
    assert StoreSoundboard(store, sb_dir).clips() == [sb_dir / "imported" / "moo.wav"]


def test_force_keeps_the_approved_take_until_the_new_one_is_approved(store, clients, clock):
    p = Pipeline(store, clients, clock)
    bootstrap_soundboard(p)
    for it in items(store):
        store.content.approve(it.id)
    before = {it.meta["key"]: it.id for it in items(store)}
    assert set(bootstrap_soundboard(p, force=True).values()) == {"generated"}
    played = StoreSoundboard(store).clips()
    assert len(played) == len(STARTER_CLIPS)  # the approved takes still play
    assert {store.content.get(i).superseded for i in before.values()} == {False}
    # A second forced run replaces the still-pending takes outright.
    bootstrap_soundboard(p, force=True)
    pending = [i for i in items(store) if i.approval.value == "pending"]
    assert len(pending) == len(STARTER_CLIPS)


def test_bootstrap_soundboard_failures_are_per_clip(store, clock, normalizer, tmp_path):
    media = FakeMedia(fail_tts=lambda t: t.startswith("Moo"))
    clients = Clients(FakeClaude(), media, FakeWeather(), normalizer)
    out = bootstrap_soundboard(Pipeline(store, clients, clock))
    assert out["cow"] == "failed" and out["duck"] == "generated"
    assert "cow" not in {i.meta["key"] for i in items(store)}
    assert not list((store.media_dir / "soundboard").glob("cow-*"))  # no partial file


def test_run_bootstrap_includes_soundboard_only_when_asked(store, clients, clock):
    out = run_bootstrap(store, clients, clock)
    assert "soundboard" not in out
    out = run_bootstrap(store, clients, clock, soundboard=True)
    assert set(out["soundboard"].values()) == {"generated"}


def test_cli_bootstrap_writes_soundboard_items(tmp_path, monkeypatch, normalizer, capsys):
    data = tmp_path / "data"
    Store(data).close()
    fakes = Clients(FakeClaude(), FakeMedia(), FakeWeather(), normalizer)
    monkeypatch.setattr("zozobox.content.pipeline.Clients.from_env",
                        classmethod(lambda cls, config=None: fakes))
    assert cli.main(["bootstrap", "--data-dir", str(data)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["soundboard"].values()) == {"generated"}
    s = Store(data)
    try:
        assert {i.meta["key"] for i in items(s)} == set(STARTER_CLIPS)
        assert not (data / "soundboard" / "starter").exists()
    finally:
        s.close()
    assert cli.main(["bootstrap", "--data-dir", str(data), "--no-soundboard"]) == 0
    assert "soundboard" not in json.loads(capsys.readouterr().out)
    assert cli.main(["status", "--data-dir", str(data)]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["soundboard"] == {"servable": 0, "pending": len(STARTER_CLIPS)}
