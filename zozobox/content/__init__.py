"""Nightly content pipeline (SPEC 7.0/7.1, PLAN M6).

Generates the story, letter of the day, weather and coloring page (plus the
fixed phrases and evergreen fallbacks at bootstrap) into the store's content
cache, in the shape docs/ENGINE.md "Content contract for M6" describes.

    python -m zozobox.content {nightly,bootstrap,weather,status} --data-dir DIR

    from zozobox.content import Clients, run_nightly
    run_nightly(store, Clients.from_env(), clock)

Cloud clients sit behind small Protocols with Fake twins (`FakeClaude`,
`FakeMedia`, `FakeWeather`); the SDKs are imported lazily. Keys come only
from the environment (ANTHROPIC_API_KEY, OPENAI_API_KEY).
"""

from .claude import (
    AnthropicClaude, ClaudeAPI, ClaudeCall, ClaudeError, ClaudeRefusal, ClaudeResult,
    FakeClaude, default_fake_response,
)
from .openai_media import (
    FakeMedia, MediaAPI, MediaError, MediaResult, OpenAIMedia, TranscriptResult, tiny_png,
    tiny_wav,
)
from .pipeline import (
    CONTENT_ERROR, CONTENT_RUN, NIGHTLY_JOBS, SAFETY_FAILED, Clients, ContentConfig,
    GenerationError, JobCancelled, Pipeline, ReviewUnavailable, SafetyRejected, Story, Verdict, rechunk, run_bootstrap,
    run_nightly, run_weather, status,
)
from .spend import (
    CAP_EVENT, PRICES, SPEND_EVENT, SpendCapReached, SpendMeter, Usage,
)
from .weather import DayForecast, FakeWeather, OpenMeteo, WeatherAPI, WeatherError, icon_for

__all__ = [
    "AnthropicClaude", "ClaudeAPI", "ClaudeCall", "ClaudeError", "ClaudeRefusal", "ClaudeResult",
    "FakeClaude", "default_fake_response",
    "FakeMedia", "MediaAPI", "MediaError", "MediaResult", "OpenAIMedia", "TranscriptResult",
    "tiny_png", "tiny_wav",
    "CONTENT_ERROR", "CONTENT_RUN", "NIGHTLY_JOBS", "SAFETY_FAILED", "Clients", "ContentConfig",
    "GenerationError", "JobCancelled", "Pipeline", "ReviewUnavailable", "SafetyRejected", "Story", "Verdict", "rechunk",
    "run_bootstrap", "run_nightly", "run_weather", "status",
    "CAP_EVENT", "PRICES", "SPEND_EVENT", "SpendCapReached", "SpendMeter", "Usage",
    "DayForecast", "FakeWeather", "OpenMeteo", "WeatherAPI", "WeatherError", "icon_for",
]
