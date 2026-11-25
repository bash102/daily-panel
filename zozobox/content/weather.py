"""Daily forecast from Open-Meteo (free, no key) and WMO code -> card icon.

Only latitude/longitude go to Open-Meteo. The location is never sent to
Claude: Claude only sees the condition and temperatures.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Protocol

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
WINDY_KMH = 40.0


class WeatherError(RuntimeError):
    pass


@dataclass(frozen=True)
class DayForecast:
    day: date
    code: int
    hi_c: float
    lo_c: float
    precip_prob: float | None = None
    wind_kmh: float | None = None

    @property
    def icon(self) -> str:
        return icon_for(self.code, self.wind_kmh)

    @property
    def condition(self) -> str:
        return CONDITION_WORDS.get(self.icon, "cloudy")


# Icons are the printer's WEATHER_ICONS names.
def icon_for(code: int, wind_kmh: float | None = None) -> str:
    """Map a WMO weather interpretation code to a card icon."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "cloudy"
    if c in (95, 96, 99):
        icon = "storm"
    elif c in (71, 73, 75, 77, 85, 86):
        icon = "snow"
    elif c in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        icon = "rain"
    elif c in (45, 48):
        icon = "fog"
    elif c == 3:
        icon = "cloudy"
    elif c == 2:
        icon = "partly_cloudy"
    elif c in (0, 1):
        icon = "sunny"
    else:
        icon = "cloudy"
    if icon in ("sunny", "partly_cloudy", "cloudy") and wind_kmh is not None \
            and wind_kmh >= WINDY_KMH:
        icon = "wind"
    return icon


CONDITION_WORDS = {
    "sunny": "sunny",
    "partly_cloudy": "a little cloudy",
    "cloudy": "cloudy",
    "rain": "rainy",
    "snow": "snowy",
    "storm": "rainy and rumbly",
    "fog": "foggy",
    "wind": "windy",
}


def feel_word(hi_c: float) -> str:
    if hi_c < 5:
        return "cold"
    if hi_c < 14:
        return "chilly"
    if hi_c < 24:
        return "nice and mild"
    return "warm"


# One representative high per feel_word band, and a file-name slug for it.
FEEL_BANDS: dict[str, float] = {"cold": 0.0, "chilly": 10.0, "mild": 18.0, "warm": 28.0}
TEMPLATE_ICONS = tuple(CONDITION_WORDS)


def feel_slug(hi_c: float) -> str:
    return {"nice and mild": "mild"}.get(feel_word(hi_c), feel_word(hi_c))


def _advice(icon: str, hi_c: float) -> str:
    if icon in ("rain", "storm"):
        return "wear your raincoat and boots"
    if icon == "snow":
        return "wear your warm coat, hat and mittens"
    if hi_c < 5:
        return "wear your warm coat and hat"
    if hi_c < 14:
        return "wear your jacket"
    if hi_c < 24:
        return "bring a sweater"
    return "wear your sun hat"


def clothing_advice(f: DayForecast) -> str:
    return _advice(f.icon, f.hi_c)


def template_text(icon: str, hi_c: float) -> tuple[str, str]:
    """(summary, sentence) for an icon and a high. Depends only on the icon
    and the feel band, so 8 icons x 4 bands cover every case (bootstrap
    pre-renders their audio so weather works at the spend cap)."""
    cond = CONDITION_WORDS.get(icon, "cloudy")
    feel = feel_word(hi_c)
    summary = f"{cond.capitalize()} and {feel}"
    sentence = f"Today it is {cond} and {feel}, so {_advice(icon, hi_c)}!"
    return summary, sentence


def template_weather(f: DayForecast) -> tuple[str, str]:
    """Pre-vetted wording used when Claude is unavailable: (summary, sentence)."""
    return template_text(f.icon, f.hi_c)


class WeatherAPI(Protocol):
    def forecast(self, lat: float, lon: float, day: date) -> DayForecast: ...


class OpenMeteo:
    def __init__(self, url: str = OPEN_METEO_URL, timeout_s: float = 20.0):
        self.url = url
        self.timeout_s = timeout_s

    def forecast(self, lat: float, lon: float, day: date) -> DayForecast:
        q = urllib.parse.urlencode({
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,wind_speed_10m_max",
            "timezone": "auto",
            "forecast_days": 3,
        })
        try:
            with urllib.request.urlopen(f"{self.url}?{q}", timeout=self.timeout_s) as r:
                body = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network, HTTP, JSON
            raise WeatherError(f"open-meteo: {type(e).__name__}: {e}") from None
        return parse_open_meteo(body, day)


def parse_open_meteo(body: dict, day: date) -> DayForecast:
    try:
        daily = body["daily"]
        days = [date.fromisoformat(d) for d in daily["time"]]
        i = days.index(day)
    except (KeyError, TypeError, ValueError) as e:
        raise WeatherError(f"open-meteo: no forecast for {day}: {e}") from None

    def val(key: str):
        try:
            v = daily[key][i]
        except (KeyError, IndexError, TypeError):
            return None
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    code, hi, lo = val("weather_code"), val("temperature_2m_max"), val("temperature_2m_min")
    if code is None or hi is None or lo is None:
        raise WeatherError(f"open-meteo: incomplete forecast for {day}")
    return DayForecast(day, int(code), hi, lo, val("precipitation_probability_max"),
                       val("wind_speed_10m_max"))


class FakeWeather:
    def __init__(self, code: int = 0, hi_c: float = 3.0, lo_c: float = -2.0,
                 wind_kmh: float | None = 10.0, error: Exception | None = None):
        self.code, self.hi_c, self.lo_c, self.wind_kmh = code, hi_c, lo_c, wind_kmh
        self.error = error
        self.calls: list[tuple[float, float, date]] = []

    def forecast(self, lat: float, lon: float, day: date) -> DayForecast:
        self.calls.append((lat, lon, day))
        if self.error is not None:
            raise self.error
        return DayForecast(day, self.code, self.hi_c, self.lo_c, None, self.wind_kmh)
