"""Recording core: stream 16-bit PCM chunks to a WAV, with cap, stop and silence.

Pure logic over a `PcmSource`, so tests feed synthetic chunks. The ALSA
backend supplies an `arecord`-backed source.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import wave
from array import array
from pathlib import Path
from typing import Protocol

from .base import Recording

log = logging.getLogger(__name__)

SAMPLE_BYTES = 2  # S16_LE
FULL_SCALE = 32768.0


class PcmSource(Protocol):
    def read(self, n: int) -> bytes: ...  # up to n bytes; b"" at end of stream
    def close(self) -> None: ...  # called by the reading thread when done
    # Optional: `kill()`, safe from another thread, makes a pending read()
    # return b"". Without it the watchdog falls back to close().


def rms_dbfs(pcm: bytes) -> float:
    """RMS level of little-endian 16-bit PCM in dBFS (-inf for silence/empty)."""
    n = len(pcm) // SAMPLE_BYTES
    if n == 0:
        return -math.inf
    a = array("h")
    a.frombytes(pcm[: n * SAMPLE_BYTES])
    if sys.byteorder != "little":
        a.byteswap()
    ms = sum(x * x for x in a) / n
    if ms <= 0:
        return -math.inf
    return 20 * math.log10(math.sqrt(ms) / FULL_SCALE)


def record_pcm(
    source: PcmSource,
    path: Path,
    max_s: float,
    stop: threading.Event,
    *,
    rate: int,
    channels: int = 1,
    chunk_s: float = 0.05,
    speech_dbfs: float = -40.0,
    min_speech_s: float = 0.3,
) -> Recording:
    """Copy PCM from `source` into a WAV at `path` until stop, max_s or EOF.

    Duration is measured in samples written, not wall time. Source errors end
    the take early; whatever was captured is kept. The source is always closed.
    """
    frame_bytes = SAMPLE_BYTES * channels
    max_frames = max(0, int(round(max_s * rate)))
    chunk_bytes = max(frame_bytes, int(rate * chunk_s) * frame_bytes)
    frames = 0
    speech_frames = 0
    carry = b""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(SAMPLE_BYTES)
            w.setframerate(rate)
            while frames < max_frames and not stop.is_set():
                try:
                    data = source.read(chunk_bytes)
                except Exception:
                    log.exception("recording source failed; keeping partial take")
                    break
                if not data:
                    break
                data = carry + data
                usable = len(data) - len(data) % frame_bytes
                data, carry = data[:usable], data[usable:]
                n = min(usable // frame_bytes, max_frames - frames)
                if n <= 0:
                    continue
                data = data[: n * frame_bytes]
                w.writeframes(data)
                frames += n
                if rms_dbfs(data) > speech_dbfs:
                    speech_frames += n
    finally:
        try:
            source.close()
        except Exception:
            log.exception("closing recording source failed")
    duration = frames / rate if rate else 0.0
    silent = speech_frames / rate < min_speech_s if rate else True
    return Recording(path=path, duration_s=duration, silent=silent)
