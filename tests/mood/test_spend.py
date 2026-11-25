from datetime import timezone

import pytest

from zozobox.mood import SPEND, SPEND_CAP_REACHED, SpendItem, SpendLedger
from zozobox.mood.spend import anthropic_cost, openai_stt_cost


def test_anthropic_cost_opus_5_5():
    s = anthropic_cost("claude-opus-5-5", 1_000_000, 100_000)
    assert s.provider == "anthropic"
    assert s.usd == pytest.approx(4.0 + 2.0)
    assert (s.input_units, s.output_units, s.unit) == (1_000_000, 100_000, "tokens")


def test_anthropic_cost_cache_and_unknown_model():
    s = anthropic_cost("claude-opus-5-5", 0, 0, cache_write_tokens=1_000_000,
                       cache_read_tokens=1_000_000)
    assert s.usd == pytest.approx(4.0 * 1.25 + 4.0 * 0.1)
    unknown = anthropic_cost("claude-new-thing", 1_000_000, 0)
    assert unknown.usd >= 4.0  # conservative


def test_openai_stt_cost_tokens_and_seconds():
    t = openai_stt_cost("gpt-4o-mini-transcribe", input_tokens=1_000_000, output_tokens=0)
    assert t.usd == pytest.approx(3.0) and t.unit == "tokens"
    d = openai_stt_cost("gpt-4o-mini-transcribe", seconds=120)
    assert d.usd == pytest.approx(0.006) and d.unit == "seconds" and d.input_units == 120


def test_record_writes_contract_payload(store):
    led = SpendLedger(store)
    led.record(SpendItem("openai", "m", 0.01, 5, 2, "seconds"), "mood_stt")
    [ev] = store.events.query(SPEND)
    assert ev.payload == {"provider": "openai", "model": "m", "purpose": "mood_stt",
                          "usd": 0.01, "input_units": 5, "output_units": 2,
                          "unit": "seconds"}


def test_month_to_date_resets_at_local_month(store, clock):
    led = SpendLedger(store)
    clock.set_local(2026, 2, 28, 23, 30)  # last day of Feb, local
    led.record(SpendItem("openai", "m", 5.0, 1, 1), "x")
    clock.set_local(2026, 3, 1, 0, 30)    # March, local (still Feb in UTC? no: 05:30 UTC)
    assert led.month_to_date_usd() == 0
    led.record(SpendItem("anthropic", "m", 1.5, 1, 1), "x")
    led.record(SpendItem("anthropic", "m", 2.0, 1, 1), "x")
    assert led.month_to_date_usd() == pytest.approx(3.5)


def test_month_boundary_uses_box_timezone(store, clock):
    led = SpendLedger(store)
    # 2026-03-01 02:00 UTC is still Feb 28 in New York.
    clock.t = clock.t.replace(year=2026, month=3, day=1, hour=2, minute=0, tzinfo=timezone.utc)
    led.record(SpendItem("openai", "m", 7.0, 1, 1), "x")
    clock.set_local(2026, 3, 1, 12, 0)
    assert led.month_to_date_usd() == 0


def test_cap_reached_logs_event(store):
    store.settings.spend_cap_usd = 1.0
    led = SpendLedger(store)
    assert not led.cap_reached("mood_stt")
    led.record(SpendItem("openai", "m", 1.0, 1, 1), "x")
    assert led.cap_reached("mood_stt")
    [ev] = store.events.query(SPEND_CAP_REACHED)
    assert ev.payload["purpose"] == "mood_stt"
    assert ev.payload["cap_usd"] == 1.0
    assert ev.payload["month_to_date_usd"] == pytest.approx(1.0)


def test_zero_cap_blocks_everything(store):
    store.settings.spend_cap_usd = 0
    assert SpendLedger(store).cap_reached("x")


def test_bad_usd_ignored(store):
    store.events.append(SPEND, {"usd": "lots"})
    store.events.append(SPEND, {"usd": 0.5})
    assert SpendLedger(store).month_to_date_usd() == pytest.approx(0.5)


def test_store_failure_fails_closed_and_never_raises(store):
    led = SpendLedger(store)
    store.close()
    assert led.cap_reached("x") is True
    led.record(SpendItem("openai", "m", 1.0, 1, 1), "x")  # no raise
