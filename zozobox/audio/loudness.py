"""Loudness normalisation (SPEC 5): every asset to -16 LUFS, 48 kHz 16-bit WAV.

Two-pass ffmpeg `loudnorm`: pass 1 measures, pass 2 applies a linear gain
using the measurements (no dynamic pumping on speech). Run at asset-ingest
time (nightly job, starter pack), never on the playback path.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

TARGET_LUFS = -16.0
TRUE_PEAK_DB = -1.5
LRA = 11.0


class NormalizeError(RuntimeError):
    pass


RunCapture = Callable[[Sequence[str]], str]  # returns stderr text; raises on failure


def _run_ffmpeg(argv: Sequence[str]) -> str:
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NormalizeError(f"ffmpeg failed to run: {e}") from e
    if r.returncode != 0:
        raise NormalizeError(f"ffmpeg exited {r.returncode}: {r.stderr[-500:]}")
    return r.stderr


def _filter(target: float, tp: float, lra: float, measured: dict | None = None) -> str:
    f = f"loudnorm=I={target}:TP={tp}:LRA={lra}"
    if measured is None:
        return f + ":print_format=json"
    return (
        f + f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true:print_format=json"
    )


def parse_loudnorm_json(stderr: str) -> dict:
    """Extract the last JSON object loudnorm prints to stderr."""
    matches = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if not matches:
        raise NormalizeError("no loudnorm measurements in ffmpeg output")
    return json.loads(matches[-1])


def normalize_commands(
    src: Path, dst: Path, *, target_lufs: float = TARGET_LUFS, true_peak: float = TRUE_PEAK_DB,
    lra: float = LRA, rate: int = 48000, channels: int = 1, measured: dict | None = None,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Pass-1 command when `measured` is None, else the pass-2 command."""
    # Downmix *before* measuring: LUFS sums channel power, so a stereo file
    # downmixed after loudnorm would land ~3 dB quiet.
    layout = {1: "mono", 2: "stereo"}.get(channels)
    pre = f"aformat=channel_layouts={layout}," if layout else ""
    post = f",aresample={rate}" if measured is not None else ""
    base = [ffmpeg, "-hide_banner", "-nostdin", "-i", str(src),
            "-af", pre + _filter(target_lufs, true_peak, lra, measured) + post]
    if measured is None:
        return base + ["-f", "null", "-"]
    return base + ["-ar", str(rate), "-ac", str(channels), "-c:a", "pcm_s16le",
                   "-y", str(dst)]


def normalize(
    src: Path, dst: Path, *, target_lufs: float = TARGET_LUFS, true_peak: float = TRUE_PEAK_DB,
    lra: float = LRA, rate: int = 48000, channels: int = 1, ffmpeg: str = "ffmpeg",
    run: RunCapture = _run_ffmpeg,
) -> dict:
    """Normalise `src` (any format ffmpeg reads) into a WAV at `dst`.

    Returns the pass-1 measurements. Raises NormalizeError on failure; `dst`
    is written via a temp file so a failed run never leaves a broken asset.
    """
    src, dst = Path(src), Path(dst)
    kw = dict(target_lufs=target_lufs, true_peak=true_peak, lra=lra, rate=rate,
              channels=channels, ffmpeg=ffmpeg)
    measured = parse_loudnorm_json(run(normalize_commands(src, dst, **kw)))
    if measured.get("input_i") in ("-inf", None) or str(measured.get("input_i")).startswith("-inf"):
        # Pure silence: loudnorm cannot measure it; just convert.
        measured = None
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp.wav")
    try:
        if measured is None:
            run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(src), "-ar", str(rate),
                 "-ac", str(channels), "-c:a", "pcm_s16le", "-y", str(tmp)])
        else:
            run(normalize_commands(src, tmp, measured=measured, **kw))
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    return measured or {}
