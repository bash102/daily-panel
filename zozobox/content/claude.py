"""Claude (Anthropic API) for text generation and safety review.

`ClaudeAPI.generate_json` sends one request and returns the structured JSON
result plus its usage (for spend logging). The real client uses the official
`anthropic` SDK (imported lazily), model `claude-opus-5-5`, adaptive thinking,
an explicit `output_config.effort`, structured outputs (`output_config.format`)
and server-side refusal fallbacks (`fallbacks="default"`, beta
`server-side-fallback-2026-07-01`). `stop_reason` is checked before content is
read. The API key comes from the environment (ANTHROPIC_API_KEY), never code.

`FakeClaude` answers from a scripted responder (no network) for tests.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .spend import Usage, anthropic_usage

log = logging.getLogger(__name__)

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

Content = str | list[dict[str, Any]]


class ClaudeError(RuntimeError):
    """A failed Claude call. `usage` is set when tokens were billed anyway."""

    def __init__(self, msg: str, usage: Usage | None = None, *, retryable: bool = True):
        super().__init__(msg)
        self.usage = usage
        self.retryable = retryable


class ClaudeRefusal(ClaudeError):
    """stop_reason == "refusal" (after any server-side fallback also declined)."""


@dataclass(frozen=True)
class ClaudeResult:
    data: dict[str, Any]
    usage: Usage | None = None
    model: str = DEFAULT_CLAUDE_MODEL


class ClaudeAPI(Protocol):
    model: str

    def generate_json(
        self,
        *,
        purpose: str,
        system: str,
        content: Content,
        schema: dict[str, Any],
        effort: str = "medium",
        max_tokens: int = 8000,
    ) -> ClaudeResult: ...


def image_block(data: bytes, media_type: str = "image/png") -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def _last_text(content) -> str | None:
    text = None
    for block in content or ():
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
    return text


class AnthropicClaude:
    def __init__(
        self,
        model: str = DEFAULT_CLAUDE_MODEL,
        *,
        client: Any = None,
        timeout_s: float = 180.0,
        max_retries: int = 2,
        fallbacks: bool = True,
    ):
        self.model = model
        self._client = client
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.fallbacks = fallbacks
        self._lock = threading.Lock()

    def _get_client(self):
        with self._lock:
            if self._client is None:
                import anthropic  # lazy: only the nightly job needs it

                # Reads ANTHROPIC_API_KEY from the environment.
                self._client = anthropic.Anthropic(
                    timeout=self.timeout_s, max_retries=self.max_retries
                )
            return self._client

    def generate_json(
        self,
        *,
        purpose: str,
        system: str,
        content: Content,
        schema: dict[str, Any],
        effort: str = "medium",
        max_tokens: int = 8000,
    ) -> ClaudeResult:
        import anthropic

        client = self._get_client()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        if self.fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            resp = client.beta.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            raise ClaudeError(f"{purpose}: bad request: {e.message}", retryable=False) from None
        except anthropic.AuthenticationError:
            raise ClaudeError(f"{purpose}: authentication failed (check ANTHROPIC_API_KEY)",
                              retryable=False) from None
        except anthropic.PermissionDeniedError as e:
            raise ClaudeError(f"{purpose}: permission denied: {e.message}", retryable=False) from None
        except anthropic.NotFoundError as e:
            raise ClaudeError(f"{purpose}: not found (model {self.model}?): {e.message}",
                              retryable=False) from None
        except anthropic.RateLimitError as e:
            raise ClaudeError(f"{purpose}: rate limited or provider spend limit: {e.message}") from None
        except anthropic.APIStatusError as e:
            raise ClaudeError(f"{purpose}: API error {e.status_code}: {e.message}") from None
        except anthropic.APITimeoutError:
            raise ClaudeError(f"{purpose}: request timed out") from None
        except anthropic.APIConnectionError:
            raise ClaudeError(f"{purpose}: network error") from None
        except anthropic.AnthropicError as e:
            raise ClaudeError(f"{purpose}: {type(e).__name__}: {e}") from None

        served = getattr(resp, "model", None) or self.model
        usage = anthropic_usage(served, getattr(resp, "usage", None))
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            cat = getattr(details, "category", None) if details is not None else None
            raise ClaudeRefusal(f"{purpose}: refused (category={cat})", usage)
        if stop == "max_tokens":
            raise ClaudeError(f"{purpose}: hit max_tokens", usage)
        if stop not in ("end_turn", "stop_sequence"):
            raise ClaudeError(f"{purpose}: unexpected stop_reason {stop!r}", usage)
        text = _last_text(getattr(resp, "content", None))
        if not text:
            raise ClaudeError(f"{purpose}: no text in response", usage)
        try:
            data = json.loads(text)
        except ValueError:
            raise ClaudeError(f"{purpose}: response was not JSON", usage) from None
        if not isinstance(data, dict):
            raise ClaudeError(f"{purpose}: JSON result is not an object", usage)
        return ClaudeResult(data, usage, served)


# ----------------------------------------------------------------- fake ----


@dataclass
class ClaudeCall:
    purpose: str
    system: str
    content: Content
    schema: dict[str, Any]
    effort: str
    max_tokens: int

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def has_image(self) -> bool:
        return not isinstance(self.content, str) and any(
            b.get("type") == "image" for b in self.content
        )


Responder = Callable[[ClaudeCall], "dict[str, Any] | Exception"]


def default_fake_response(call: ClaudeCall) -> dict[str, Any]:
    p = call.purpose
    if "review" in p:
        return {"verdict": "pass", "reasons": []}
    if p.startswith("story"):
        return {
            "title": "Zozo and the Sharing Picnic",
            "theme": "sharing at a picnic",
            "value": "sharing",
            "chunks": [
                "One sunny morning, Zozo packed a little basket. In went two red apples, "
                "a soft blanket, and a teddy bear named Bobo. Off to the park!",
                "At the park, Zozo met a friend named Mia. Mia had no snack. Zozo looked "
                "at the two red apples. Hmm. Then Zozo smiled and said, here you go!",
                "Mia gave a big happy smile. Crunch, crunch, crunch! The two friends ate "
                "their apples on the soft blanket and watched the ducks say quack quack.",
                "When it was time to go home, Mia gave Zozo a big hug. Sharing feels warm "
                "and cozy, said Zozo. The end. Sweet dreams, little friend.",
            ],
        }
    if p.startswith("letter"):
        from .evergreen import LETTER_WORDS

        m = re.search(r"letter is ([A-Z])", call.text())
        L = m.group(1) if m else "B"
        l = L.lower()
        return {"words": (list(LETTER_WORDS.get(L, ())) + [l + "oo", l + "ee", l + "aa"])[:4]}
    if p.startswith("weather"):
        return {"summary": "Sunny and cold",
                "sentence": "It is sunny and cold today, so wear your cozy jacket!"}
    if p.startswith("coloring"):
        return {"title": "Happy Turtle",
                "subject": "a smiling turtle sitting on a rock",
                "intro": "Here is a happy turtle for you to color!"}
    return {}


class FakeClaude:
    """Scripted Claude. `responder(call)` returns a dict (the JSON result) or an
    exception to raise. Usage is a fixed small amount per call."""

    def __init__(
        self,
        responder: Responder | None = None,
        *,
        model: str = DEFAULT_CLAUDE_MODEL,
        usd_per_call: float = 0.01,
    ):
        self.model = model
        self.responder = responder or default_fake_response
        self.usd_per_call = usd_per_call
        self.calls: list[ClaudeCall] = []
        self._lock = threading.Lock()

    def generate_json(self, *, purpose, system, content, schema, effort="medium",
                      max_tokens=8000) -> ClaudeResult:
        call = ClaudeCall(purpose, system, content, schema, effort, max_tokens)
        with self._lock:
            self.calls.append(call)
        usage = Usage("anthropic", self.model, 1000, 500, self.usd_per_call)
        out = self.responder(call)
        if isinstance(out, ClaudeError):
            if out.usage is None:
                out.usage = usage
            raise out
        if isinstance(out, BaseException):
            raise out
        return ClaudeResult(dict(out), usage, self.model)

    def purposes(self) -> list[str]:
        return [c.purpose for c in self.calls]
