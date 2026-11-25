"""Spend tracking (SPEC 7.5) shared with the dashboard.

After every paid API call the pipeline logs a store event of type `spend`:

    {provider: "anthropic"|"openai", model, purpose, usd, input_units, output_units}

`usd` is an estimate from the call's usage and the price table below (verify
the prices at build time; providers change them). Before each paid call the
month-to-date sum of `usd` is compared with `settings.spend_cap_usd`; at or
over the cap the call is refused and one `spend_cap_reached` event is logged
per run.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

log = logging.getLogger(__name__)

SPEND_EVENT = "spend"
CAP_EVENT = "spend_cap_reached"

# USD per 1M units. Anthropic: tokens. OpenAI images: tokens (text in, image
# out). OpenAI TTS: input characters (no usage is returned for speech).
# OpenAI STT: audio seconds -> per-minute price below. VERIFY AT BUILD.
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20},
    "claude-opus-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50},
    "gpt-image-2": {"input": 5.00, "output": 40.00, "per_image": 0.05},
    "gpt-image-1": {"input": 5.00, "output": 40.00, "per_image": 0.05},
    "gpt-4o-mini-tts": {"input_chars": 15.00},
    "gpt-4o-mini-transcribe": {"per_minute": 0.003},
}
# Unknown models are priced pessimistically so the cap still bites.
DEFAULT_PRICE = {"input": 15.00, "output": 75.00, "cache_read": 1.50,
                 "per_image": 0.20, "input_chars": 30.00, "per_minute": 0.02}


def _price(model: str, key: str) -> float:
    return PRICES.get(model, {}).get(key, DEFAULT_PRICE[key])


@dataclass(frozen=True)
class Usage:
    """What one paid call consumed. usd is the estimate logged as spend."""

    provider: str
    model: str
    input_units: int = 0
    output_units: int = 0
    usd: float = 0.0


def anthropic_usage(model: str, usage) -> Usage:
    """From an Anthropic `usage` object (thinking tokens are in output_tokens)."""
    def g(name: str) -> int:
        v = getattr(usage, name, None) if usage is not None else None
        return int(v) if isinstance(v, (int, float)) else 0

    inp, out = g("input_tokens"), g("output_tokens")
    cache_w, cache_r = g("cache_creation_input_tokens"), g("cache_read_input_tokens")
    usd = (
        (inp + cache_w * 1.25) * _price(model, "input")
        + cache_r * _price(model, "cache_read")
        + out * _price(model, "output")
    ) / 1e6
    return Usage("anthropic", model, inp + cache_w + cache_r, out, round(usd, 6))


def openai_image_usage(model: str, usage) -> Usage:
    def g(name: str) -> int:
        v = getattr(usage, name, None) if usage is not None else None
        return int(v) if isinstance(v, (int, float)) else 0

    inp, out = g("input_tokens"), g("output_tokens")
    if inp == 0 and out == 0:
        return Usage("openai", model, 0, 1, _price(model, "per_image"))
    usd = (inp * _price(model, "input") + out * _price(model, "output")) / 1e6
    return Usage("openai", model, inp, out, round(usd, 6))


def openai_tts_usage(model: str, text: str) -> Usage:
    n = len(text)
    return Usage("openai", model, n, 0, round(n * _price(model, "input_chars") / 1e6, 6))


def openai_stt_usage(model: str, seconds: float) -> Usage:
    secs = max(0.0, float(seconds))
    return Usage("openai", model, int(round(secs)), 0,
                 round(secs / 60.0 * _price(model, "per_minute"), 6))


class SpendCapReached(RuntimeError):
    pass


class SpendMeter:
    """Checks the cap before paid calls and logs `spend` events after them."""

    def __init__(self, store, clock: Callable[[], datetime] | None = None):
        self.store = store
        self.clock = clock or store.now
        self._lock = threading.Lock()
        self._cap_logged = False
        self.run_usd = 0.0

    def month_start(self) -> datetime:
        local = self.store.settings.local(self.clock())
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def month_to_date(self) -> float:
        total = 0.0
        for ev in self.store.events.query(SPEND_EVENT, since=self.month_start(), limit=None):
            v = ev.payload.get("usd")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total += float(v)
        return total

    def cap(self) -> float:
        return float(self.store.settings.spend_cap_usd)

    def cap_reached(self) -> bool:
        try:
            return self.month_to_date() >= self.cap()
        except Exception:
            log.exception("spend: could not read month-to-date spend; assuming capped")
            return True

    def ensure_budget(self, purpose: str) -> None:
        """Raise SpendCapReached (logging one event per run) when capped."""
        if not self.cap_reached():
            return
        with self._lock:
            first = not self._cap_logged
            self._cap_logged = True
        if first:
            try:
                self.store.events.append(CAP_EVENT, {
                    "purpose": purpose,
                    "month_to_date_usd": round(self.month_to_date(), 4),
                    "cap_usd": self.cap(),
                })
            except Exception:
                log.exception("spend: could not log spend_cap_reached")
        raise SpendCapReached(f"monthly spend cap reached (before {purpose})")

    def record(self, usage: Usage | None, purpose: str) -> None:
        if usage is None:
            return
        with self._lock:
            self.run_usd += usage.usd
        try:
            self.store.events.append(SPEND_EVENT, {
                "provider": usage.provider,
                "model": usage.model,
                "purpose": purpose,
                "usd": usage.usd,
                "input_units": usage.input_units,
                "output_units": usage.output_units,
            })
        except Exception:
            log.exception("spend: could not log spend for %s", purpose)
