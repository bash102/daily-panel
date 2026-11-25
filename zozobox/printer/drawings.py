"""Programmatic thick-line drawings: mood faces and weather icons.

Everything is drawn at 2x and downsampled, giving smooth curves once the card
is thresholded. Outputs are square 'L' images (0 = black ink, 255 = paper)
with white interiors, so they work as coloring pictures.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

SS = 2  # supersampling factor

MOODS: tuple[str, ...] = ("happy", "mad", "sad", "neutral")

WEATHER_ICONS: tuple[str, ...] = (
    "sunny",
    "partly_cloudy",
    "cloudy",
    "rain",
    "snow",
    "storm",
    "fog",
    "wind",
)

_ALIASES = {
    "clear": "sunny",
    "sun": "sunny",
    "clear_day": "sunny",
    "mostly_sunny": "partly_cloudy",
    "partly_sunny": "partly_cloudy",
    "mostly_cloudy": "cloudy",
    "overcast": "cloudy",
    "clouds": "cloudy",
    "cloud": "cloudy",
    "drizzle": "rain",
    "showers": "rain",
    "rainy": "rain",
    "sleet": "snow",
    "snowy": "snow",
    "hail": "snow",
    "thunderstorm": "storm",
    "thunder": "storm",
    "lightning": "storm",
    "stormy": "storm",
    "mist": "fog",
    "haze": "fog",
    "foggy": "fog",
    "windy": "wind",
}


def normalize_icon(icon: str | None) -> str:
    """Map an icon name (any case, '-' or ' ' separators, common synonyms) to
    one of WEATHER_ICONS. Unknown names fall back to 'cloudy'."""
    key = (icon or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in WEATHER_ICONS:
        return key
    return _ALIASES.get(key, "cloudy")


def _canvas(px: int) -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
    s = px * SS
    img = Image.new("L", (s, s), 255)
    return img, ImageDraw.Draw(img), s


def _finish(img: Image.Image, px: int) -> Image.Image:
    return img.resize((px, px), Image.Resampling.LANCZOS)


def _ring(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, lw: float) -> None:
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=0, width=round(lw), fill=255)


def _dot(d: ImageDraw.ImageDraw, cx: float, cy: float, rx: float, ry: float | None = None) -> None:
    ry = rx if ry is None else ry
    d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=0)


def _line(d: ImageDraw.ImageDraw, pts, lw: float) -> None:
    """Polyline with round caps and joints."""
    w = round(lw)
    d.line(pts, fill=0, width=w, joint="curve")
    for x, y in (pts[0], pts[-1]):
        _dot(d, x, y, w / 2)


def _arc(d: ImageDraw.ImageDraw, cx, cy, r, start, end, lw) -> None:
    """Arc (degrees, PIL convention: 0 = 3 o'clock, clockwise) with round caps."""
    w = round(lw)
    d.arc((cx - r, cy - r, cx + r, cy + r), start, end, fill=0, width=w)
    rc = r - w / 2
    for a in (start, end):
        t = math.radians(a)
        _dot(d, cx + rc * math.cos(t), cy + rc * math.sin(t), w / 2)


# ------------------------------------------------------------------ faces ---


def draw_face(mood: str, px: int) -> Image.Image:
    if mood not in MOODS:
        raise ValueError(f"unknown mood {mood!r}; expected one of {MOODS}")
    img, d, s = _canvas(px)
    lw = s / 22
    c = s / 2
    _ring(d, c, c, s / 2 - lw / 2 - 2, lw)
    ex, ey, er = s * 0.17, s * 0.40, s * 0.055
    eye_ry = er * 1.35
    if mood == "mad":
        ey += s * 0.03
        eye_ry = er
    for sx in (-1, 1):
        _dot(d, c + sx * ex, ey, er, eye_ry)
    if mood == "happy":
        _arc(d, c, s * 0.50, s * 0.24, 25, 155, lw)
    elif mood == "sad":
        _arc(d, c, s * 0.86, s * 0.22, 215, 325, lw)
        # A tear to color in, under the right eye.
        _drop(d, c + ex + er * 0.4, ey + s * 0.20, s * 0.05, lw * 0.7)
    elif mood == "mad":
        for sx in (-1, 1):
            inner = (c + sx * s * 0.07, s * 0.32)
            outer = (c + sx * s * 0.26, s * 0.24)
            _line(d, [outer, inner], lw)
        _arc(d, c, s * 0.90, s * 0.26, 235, 305, lw)
    else:  # neutral
        _line(d, [(c - s * 0.16, s * 0.68), (c + s * 0.16, s * 0.68)], lw)
    return _finish(img, px)


# --------------------------------------------------------------- weather ----


def _sun(d, cx, cy, r, lw, rays: int = 8, ray_len: float | None = None) -> None:
    ray_len = r * 0.55 if ray_len is None else ray_len
    gap = lw * 1.2
    for i in range(rays):
        a = 2 * math.pi * i / rays
        r0, r1 = r + gap, r + gap + ray_len
        _line(d, [(cx + r0 * math.cos(a), cy + r0 * math.sin(a)), (cx + r1 * math.cos(a), cy + r1 * math.sin(a))], lw)
    _ring(d, cx, cy, r, lw)


def _cloud(d, x0, y0, w, lw) -> float:
    """Outlined cloud whose bounding box starts at (x0, y0) with width w.
    Returns the y of its flat bottom. The interior is painted white, so the
    cloud covers anything drawn earlier (e.g. the sun behind it)."""
    h = w * 0.58
    base = y0 + h
    puffs = [  # (center x, center y, radius) as fractions of w
        (0.20, 0.40, 0.18),
        (0.42, 0.26, 0.24),
        (0.68, 0.32, 0.21),
        (0.82, 0.40, 0.14),
    ]
    rect = (x0 + w * 0.06, y0 + h * 0.48, x0 + w * 0.94, base)
    rad = (rect[3] - rect[1]) / 2

    def shapes(shrink: float, fill: int) -> None:
        for fx, fy, fr in puffs:
            cx, cy, r = x0 + fx * w, y0 + fy * w, fr * w - shrink
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
        d.rounded_rectangle(
            (rect[0] + shrink, rect[1] + shrink, rect[2] - shrink, rect[3] - shrink),
            radius=max(rad - shrink, 1),
            fill=fill,
        )

    shapes(0, 0)  # union in ink...
    shapes(lw, 255)  # ...then the shrunken union in paper leaves only the outline
    return base


def _drop(d, cx, cy, r, lw) -> None:
    """Outlined raindrop with its round bottom centered at (cx, cy)."""
    tip = (cx, cy - r * 2.4)
    d.polygon([tip, (cx - r * 0.93, cy - r * 0.35), (cx + r * 0.93, cy - r * 0.35)], fill=0)
    _dot(d, cx, cy, r)
    ir = r - lw
    if ir > 1:
        itip = (cx, cy - r * 2.4 + lw * 2.2)
        d.polygon([itip, (cx - ir * 0.93, cy - ir * 0.35), (cx + ir * 0.93, cy - ir * 0.35)], fill=255)
        _dot(d, cx, cy, ir)
        d.ellipse((cx - ir, cy - ir, cx + ir, cy + ir), fill=255)


def _flake(d, cx, cy, r, lw) -> None:
    for k in range(3):
        a = math.pi / 2 + k * math.pi / 3
        dx, dy = r * math.cos(a), r * math.sin(a)
        _line(d, [(cx - dx, cy - dy), (cx + dx, cy + dy)], lw)


def _bolt(d, x, y, h, lw) -> None:
    """Outlined lightning bolt, top at (x, y), height h."""
    u = h / 10
    pts = [
        (x + 1.5 * u, y),
        (x + 5.5 * u, y),
        (x + 3.6 * u, y + 3.8 * u),
        (x + 6.4 * u, y + 3.8 * u),
        (x + 1.2 * u, y + 10 * u),
        (x + 2.6 * u, y + 5.4 * u),
        (x - 0.4 * u, y + 5.4 * u),
    ]
    d.polygon(pts, fill=255)
    d.line(pts + pts[:2], fill=0, width=round(lw), joint="curve")


def draw_weather(icon: str, px: int) -> Image.Image:
    name = normalize_icon(icon)
    img, d, s = _canvas(px)
    lw = s / 26
    if name == "sunny":
        _sun(d, s / 2, s / 2, s * 0.22, lw, ray_len=s * 0.13)
    elif name == "partly_cloudy":
        _sun(d, s * 0.36, s * 0.34, s * 0.17, lw, ray_len=s * 0.09)
        _cloud(d, s * 0.20, s * 0.40, s * 0.76, lw)
    elif name == "cloudy":
        _cloud(d, s * 0.30, s * 0.10, s * 0.60, lw)
        _cloud(d, s * 0.06, s * 0.36, s * 0.80, lw)
    elif name in ("rain", "snow", "storm"):
        base = _cloud(d, s * 0.08, s * 0.04, s * 0.84, lw)
        if name == "rain":
            for i, fx in enumerate((0.27, 0.50, 0.73)):
                _drop(d, s * fx, base + s * (0.24 if i % 2 == 0 else 0.34), s * 0.06, lw)
        elif name == "snow":
            for i, fx in enumerate((0.25, 0.50, 0.75)):
                _flake(d, s * fx, base + s * (0.16 if i % 2 == 0 else 0.28), s * 0.075, lw * 0.85)
        else:
            _bolt(d, s * 0.42, base - lw * 2, s * 0.50, lw)
            _drop(d, s * 0.24, base + s * 0.22, s * 0.05, lw)
            _drop(d, s * 0.78, base + s * 0.22, s * 0.05, lw)
    elif name == "fog":
        base = _cloud(d, s * 0.14, s * 0.04, s * 0.72, lw)
        for i, (a, b) in enumerate(((0.10, 0.80), (0.22, 0.92), (0.10, 0.74))):
            y = base + s * (0.12 + 0.12 * i)
            _line(d, [(s * a, y), (s * b, y)], lw)
    else:  # wind
        for y, length, r in ((0.30, 0.56, 0.10), (0.52, 0.68, 0.12), (0.74, 0.46, 0.09)):
            x0 = s * 0.10
            x1 = x0 + s * length
            cy = s * y - s * r
            _line(d, [(x0, s * y), (x1, s * y)], lw)
            # Curl: an arc that comes back over the line's end.
            _arc(d, x1, cy, s * r, 180, 450, lw)
    return _finish(img, px)
