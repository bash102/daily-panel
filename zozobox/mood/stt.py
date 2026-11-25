"""Speech-to-text for mood answers: OpenAI transcription (SPEC 7.0).

    Transcriber protocol: transcribe(path) -> Transcript   (blocking, WAV in)

Raises ServiceUnavailable for network, timeout, auth and API errors
(kind "bad_request" when OpenAI rejects this audio, "unreadable" when the
file can't be read: both are per-take, see errors.PER_TAKE_KINDS).
Only the audio clip and a generic hint are sent; no names or location.
"""

from __future__ import annotations

import logging
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from .errors import ServiceUnavailable
from .spend import SpendItem, openai_stt_cost

log = logging.getLogger(__name__)

DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"  # VERIFY AT BUILD
STT_MODEL_ENV = "ZOZOBOX_STT_MODEL"

# A neutral vocabulary hint: helps the recognizer with toddler speech.
STT_PROMPT = (
    "A small child answers the question 'How are you feeling?'. "
    "Likely words: happy, mad, sad, okay, fine, angry, grumpy."
)


@dataclass(frozen=True)
class Transcript:
    text: str
    spend: SpendItem | None = None


class Transcriber(Protocol):
    def transcribe(self, path: Path) -> Transcript: ...


def wav_seconds(path: Path) -> float:
    """Duration of a WAV file, 0.0 when it can't be read."""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else 0.0
    except Exception:
        return 0.0


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class OpenAITranscriber:
    """OpenAI `audio.transcriptions.create` with a short timeout.

    `client` is injected in tests; otherwise an `openai.OpenAI` client is
    built lazily (key from OPENAI_API_KEY)."""

    provider = "openai"

    def __init__(
        self,
        model: str | None = None,
        *,
        client: Any = None,
        timeout_s: float = 8.0,
        max_retries: int = 1,
        language: str = "en",
    ):
        self.model = model or os.environ.get(STT_MODEL_ENV) or DEFAULT_STT_MODEL
        self._client = client
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.language = language

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import openai
            except ImportError as e:
                raise ServiceUnavailable("openai", "config", "openai package not installed") from e
            try:
                self._client = openai.OpenAI(timeout=self.timeout_s, max_retries=self.max_retries)
            except openai.OpenAIError as e:  # e.g. OPENAI_API_KEY not set
                raise ServiceUnavailable("openai", "auth", type(e).__name__) from e
        return self._client

    def transcribe(self, path: Path) -> Transcript:
        path = Path(path)
        client = self._get_client()
        try:
            import openai
        except ImportError:  # only reachable with an injected client
            openai = None  # type: ignore[assignment]
        try:
            f = open(path, "rb")
        except OSError as e:
            raise ServiceUnavailable(
                "openai", "unreadable", f"cannot read audio: {type(e).__name__}") from e
        try:
            with f:
                resp = client.audio.transcriptions.create(
                    file=f,
                    model=self.model,
                    language=self.language,
                    prompt=STT_PROMPT,
                    timeout=self.timeout_s,
                )
        except Exception as e:
            raise _map_openai_error(e, openai) from e
        text = _get(resp, "text", None)
        if text is None and isinstance(resp, str):
            text = resp
        text = (text or "").strip()
        return Transcript(text, self._spend(resp, path))

    def _spend(self, resp: Any, path: Path) -> SpendItem:
        usage = _get(resp, "usage", None)
        utype = _get(usage, "type", None) if usage is not None else None
        if utype == "tokens":
            return openai_stt_cost(
                self.model,
                input_tokens=int(_get(usage, "input_tokens", 0) or 0),
                output_tokens=int(_get(usage, "output_tokens", 0) or 0),
            )
        if utype == "duration":
            return openai_stt_cost(self.model, seconds=float(_get(usage, "seconds", 0) or 0))
        return openai_stt_cost(self.model, seconds=wav_seconds(path))


def _map_openai_error(e: Exception, openai: Any) -> ServiceUnavailable:
    """Specific-first mapping of OpenAI SDK errors (never includes secrets)."""
    name = type(e).__name__
    if openai is None:
        return ServiceUnavailable("openai", "unexpected", name)
    if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
        kind = "auth"
    elif isinstance(e, openai.NotFoundError):
        kind = "not_found"
    elif isinstance(e, (openai.BadRequestError, openai.UnprocessableEntityError)):
        kind = "bad_request"
    elif isinstance(e, openai.RateLimitError):
        kind = "rate_limit"
    elif isinstance(e, openai.APIStatusError):
        kind = "server"
        name = f"{name} {getattr(e, 'status_code', '')}".strip()
    elif isinstance(e, openai.APITimeoutError):
        kind = "timeout"
    elif isinstance(e, openai.APIConnectionError):
        kind = "network"
    elif isinstance(e, openai.OpenAIError):
        kind = "unexpected"
    else:
        kind = "unexpected"
    return ServiceUnavailable("openai", kind, name)


class FakeTranscriber:
    """Scripted transcripts (str, Transcript or Exception), then `default`."""

    provider = "fake"

    def __init__(self, results: Iterable[str | Transcript | Exception] = (), *,
                 default: str | Transcript = "", spend: SpendItem | None = None):
        self.results = list(results)
        self.default = default
        self.spend = spend
        self.calls: list[Path] = []

    def transcribe(self, path: Path) -> Transcript:
        self.calls.append(Path(path))
        r = self.results.pop(0) if self.results else self.default
        if isinstance(r, Exception):
            raise r
        if isinstance(r, Transcript):
            return r
        return Transcript(r, self.spend)
