"""Spend logging and the monthly spend cap (SPEC 7.5).

Contract shared with the dashboard: after every paid API call an event
`spend` is logged with payload

    {provider: "anthropic"|"openai", model, purpose, usd, input_units, output_units}

(plus `unit`: "tokens" or "seconds", describing input_units). Before a paid
call, `SpendLedger.cap_reached()` refuses - and logs `spend_cap_reached` -
when the month-to-date sum of spend `usd` is >= settings `spend_cap_usd`.
The month is the calendar month in the box's timezone.

Prices are estimates for budgeting, not billing. VERIFY AT BUILD.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger(__name__)

SPEND = "spend"
SPEND_CAP_REACHED = "spend_cap_reached"

# USD per million tokens: (input, output). Claude prices from the Anthropic
# model table (2026-11). Unknown models are priced conservatively.
ANTHROPIC_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
ANTHROPIC_UNKNOWN = (10.0, 50.0)
CACHE_WRITE_FACTOR = 1.25
CACHE_READ_FACTOR = 0.10

# OpenAI speech-to-text. per_min applies when usage comes back as a duration
# (or is missing); tokens: (audio-in, text-out) USD per million tokens.
# VERIFY AT BUILD against the OpenAI pricing page.
OPENAI_STT_PRICES: dict[str, dict[str, Any]] = {
    "gpt-4o-mini-transcribe": {"per_min": 0.003, "tokens": (3.0, 5.0)},
    "gpt-4o-transcribe": {"per_min": 0.006, "tokens": (6.0, 10.0)},
    "whisper-1": {"per_min": 0.006, "tokens": (6.0, 10.0)},
}
OPENAI_STT_UNKNOWN = {"per_min": 0.006, "tokens": (6.0, 10.0)}


@dataclass(frozen=True)
class SpendItem:
    """One paid call. unit describes input_units ("tokens" or "seconds")."""

    provider: str
    model: str
    usd: float
    input_units: float
    output_units: float
    unit: str = "tokens"


def anthropic_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> SpendItem:
    pin, pout = ANTHROPIC_PRICES.get(model, ANTHROPIC_UNKNOWN)
    usd = (
        input_tokens * pin
        + cache_write_tokens * pin * CACHE_WRITE_FACTOR
        + cache_read_tokens * pin * CACHE_READ_FACTOR
        + output_tokens * pout
    ) / 1e6
    return SpendItem(
        "anthropic", model, round(usd, 8),
        input_tokens + cache_write_tokens + cache_read_tokens, output_tokens,
    )


def openai_stt_cost(
    model: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    seconds: float | None = None,
) -> SpendItem:
    p = OPENAI_STT_PRICES.get(model, OPENAI_STT_UNKNOWN)
    if input_tokens is not None:
        pin, pout = p["tokens"]
        out = output_tokens or 0
        usd = (input_tokens * pin + out * pout) / 1e6
        return SpendItem("openai", model, round(usd, 8), input_tokens, out, "tokens")
    secs = max(0.0, float(seconds or 0.0))
    usd = secs / 60.0 * p["per_min"]
    return SpendItem("openai", model, round(usd, 8), round(secs, 3), output_tokens or 0, "seconds")


class SpendLedger:
    """Reads and writes spend events in the store. Never raises."""

    def __init__(self, store, *, clock: Callable[[], datetime] | None = None):
        self.store = store
        self._clock = clock or store.now

    def _month_start(self) -> datetime:
        local = self.store.settings.local(self._clock())
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def month_to_date_usd(self) -> float:
        evs = self.store.events.query(SPEND, since=self._month_start(), limit=None)
        total = 0.0
        for ev in evs:
            try:
                total += float(ev.payload.get("usd") or 0.0)
            except (TypeError, ValueError):
                log.warning("spend: bad usd in event %s", ev.id)
        return total

    def cap_usd(self) -> float:
        return float(self.store.settings.spend_cap_usd)

    def cap_reached(self, purpose: str) -> bool:
        """True (and logs spend_cap_reached) when no paid call may be made.
        Fails closed: if the store can't be read, the cap counts as reached."""
        try:
            mtd, cap = self.month_to_date_usd(), self.cap_usd()
        except Exception:
            log.exception("spend: could not read month-to-date spend; refusing paid call")
            return True
        if mtd < cap:
            return False
        log.warning("spend: cap reached (%.4f >= %.2f USD), refusing %s", mtd, cap, purpose)
        try:
            self.store.events.append(
                SPEND_CAP_REACHED,
                {"purpose": purpose, "month_to_date_usd": round(mtd, 6), "cap_usd": cap},
            )
        except Exception:
            log.exception("spend: could not log spend_cap_reached")
        return True

    def record(self, item: SpendItem, purpose: str) -> None:
        try:
            self.store.events.append(
                SPEND,
                {
                    "provider": item.provider,
                    "model": item.model,
                    "purpose": purpose,
                    "usd": item.usd,
                    "input_units": item.input_units,
                    "output_units": item.output_units,
                    "unit": item.unit,
                },
            )
        except Exception:
            log.exception("spend: could not log spend event")
