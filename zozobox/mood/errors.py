"""Errors that mean "couldn't get an answer now" (the engine's offline path)."""

from __future__ import annotations

from typing import Any

# Kinds that are about one take (its audio or its answer), not about the
# service: retrying the same take is pointless, but other takes may work.
# Everything else (network, timeout, rate_limit, server, auth, config,
# not_found, unexpected, and Claude bad_request, which would hit every take)
# is treated as global: the reprocessor stops the run and retries later.
PER_TAKE_KINDS: frozenset[tuple[str, str]] = frozenset({
    ("openai", "bad_request"),   # OpenAI rejected this audio (corrupt/short WAV)
    ("openai", "unreadable"),    # the audio file can't be read locally
    ("anthropic", "truncated"),  # Claude stopped early (max_tokens/pause_turn)
})

# Kinds that mean the box is misconfigured (missing package/key, bad key):
# surfaced to the dashboard as an error event, not only a log line.
CONFIG_KINDS: frozenset[str] = frozenset({"config", "auth"})


class ServiceUnavailable(Exception):
    """A cloud call failed in a way that should leave the take pending.

    kind: network, timeout, auth, rate_limit, server, bad_request, not_found,
    config, unreadable, truncated or unexpected. The message never contains
    secrets. `spend` carries the cost of a call that was billed although it
    produced no usable answer (e.g. a truncated Claude reply)."""

    def __init__(self, provider: str, kind: str, detail: str = "", *, spend: Any = None):
        super().__init__(f"{provider} {kind}: {detail}" if detail else f"{provider} {kind}")
        self.provider = provider
        self.kind = kind
        self.detail = detail
        self.spend = spend

    @property
    def per_take(self) -> bool:
        return (self.provider, self.kind) in PER_TAKE_KINDS

    @property
    def config_error(self) -> bool:
        return self.kind in CONFIG_KINDS
