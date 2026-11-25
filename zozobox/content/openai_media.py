"""OpenAI images, text-to-speech and transcription.

The real client uses the official `openai` SDK (imported lazily):
`client.images.generate`, `client.audio.speech.create` and
`client.audio.transcriptions.create`. The key comes from the environment
(OPENAI_API_KEY). Model names are configurable; the defaults ("gpt-image-2",
"gpt-4o-mini-tts", "gpt-4o-mini-transcribe") must be verified at build time.

One fixed warm, gentle voice is used for every clip (SPEC 7.0).
`FakeMedia` returns tiny valid WAV/PNG bytes for tests.
"""

from __future__ import annotations

import base64
import io
import logging
import struct
import threading
import wave
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .spend import Usage, openai_image_usage, openai_stt_usage, openai_tts_usage

log = logging.getLogger(__name__)

DEFAULT_IMAGE_MODEL = "gpt-image-2"  # verify at build
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"  # verify at build
DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"  # verify at build
DEFAULT_VOICE = "coral"  # verify at build: pick the warmest, gentlest voice
DEFAULT_VOICE_INSTRUCTIONS = (
    "Speak warmly, gently and a little slowly, like a kind preschool teacher talking "
    "to a two-year-old. Cheerful and calm, never loud or dramatic."
)


class MediaError(RuntimeError):
    def __init__(self, msg: str, usage: Usage | None = None, *, retryable: bool = True):
        super().__init__(msg)
        self.usage = usage
        self.retryable = retryable


@dataclass(frozen=True)
class MediaResult:
    data: bytes
    usage: Usage | None = None
    ext: str = "wav"


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    usage: Usage | None = None


class MediaAPI(Protocol):
    def tts(self, text: str) -> MediaResult: ...      # WAV bytes
    def image(self, prompt: str) -> MediaResult: ...  # PNG bytes


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


class OpenAIMedia:
    def __init__(
        self,
        *,
        image_model: str = DEFAULT_IMAGE_MODEL,
        tts_model: str = DEFAULT_TTS_MODEL,
        stt_model: str = DEFAULT_STT_MODEL,
        voice: str = DEFAULT_VOICE,
        voice_instructions: str = DEFAULT_VOICE_INSTRUCTIONS,
        image_size: str = "1024x1024",
        image_quality: str = "medium",
        client: Any = None,
        timeout_s: float = 180.0,
        max_retries: int = 2,
    ):
        self.image_model = image_model
        self.tts_model = tts_model
        self.stt_model = stt_model
        self.voice = voice
        self.voice_instructions = voice_instructions
        self.image_size = image_size
        self.image_quality = image_quality
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._client = client
        self._lock = threading.Lock()

    def _get_client(self):
        with self._lock:
            if self._client is None:
                import openai  # lazy

                # Reads OPENAI_API_KEY from the environment.
                self._client = openai.OpenAI(timeout=self.timeout_s, max_retries=self.max_retries)
            return self._client

    def _call(self, what: str, fn: Callable[[], Any]) -> Any:
        import openai

        try:
            return fn()
        except openai.BadRequestError as e:
            # Includes the provider's own moderation rejecting a prompt.
            raise MediaError(f"{what}: bad request: {e.message}", retryable=False) from None
        except openai.AuthenticationError:
            raise MediaError(f"{what}: authentication failed (check OPENAI_API_KEY)",
                             retryable=False) from None
        except openai.PermissionDeniedError as e:
            raise MediaError(f"{what}: permission denied: {e.message}", retryable=False) from None
        except openai.NotFoundError as e:
            raise MediaError(f"{what}: not found (model name?): {e.message}",
                             retryable=False) from None
        except openai.RateLimitError as e:
            raise MediaError(f"{what}: rate limited or budget reached: {e.message}") from None
        except openai.APIStatusError as e:
            raise MediaError(f"{what}: API error {e.status_code}: {e.message}") from None
        except openai.APITimeoutError:
            raise MediaError(f"{what}: request timed out") from None
        except openai.APIConnectionError:
            raise MediaError(f"{what}: network error") from None
        except openai.OpenAIError as e:
            raise MediaError(f"{what}: {type(e).__name__}: {e}") from None

    def tts(self, text: str) -> MediaResult:
        client = self._get_client()
        resp = self._call("tts", lambda: client.audio.speech.create(
            model=self.tts_model,
            voice=self.voice,
            input=text,
            instructions=self.voice_instructions,
            response_format="wav",
        ))
        usage = openai_tts_usage(self.tts_model, text)
        data = resp.read() if hasattr(resp, "read") else getattr(resp, "content", b"")
        if not data:
            raise MediaError("tts: empty audio", usage)
        return MediaResult(bytes(data), usage, "wav")

    def image(self, prompt: str) -> MediaResult:
        client = self._get_client()
        resp = self._call("image", lambda: client.images.generate(
            model=self.image_model,
            prompt=prompt,
            n=1,
            size=self.image_size,
            quality=self.image_quality,
            output_format="png",
            background="opaque",
        ))
        usage = openai_image_usage(self.image_model, getattr(resp, "usage", None))
        items = getattr(resp, "data", None) or []
        b64 = getattr(items[0], "b64_json", None) if items else None
        if not b64:
            raise MediaError("image: no image data returned", usage)
        try:
            data = base64.b64decode(b64)
        except ValueError:
            raise MediaError("image: bad base64 data", usage) from None
        return MediaResult(data, usage, "png")

    def transcribe(self, path: str | Path, *, prompt: str | None = None) -> TranscriptResult:
        """Speech to text (for M7 mood answers)."""
        client = self._get_client()
        path = Path(path)

        def go():
            with open(path, "rb") as f:
                kw: dict[str, Any] = dict(model=self.stt_model, file=f)
                if prompt:
                    kw["prompt"] = prompt
                return client.audio.transcriptions.create(**kw)

        resp = self._call("transcribe", go)
        usage = openai_stt_usage(self.stt_model, _wav_seconds(path))
        text = resp if isinstance(resp, str) else getattr(resp, "text", "")
        return TranscriptResult((text or "").strip(), usage)


# ----------------------------------------------------------------- fakes ----


def tiny_wav(seconds: float = 0.1, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def tiny_png(w: int = 8, h: int = 8) -> bytes:
    """A white grayscale PNG, no Pillow needed."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeMedia:
    """Records prompts; returns tiny WAV/PNG. `fail_tts` / `fail_image` may be
    a predicate on the text/prompt, or True, to raise MediaError."""

    def __init__(self, *, fail_tts: Any = False, fail_image: Any = False,
                 usd_tts: float = 0.001, usd_image: float = 0.04):
        self.fail_tts = fail_tts
        self.fail_image = fail_image
        self.usd_tts = usd_tts
        self.usd_image = usd_image
        self.tts_texts: list[str] = []
        self.image_prompts: list[str] = []
        self.transcribed: list[Path] = []
        self.transcript = ""
        self._lock = threading.Lock()

    @staticmethod
    def _fails(rule: Any, arg: str) -> bool:
        return rule(arg) if callable(rule) else bool(rule)

    def tts(self, text: str) -> MediaResult:
        with self._lock:
            self.tts_texts.append(text)
        usage = Usage("openai", DEFAULT_TTS_MODEL, len(text), 0, self.usd_tts)
        if self._fails(self.fail_tts, text):
            raise MediaError("fake tts failure", usage)
        return MediaResult(tiny_wav(), usage, "wav")

    def image(self, prompt: str) -> MediaResult:
        with self._lock:
            self.image_prompts.append(prompt)
        usage = Usage("openai", DEFAULT_IMAGE_MODEL, 100, 1000, self.usd_image)
        if self._fails(self.fail_image, prompt):
            raise MediaError("fake image failure", usage)
        return MediaResult(tiny_png(), usage, "png")

    def transcribe(self, path, *, prompt=None) -> TranscriptResult:
        self.transcribed.append(Path(path))
        return TranscriptResult(self.transcript, Usage("openai", DEFAULT_STT_MODEL, 1, 0, 0.0001))
