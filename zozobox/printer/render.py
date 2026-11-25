"""Card rendering for the Brother QL-800 (SPEC section 2.1).

Every card is drawn in 8-bit grey at the final resolution and thresholded to a
1-bit ('1' mode) image exactly `WIDTH_PX` wide, with no dithering, so lines
come out clean and solid. Drawings are made of thick outlines a toddler can
color in. Fonts: DejaVu Sans Bold from the usual Debian paths, falling back to
Pillow's built-in font.
"""

from __future__ import annotations

import functools
import logging
import math
import textwrap
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .drawings import MOODS, draw_face, draw_weather, normalize_icon

log = logging.getLogger(__name__)

WIDTH_PX = 696  # 62 mm continuous paper on the QL-800 at 300 dpi
MARGIN = 24  # side margin inside the printable width
TOP = 30  # blank space at the top and bottom of every card
GAP = 24  # vertical gap between blocks
MAX_ART_HEIGHT = 1400  # ~12 cm: keeps a coloring page from becoming a scroll
INNER = WIDTH_PX - 2 * MARGIN

FONT_PATHS: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",  # anywhere FreeType can find it by name
)

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


@functools.lru_cache(maxsize=64)
def font(size: int) -> Font:
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    log.warning("DejaVuSans-Bold not found; using Pillow's default font")
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


# ---------------------------------------------------------------- layout ----


def _text_size(f: Font, text: str, stroke: int = 0) -> tuple[int, int, int, int]:
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    return probe.textbbox((0, 0), text, font=f, stroke_width=stroke)


def text_block(
    text: str, size: int, *, outline: int = 0, width: int = INNER, align: str = "center"
) -> Image.Image:
    """Text (possibly several lines) as a tight grey block. `outline` > 0 draws
    hollow letters with that stroke width, for coloring in."""
    f = font(size)
    lines = text.split("\n")
    boxes = [_text_size(f, ln or " ", outline) for ln in lines]
    line_h = max(b[3] - b[1] for b in boxes)
    spacing = size // 5
    h = line_h * len(lines) + spacing * (len(lines) - 1)
    img = Image.new("L", (width, max(h, 1)), 255)
    d = ImageDraw.Draw(img)
    y = 0
    for ln, (x0, y0, x1, y1) in zip(lines, boxes):
        w = x1 - x0
        x = (width - w) // 2 if align == "center" else 0
        if outline:
            d.text((x - x0, y - y0), ln, font=f, fill=255, stroke_width=outline, stroke_fill=0)
        else:
            d.text((x - x0, y - y0), ln, font=f, fill=0)
        y += line_h + spacing
    return img


def fit_size(text: str, start: int, width: int = INNER, outline: int = 0, minimum: int = 12) -> int:
    """Largest font size <= start at which `text` fits on one line in `width`."""
    size = start
    while size > minimum:
        x0, _, x1, _ = _text_size(font(size), text, outline)
        if x1 - x0 <= width:
            return size
        size = int(size * 0.92)
    return minimum


def wrap(text: str, size: int, width: int = INNER) -> str:
    """Word-wrap to the pixel width at the given font size."""
    f = font(size)
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip()
        x0, _, x1, _ = _text_size(f, cand)
        if cur and x1 - x0 > width:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    # A single word wider than the line: fall back to hard character wrapping.
    out: list[str] = []
    for ln in lines:
        x0, _, x1, _ = _text_size(f, ln)
        if x1 - x0 > width:
            per = max(1, int(len(ln) * width / (x1 - x0)))
            out.extend(textwrap.wrap(ln, per, break_long_words=True))
        else:
            out.append(ln)
    return "\n".join(out)


def stack(blocks: Iterable[Image.Image | None], gap: int = GAP) -> Image.Image:
    """Center grey blocks vertically on a WIDTH_PX card and threshold to 1-bit."""
    blocks = [b for b in blocks if b is not None]
    h = 2 * TOP + sum(b.height for b in blocks) + gap * max(len(blocks) - 1, 0)
    card = Image.new("L", (WIDTH_PX, h), 255)
    y = TOP
    for b in blocks:
        card.paste(b.convert("L"), ((WIDTH_PX - b.width) // 2, y))
        y += b.height + gap
    return to_1bit(card)


def to_1bit(img: Image.Image, threshold: int = 128) -> Image.Image:
    """Hard threshold (no dithering) to a '1' mode image."""
    return img.convert("L").point(lambda v: 255 if v >= threshold else 0, mode="1")


def trim(img: Image.Image, pad: int = 8) -> Image.Image:
    """Crop surrounding blank paper, keeping `pad` px around the ink."""
    box = ImageOps.invert(img.convert("L")).getbbox()
    if box is None:
        return img
    x0, y0, x1, y1 = box
    return img.crop((max(x0 - pad, 0), max(y0 - pad, 0), min(x1 + pad, img.width), min(y1 + pad, img.height)))


# --------------------------------------------------------------- line art ---


def flatten(img: Image.Image) -> Image.Image:
    """Any input mode -> 'L' on a white background (transparency becomes white)."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba)
    if img.mode == "1":
        return img.convert("L")
    if img.mode in ("I", "I;16", "F"):
        img = ImageOps.autocontrast(img.convert("F").convert("L"))
    return img.convert("L")


def line_art(
    art: Image.Image,
    max_w: int = INNER,
    max_h: int = MAX_ART_HEIGHT,
    *,
    threshold: int = 150,
    thicken: int = 5,
) -> Image.Image:
    """Turn arbitrary art (color, grey, alpha, 1-bit) into clean black line art.

    Scales to fit `max_w` x `max_h` (keeping aspect), stretches the contrast,
    thresholds without dithering (light color fills drop out to white, dark
    lines stay black), and dilates dark pixels by `thicken` so thin lines
    survive downscaling and are thick enough to color along. Returns 'L'
    with values 0/255.
    """
    g = flatten(art)
    if g.width < 1 or g.height < 1:
        raise ValueError("empty image")
    g = ImageOps.autocontrast(g, cutoff=1)
    scale = min(max_w / g.width, max_h / g.height)
    size = (max(1, round(g.width * scale)), max(1, round(g.height * scale)))
    g = g.resize(size, Image.Resampling.LANCZOS)
    g = g.point(lambda v: 255 if v >= threshold else 0)
    if thicken and thicken > 1:
        k = thicken if thicken % 2 else thicken + 1
        g = g.filter(ImageFilter.MinFilter(k))
    return g


# ------------------------------------------------------------------ cards ---


def _fmt_temp(c: float) -> str:
    try:
        c = float(c)
    except (TypeError, ValueError):
        return "--"
    if not math.isfinite(c):
        return "--"
    return f"{int(round(c))}°C"


def render_letter_card(letter: str, words: Sequence[str], drawing: Image.Image | None) -> Image.Image:
    """Hollow "Bb" glyphs to color, up to three vocabulary words, optional drawing."""
    if not isinstance(letter, str) or len(letter.strip()) != 1 or not letter.strip().isalpha():
        raise ValueError(f"letter must be a single alphabetic character, got {letter!r}")
    ch = letter.strip()
    glyph = ch.upper() + ch.lower()
    outline = 9
    size = fit_size(glyph, 430, INNER - 2 * outline, outline)
    blocks: list[Image.Image | None] = [text_block(glyph, size, outline=outline)]
    shown = [w.strip() for w in words if w and w.strip()][:3]
    if shown:
        wsize = min(fit_size(w.capitalize(), 84) for w in shown)
        blocks.append(text_block("\n".join(w.capitalize() for w in shown), wsize))
    if drawing is not None:
        blocks.append(line_art(drawing, max_h=900))
    return stack(blocks)


def render_weather_card(summary: str, icon: str, hi_c: float, lo_c: float) -> Image.Image:
    """Big outlined weather icon, the spoken summary, and the high/low."""
    name = normalize_icon(icon)
    if name != (icon or "").strip().lower():
        log.debug("weather icon %r drawn as %r", icon, name)
    pic = trim(draw_weather(name, 560))
    blocks: list[Image.Image | None] = [pic]
    if summary and summary.strip():
        blocks.append(text_block(wrap(summary.strip(), 54), 54))
    temps = f"High {_fmt_temp(hi_c)}   Low {_fmt_temp(lo_c)}"
    blocks.append(text_block(temps, fit_size(temps, 64)))
    return stack(blocks)


def render_mood_card(mood: str | None) -> Image.Image:
    """One big face with its name, or all four faces in a labelled 2x2 grid.

    `None` or an unrecognised label (e.g. "unknown") gives the grid.
    """
    key = (mood or "").strip().lower()
    if key in MOODS:
        face = draw_face(key, 560)
        return stack([face, text_block(key.capitalize(), 96)])
    if mood is not None:
        log.info("mood %r not one of %s; printing all faces", mood, MOODS)
    cell_w = WIDTH_PX // 2
    face_px = cell_w - 2 * 20
    label_size = 50
    cells = []
    for m in MOODS:
        cell = Image.new("L", (cell_w, face_px + 12 + 70), 255)
        cell.paste(draw_face(m, face_px), ((cell_w - face_px) // 2, 0))
        lab = text_block(m.capitalize(), label_size, width=cell_w)
        cell.paste(lab, (0, face_px + 12))
        cells.append(cell)
    grid = Image.new("L", (WIDTH_PX, 2 * cells[0].height + GAP), 255)
    for i, cell in enumerate(cells):
        grid.paste(cell, ((i % 2) * cell_w, (i // 2) * (cell.height + GAP)))
    return stack([grid])


def render_coloring_page(art: Image.Image, title: str | None = None) -> Image.Image:
    """Generated art cleaned to thick black lines, with an optional title."""
    blocks: list[Image.Image | None] = []
    if title and title.strip():
        t = title.strip()
        size = 64
        if fit_size(t, size) < 44:
            blocks.append(text_block(wrap(t, 44), 44))
        else:
            blocks.append(text_block(t, fit_size(t, size)))
    blocks.append(line_art(art))
    return stack(blocks)


__all__ = [
    "WIDTH_PX",
    "FONT_PATHS",
    "font",
    "line_art",
    "to_1bit",
    "render_letter_card",
    "render_weather_card",
    "render_mood_card",
    "render_coloring_page",
]
