import math

import pytest
from PIL import Image, ImageDraw

from zozobox.printer import (
    MOODS,
    WEATHER_ICONS,
    WIDTH_PX,
    normalize_icon,
    render_coloring_page,
    render_letter_card,
    render_mood_card,
    render_weather_card,
)
from zozobox.printer import render
from zozobox.printer.drawings import draw_face, draw_weather


def ink(img: Image.Image) -> float:
    """Fraction of black pixels."""
    g = img.convert("L")
    hist = g.histogram()
    return sum(hist[:128]) / (g.width * g.height)


def check_card(img: Image.Image, min_h=300, max_h=2400):
    assert img.mode == "1"
    assert img.width == WIDTH_PX
    assert min_h <= img.height <= max_h
    assert set(img.convert("L").tobytes()) <= {0, 255}
    assert 0.01 < ink(img) < 0.6
    # Blank paper margin at the top and bottom rows.
    g = img.convert("L")
    assert min(g.crop((0, 0, WIDTH_PX, 5)).tobytes()) == 255
    assert min(g.crop((0, img.height - 5, WIDTH_PX, img.height)).tobytes()) == 255


def sample_art(mode="RGB", size=(800, 600)):
    art = Image.new("RGB", size, (250, 235, 190))  # pale color fill
    d = ImageDraw.Draw(art)
    d.ellipse((100, 100, 700, 500), outline=(20, 20, 20), width=6, fill=(255, 170, 170))
    d.line((0, 0, size[0], size[1]), fill=(0, 0, 0), width=2)  # thin line
    if mode == "1":
        return art.convert("L").point(lambda v: 255 if v > 128 else 0, mode="1")
    return art.convert(mode)


# ---------------------------------------------------------------- letter ----


def test_letter_card_basic():
    img = render_letter_card("b", ["ball", "bear"], None)
    check_card(img)


def test_letter_card_with_drawing_is_taller():
    a = render_letter_card("B", ["ball"], None)
    b = render_letter_card("B", ["ball"], sample_art())
    check_card(b)
    assert b.height > a.height + 200


def test_letter_card_no_words():
    check_card(render_letter_card("z", [], None))


def test_letter_card_caps_words_and_skips_blank():
    many = render_letter_card("a", ["apple", "ant", "alligator", "axe", "arm"], None)
    three = render_letter_card("a", ["apple", "ant", "alligator"], None)
    assert many.height == three.height
    assert render_letter_card("a", ["", "  ", "apple"], None).height == render_letter_card("a", ["apple"], None).height


@pytest.mark.parametrize("letter", ["W", "m", "i"])
def test_letter_glyph_fits_width(letter):
    img = render_letter_card(letter, ["word"], None)
    # Wide letters must not be clipped: the outermost columns stay blank.
    g = img.convert("L")
    assert min(g.crop((0, 0, 4, img.height)).tobytes()) == 255
    assert min(g.crop((WIDTH_PX - 4, 0, WIDTH_PX, img.height)).tobytes()) == 255


@pytest.mark.parametrize("bad", ["", "ab", "1", " ", None, "?"])
def test_letter_card_rejects_bad_letter(bad):
    with pytest.raises(ValueError):
        render_letter_card(bad, ["x"], None)


def test_letter_is_hollow_for_coloring():
    # Outlined glyph: the card's top half has far less ink than a solid glyph would.
    img = render_letter_card("O", [], None)
    assert ink(img) < 0.25


def test_long_word_shrinks_to_fit():
    img = render_letter_card("s", ["supercalifragilisticexpialidocious"], None)
    check_card(img)


# --------------------------------------------------------------- weather ----


@pytest.mark.parametrize("icon", WEATHER_ICONS)
def test_weather_card_all_icons(icon):
    img = render_weather_card("Sunny and cold, wear your jacket", icon, 7.6, -2.2)
    check_card(img)


def test_weather_icons_are_distinct():
    pics = {name: draw_weather(name, 200).tobytes() for name in WEATHER_ICONS}
    assert len(set(pics.values())) == len(WEATHER_ICONS)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Sunny", "sunny"),
        ("partly-cloudy", "partly_cloudy"),
        ("Partly Cloudy", "partly_cloudy"),
        ("thunderstorm", "storm"),
        ("drizzle", "rain"),
        ("mist", "fog"),
        ("windy", "wind"),
        ("clear", "sunny"),
        ("tornado??", "cloudy"),
        ("", "cloudy"),
        (None, "cloudy"),
    ],
)
def test_normalize_icon(raw, expected):
    assert normalize_icon(raw) == expected


def test_weather_unknown_icon_still_renders():
    check_card(render_weather_card("Hmm", "volcano", 10, 5))


def test_weather_temps_rounding_and_nan():
    assert render._fmt_temp(7.6) == "8°C"
    assert render._fmt_temp(-2.2) == "-2°C"
    assert render._fmt_temp(-0.4) == "0°C"
    assert render._fmt_temp(float("nan")) == "--"
    check_card(render_weather_card("", "rain", float("nan"), math.inf))


def test_weather_long_summary_wraps():
    short = render_weather_card("Rain", "rain", 10, 5)
    long = render_weather_card("Rainy and windy all day long so put on your boots and your raincoat " * 2, "rain", 10, 5)
    check_card(long)
    assert long.height > short.height + 100


# ------------------------------------------------------------------ mood ----


@pytest.mark.parametrize("mood", MOODS)
def test_single_mood_card(mood):
    img = render_mood_card(mood)
    check_card(img)


def test_mood_case_insensitive():
    assert render_mood_card("Happy").tobytes() == render_mood_card("happy").tobytes()


def test_mood_none_is_grid_of_four():
    grid = render_mood_card(None)
    check_card(grid)
    single = render_mood_card("happy")
    assert grid.tobytes() != single.tobytes()
    # Ink appears in all four quadrants.
    g = grid.convert("L")
    w, h = g.size
    for qx in (0, 1):
        for qy in (0, 1):
            q = g.crop((qx * w // 2, qy * h // 2, (qx + 1) * w // 2, (qy + 1) * h // 2))
            assert ink(q) > 0.02


def test_unknown_mood_falls_back_to_grid():
    assert render_mood_card("unknown").tobytes() == render_mood_card(None).tobytes()


def test_faces_are_distinct_and_validated():
    faces = {m: draw_face(m, 200).tobytes() for m in MOODS}
    assert len(set(faces.values())) == 4
    with pytest.raises(ValueError):
        draw_face("silly", 200)


def test_face_lines_are_thick():
    # A horizontal scan through the middle of the face crosses the outline
    # with a run of several black pixels (thick enough to color along).
    face = render_mood_card("neutral").convert("L")
    row = [face.getpixel((x, 300)) for x in range(WIDTH_PX)]
    runs, cur = [], 0
    for v in row:
        if v < 128:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    assert runs and max(runs) >= 15


# -------------------------------------------------------------- coloring ----


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P", "1", "LA", "I"])
def test_coloring_page_accepts_any_mode(mode):
    img = render_coloring_page(sample_art(mode))
    check_card(img)


def test_coloring_page_scales_to_width():
    small = sample_art(size=(200, 150))
    img = render_coloring_page(small)
    check_card(img)
    # Art is scaled up to fill the inner width: ink reaches near both sides.
    g = img.convert("L")
    xs = [x for x in range(WIDTH_PX) if min(g.crop((x, 0, x + 1, img.height)).tobytes()) == 0]
    assert xs[0] < 60 and xs[-1] > WIDTH_PX - 60


def test_coloring_page_fills_drop_out_lines_stay():
    img = render_coloring_page(sample_art())
    # The pale and pink fills become white: far less ink than a solid image.
    assert ink(img) < 0.15
    # The ellipse outline survives.
    assert ink(img) > 0.01


def test_coloring_page_tall_art_is_capped():
    tall = sample_art(size=(300, 3000))
    img = render_coloring_page(tall)
    assert img.width == WIDTH_PX
    assert img.height <= render.MAX_ART_HEIGHT + 2 * render.TOP + 10


def test_coloring_page_transparent_background_is_white():
    art = Image.new("RGBA", (400, 400), (0, 0, 0, 0))  # transparent black
    ImageDraw.Draw(art).rectangle((50, 50, 350, 350), outline=(0, 0, 0, 255), width=8)
    img = render_coloring_page(art)
    check_card(img)
    assert ink(img) < 0.2


def test_coloring_page_no_dither_on_grey():
    # A flat mid-grey must not turn into a dither pattern: all one value.
    grey = Image.new("L", (300, 300), 200)
    ImageDraw.Draw(grey).line((0, 150, 300, 150), fill=0, width=6)
    out = render.line_art(grey)
    vals = set(out.tobytes())
    assert vals <= {0, 255}
    top = out.crop((0, 0, out.width, out.height // 3))
    assert set(top.tobytes()) == {255}


def test_thin_lines_are_thickened():
    art = Image.new("L", (1400, 700), 255)
    ImageDraw.Draw(art).line((0, 350, 1400, 350), fill=0, width=2)  # 2px, halved by scaling
    out = render.line_art(art, thicken=5)
    col = [out.getpixel((out.width // 2, y)) for y in range(out.height)]
    assert sum(1 for v in col if v == 0) >= 4


def test_coloring_page_title():
    a = render_coloring_page(sample_art())
    b = render_coloring_page(sample_art(), title="A big red balloon")
    check_card(b)
    assert b.height > a.height
    c = render_coloring_page(sample_art(), title="A very long title for a coloring page about dinosaurs in space")
    check_card(c)


def test_coloring_page_empty_art_rejected():
    with pytest.raises(ValueError):
        render_coloring_page(Image.new("RGB", (0, 0)))


# ----------------------------------------------------------------- fonts ----


def test_font_fallback_when_dejavu_missing(monkeypatch):
    monkeypatch.setattr(render, "FONT_PATHS", ("/nonexistent/x.ttf",))
    render.font.cache_clear()
    try:
        f = render.font(80)
        assert f is not None
        check_card(render_letter_card("c", ["cat"], None))
    finally:
        render.font.cache_clear()


def test_wrap_breaks_long_words():
    out = render.wrap("a" * 200, 54)
    assert len(out.split("\n")) > 1


def test_weather_non_numeric_temps_print_dashes():
    # Regression: a string/None/object temperature used to raise TypeError.
    assert render._fmt_temp("3") == "3°C"
    assert render._fmt_temp("warm") == "--"
    assert render._fmt_temp(None) == "--"
    assert render._fmt_temp(object()) == "--"
    check_card(render_weather_card("x", "sun", 8, "3"))
    check_card(render_weather_card("x", "sun", "hot", None))
