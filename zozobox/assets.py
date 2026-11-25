"""Bundled chimes, generated deterministically (no binary files in the repo).

    python -m zozobox.assets DIR      # writes DIR/click.wav and DIR/beep.wav

The app calls `ensure_chimes(<data_dir>/chimes)` at startup, so a fresh box
always has them. The same code always produces the same bytes (stdlib only,
no randomness), and a file is rewritten only when its bytes differ.

- click: a short, soft 2 kHz tick (25 ms) played on every accepted press.
- beep: a gentle 880 Hz tone (300 ms) before the grandma reply records.

Both are 48 kHz 16-bit mono like every other asset, and deliberately quiet
(peak about -10 dBFS): they play on the box speaker, under its ceiling.
"""

from __future__ import annotations

import io
import math
import struct
import sys
import wave
from pathlib import Path

RATE = 48000
CLICK = "click.wav"
BEEP = "beep.wav"


def _wav(samples: list[float]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(
            struct.pack("<h", max(-32767, min(32767, round(s * 32767)))) for s in samples))
    return buf.getvalue()


def click_wav() -> bytes:
    n = int(0.025 * RATE)
    out = []
    for i in range(n):
        t = i / RATE
        attack = min(1.0, i / (0.001 * RATE))  # 1 ms ramp: no pop
        env = attack * math.exp(-t / 0.004)
        out.append(0.32 * env * math.sin(2 * math.pi * 2000 * t))
    return _wav(out)


def beep_wav() -> bytes:
    n = int(0.30 * RATE)
    fade = int(0.015 * RATE)
    out = []
    for i in range(n):
        env = min(1.0, i / fade, (n - 1 - i) / fade)
        t = i / RATE
        out.append(0.30 * env * math.sin(2 * math.pi * 880 * t))
    return _wav(out)


CHIMES = {CLICK: click_wav, BEEP: beep_wav}


def ensure_chimes(directory: str | Path) -> dict[str, Path]:
    """Write the chimes into `directory` when missing or different."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name, make in CHIMES.items():
        p = d / name
        data = make()
        try:
            same = p.read_bytes() == data
        except OSError:
            same = False
        if not same:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)
        out[name] = p
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0].startswith("-"):
        print("usage: python -m zozobox.assets DIR", file=sys.stderr)
        return 2
    for p in ensure_chimes(args[0]).values():
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
