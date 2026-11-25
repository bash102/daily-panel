"""zozobox-import-clip: add sound files to the soundboard (SPEC 2.3).

    zozobox-import-clip cow.flac --source https://freesound.org/s/12345/ --license CC0
    zozobox-import-clip *.wav --dir ~/.local/share/zozobox/soundboard

The default directory is the engine's: box.toml `[app].soundboard_dir`
($ZOZOBOX_CONFIG or ~/.config/zozobox/box.toml), else <data dir>/soundboard.
Run it as the zozobox user on a file that user can read (not one in an
admin's 0700 home directory): see docs/DEPLOY.md.

Each file is loudness-normalized to -16 LUFS, 48 kHz mono WAV with
`zozobox.audio.normalize` (needs ffmpeg) into `<dir>/imported/<name>.wav`,
and a line is appended to `<dir>/CREDITS.tsv` (file, source, license,
author). Clips longer than --max-s (default 20 s) are refused: the
soundboard is for short sounds. The engine picks up new files on the next
press; no restart needed.

Only add clips you are allowed to use. Freesound's "Creative Commons 0"
license filter finds clips that need no attribution; see docs/DEPLOY.md.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

IMPORTED_SUBDIR = "imported"
CREDITS = "CREDITS.tsv"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:60] or "clip"


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


def import_clip(src: Path, soundboard_dir: Path, *, name: str | None = None,
                source: str = "", license: str = "", author: str = "",
                max_s: float = 20.0, overwrite: bool = False,
                normalize: Callable[[Path, Path], object] | None = None) -> Path:
    """Normalize `src` into the soundboard. Returns the new file's path.
    Raises ValueError (too long, exists) or the normalizer's error."""
    if normalize is None:
        from .audio import normalize as _norm

        normalize = _norm
    src = Path(src)
    try:
        ok = src.is_file() and os.access(src, os.R_OK)
    except PermissionError:
        ok = False
    if not ok:
        if src.exists() or not os.access(src.parent, os.X_OK):
            raise ValueError(f"{src}: cannot read it as this user (copy it somewhere "
                             f"world-readable such as /tmp first)")
        raise ValueError(f"{src}: no such file")
    out_dir = Path(soundboard_dir) / IMPORTED_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{slugify(name or src.stem)}.wav"
    if dest.exists() and not overwrite:
        raise ValueError(f"{dest} already exists (use --name or --overwrite)")
    tmp = out_dir / f".{dest.stem}.importing.wav"
    try:
        normalize(src, tmp)
        secs = wav_seconds(tmp)
        if secs > max_s:
            raise ValueError(f"{src}: {secs:.1f}s is longer than {max_s:.0f}s (--max-s)")
        if secs <= 0.05:
            raise ValueError(f"{src}: empty after normalizing")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    credits = Path(soundboard_dir) / CREDITS
    new = not credits.exists()
    with open(credits, "a", encoding="utf-8") as f:
        if new:
            f.write("file\tsource\tlicense\tauthor\timported_at\n")
        clean = [str(x).replace("\t", " ").replace("\n", " ") for x in
                 (dest.relative_to(soundboard_dir).as_posix(), source, license, author,
                  datetime.now(timezone.utc).isoformat(timespec="seconds"))]
        f.write("\t".join(clean) + "\n")
    return dest


def main(argv: Sequence[str] | None = None, *,
         normalize: Callable[[Path, Path], object] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zozobox-import-clip", description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--dir", type=Path, default=None,
                    help="soundboard dir (default: the engine's [app].soundboard_dir from "
                         "box.toml, else <data dir>/soundboard)")
    ap.add_argument("--config", default=None, help="box.toml (default $ZOZOBOX_CONFIG or "
                                                   "~/.config/zozobox/box.toml)")
    ap.add_argument("--name", help="file name for a single clip (default: from the source name)")
    ap.add_argument("--source", default="", help="where it came from, e.g. the Freesound URL")
    ap.add_argument("--license", default="CC0", help="license (default CC0)")
    ap.add_argument("--author", default="")
    ap.add_argument("--max-s", type=float, default=20.0)
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    if a.name and len(a.files) > 1:
        ap.error("--name works with a single file only")
    if a.dir is None:
        from .boxconfig import ConfigError, load_app_config

        try:
            a.dir = load_app_config(a.config, required=bool(a.config)).soundboard_dir
        except ConfigError as e:
            print(f"zozobox-import-clip: config error: {e}", file=sys.stderr)
            return 2
    bad = 0
    for f in a.files:
        try:
            dest = import_clip(f, a.dir, name=a.name, source=a.source, license=a.license,
                               author=a.author, max_s=a.max_s, overwrite=a.overwrite,
                               normalize=normalize)
            print(f"added {dest}")
        except Exception as e:
            print(f"skipped {f}: {e}", file=sys.stderr)
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
