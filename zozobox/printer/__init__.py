"""Printer (M3): toddler card rendering and the Brother QL-800 backend."""

from .backend import (
    DEFAULT_IDENTIFIER,
    BrotherQLPrinter,
    FakePrinter,
    Printer,
    PrinterError,
    QLConfig,
    prepare,
    send_instructions,
)
from .locked import (
    LockedPrinter,
    SupervisedPrinter,
    brother_factory,
    file_lock,
    lock_path,
    make_locked_brother,
)
from .drawings import MOODS, WEATHER_ICONS, normalize_icon
from .render import (
    WIDTH_PX,
    render_coloring_page,
    render_letter_card,
    render_mood_card,
    render_weather_card,
)

__all__ = [
    "WIDTH_PX",
    "MOODS",
    "WEATHER_ICONS",
    "normalize_icon",
    "render_letter_card",
    "render_weather_card",
    "render_mood_card",
    "render_coloring_page",
    "Printer",
    "PrinterError",
    "FakePrinter",
    "BrotherQLPrinter",
    "QLConfig",
    "DEFAULT_IDENTIFIER",
    "prepare",
    "send_instructions",
    "LockedPrinter",
    "SupervisedPrinter",
    "brother_factory",
    "file_lock",
    "lock_path",
    "make_locked_brother",
]
