from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from zozobox.content import Clients, FakeClaude, FakeMedia, FakeWeather, SpendCapReached, SpendMeter
from zozobox.content import __main__ as cli
from zozobox.content import spend as S
from zozobox.store import Store


def test_anthropic_usage_pricing():
    u = S.anthropic_usage("claude-opus-5-5", NS(input_tokens=1_000_000, output_tokens=100_000,
                                                cache_read_input_tokens=1_000_000,
                                                cache_creation_input_tokens=0))
    assert u.usd == pytest.approx(4.0 + 2.0 + 0.2)
    assert u.input_units == 2_000_000 and u.output_units == 100_000
    assert S.anthropic_usage("claude-opus-5-5", None).usd == 0


def test_unknown_model_priced_pessimistically():
    known = S.anthropic_usage("claude-opus-5-5", NS(input_tokens=1000, output_tokens=1000))
    unknown = S.anthropic_usage("mystery", NS(input_tokens=1000, output_tokens=1000))
    assert unknown.usd > known.usd
    assert S.openai_image_usage("gpt-image-2", None).usd == S.PRICES["gpt-image-2"]["per_image"]
    assert S.openai_tts_usage("gpt-4o-mini-tts", "x" * 1000).usd == pytest.approx(0.015)
    assert S.openai_stt_usage("gpt-4o-mini-transcribe", 60).usd == pytest.approx(0.003)


def test_meter_records_and_caps(store, clock):
    m = SpendMeter(store, clock)
    m.record(S.Usage("openai", "gpt-image-2", 10, 20, 0.5), "coloring_image")
    ev = store.events.query("spend")[0]
    assert ev.payload == {"provider": "openai", "model": "gpt-image-2", "purpose": "coloring_image",
                          "usd": 0.5, "input_units": 10, "output_units": 20}
    assert m.month_to_date() == 0.5 and m.run_usd == 0.5
    m.ensure_budget("x")  # under the cap
    store.settings.spend_cap_usd = 0.5
    with pytest.raises(SpendCapReached):
        m.ensure_budget("story")
    with pytest.raises(SpendCapReached):
        m.ensure_budget("letter")
    caps = store.events.query("spend_cap_reached")
    assert len(caps) == 1 and caps[0].payload["purpose"] == "story"
    assert caps[0].payload["cap_usd"] == 0.5


def test_month_boundary_is_local(store, clock):
    m = SpendMeter(store, clock)
    clock.set_local(2026, 2, 28, 23, 30)
    m.record(S.Usage("anthropic", "m", 1, 1, 5.0), "a")
    clock.set_local(2026, 3, 1, 0, 30)  # still Feb 28 in UTC? no: 05:30 UTC Mar 1
    m.record(S.Usage("anthropic", "m", 1, 1, 1.0), "b")
    assert m.month_to_date() == 1.0
    assert m.month_start().day == 1 and m.month_start().hour == 0


def test_meter_ignores_garbage_payloads(store, clock):
    store.events.append("spend", {"usd": "lots"})
    store.events.append("spend", {"usd": True})
    store.events.append("spend", {})
    assert SpendMeter(store, clock).month_to_date() == 0


# ------------------------------------------------------------------ CLI ----


@pytest.fixture
def cli_env(tmp_path, monkeypatch, normalizer):
    data = tmp_path / "data"
    s = Store(data)
    s.settings.set_location("Home", 1.0, 2.0)
    s.close()
    fakes = Clients(FakeClaude(), FakeMedia(), FakeWeather(), normalizer)
    monkeypatch.setattr("zozobox.content.pipeline.Clients.from_env",
                        classmethod(lambda cls, config=None: fakes))
    return data, fakes


def test_cli_status(cli_env, capsys):
    data, _ = cli_env
    assert cli.main(["status", "--data-dir", str(data)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["spend_cap_usd"] == 30.0 and "needs_generation" in out


def test_cli_bootstrap_then_nightly(cli_env, capsys):
    data, fakes = cli_env
    assert cli.main(["bootstrap", "--data-dir", str(data)]) == 0
    capsys.readouterr()
    assert cli.main(["nightly", "--data-dir", str(data), "--job", "story"]) == 0
    assert json.loads(capsys.readouterr().out) == {"story": "generated"}
    assert cli.main(["weather", "--data-dir", str(data)]) == 0
    assert json.loads(capsys.readouterr().out) == {"weather": "generated"}


def test_cli_failure_exit_code(cli_env, capsys):
    data, fakes = cli_env
    fakes.weather.error = RuntimeError("down")
    assert cli.main(["weather", "--data-dir", str(data), "--force"]) == 1


def test_cli_bad_store(tmp_path, capsys):
    f = tmp_path / "file"
    f.write_text("x")
    assert cli.main(["status", "--data-dir", str(f)]) == 2
    assert "cannot open store" in capsys.readouterr().out


def test_cli_bad_result_detection():
    assert cli._bad({"a": "generated", "b": "skipped"}) is False
    assert cli._bad({"a": "timeout"}) is True
    assert cli._bad({"phrases": {"x": "failed"}}) is True
    assert cli._bad({"locked": True}) is True
