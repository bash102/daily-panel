"""Real client wrappers driven by fake SDK objects: no network."""

from __future__ import annotations

import base64
import json
import wave
from datetime import date
from types import SimpleNamespace as NS

import pytest

from zozobox.content import (
    AnthropicClaude, ClaudeError, ClaudeRefusal, ContentConfig, MediaError, OpenAIMedia,
    tiny_png, tiny_wav,
)
from zozobox.content import weather as W
from zozobox.content.claude import FALLBACK_BETA, image_block


# ------------------------------------------------------------- anthropic ----


class FakeBetaMessages:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def create(self, **kw):
        self.kwargs = kw
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def fake_anthropic(result):
    msgs = FakeBetaMessages(result)
    return NS(beta=NS(messages=msgs)), msgs


def response(stop="end_turn", text='{"verdict": "pass", "reasons": []}', **kw):
    content = [NS(type="thinking", thinking=""), NS(type="text", text=text)]
    return NS(stop_reason=stop, content=content, model=kw.get("model", "claude-opus-5-5"),
              usage=NS(input_tokens=1000, output_tokens=2000, cache_creation_input_tokens=0,
                       cache_read_input_tokens=0),
              stop_details=kw.get("stop_details"))


def call(c, **kw):
    args = dict(purpose="story_review", system="sys", content="hello",
                schema={"type": "object"}, effort="low", max_tokens=100)
    args.update(kw)
    return c.generate_json(**args)


def test_anthropic_request_shape():
    pytest.importorskip("anthropic")
    client, msgs = fake_anthropic(response())
    res = call(AnthropicClaude(client=client))
    kw = msgs.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "low",
                                   "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["messages"] == [{"role": "user", "content": "hello"}]
    for banned in ("temperature", "top_p", "top_k", "tool_choice"):
        assert banned not in kw
    assert res.data == {"verdict": "pass", "reasons": []}
    assert res.usage.provider == "anthropic" and res.usage.input_units == 1000
    assert res.usage.usd == pytest.approx((1000 * 4 + 2000 * 20) / 1e6)


def test_anthropic_refusal_and_bad_stops():
    pytest.importorskip("anthropic")
    client, _ = fake_anthropic(response("refusal", text="",
                                        stop_details=NS(category="bio", explanation=None)))
    with pytest.raises(ClaudeRefusal) as ei:
        call(AnthropicClaude(client=client))
    assert ei.value.usage is not None and ei.value.usage.usd > 0
    client, _ = fake_anthropic(response("max_tokens"))
    with pytest.raises(ClaudeError, match="max_tokens"):
        call(AnthropicClaude(client=client))
    client, _ = fake_anthropic(response(text="not json"))
    with pytest.raises(ClaudeError, match="not JSON"):
        call(AnthropicClaude(client=client))
    client, _ = fake_anthropic(response(text="[1, 2]"))
    with pytest.raises(ClaudeError, match="not an object"):
        call(AnthropicClaude(client=client))


def test_anthropic_fallback_model_is_priced_as_served():
    pytest.importorskip("anthropic")
    client, _ = fake_anthropic(response(model="claude-opus-4-8"))
    res = call(AnthropicClaude(client=client))
    assert res.model == "claude-opus-4-8" and res.usage.model == "claude-opus-4-8"


def test_anthropic_fallbacks_can_be_disabled():
    pytest.importorskip("anthropic")
    client, msgs = fake_anthropic(response())
    call(AnthropicClaude(client=client, fallbacks=False))
    assert "fallbacks" not in msgs.kwargs and "betas" not in msgs.kwargs


def _anthropic_errors():
    anthropic = pytest.importorskip("anthropic")
    import httpx2

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def status(cls, code):
        return cls("msg", response=httpx2.Response(code, request=req), body=None)

    return [
        (status(anthropic.BadRequestError, 400), False),
        (status(anthropic.AuthenticationError, 401), False),
        (status(anthropic.PermissionDeniedError, 403), False),
        (status(anthropic.NotFoundError, 404), False),
        (status(anthropic.RateLimitError, 429), True),
        (status(anthropic.InternalServerError, 500), True),
        (anthropic.APITimeoutError(request=req), True),
        (anthropic.APIConnectionError(request=req), True),
    ]


def test_anthropic_sdk_errors_are_mapped():
    for err, retryable in _anthropic_errors():
        client, _ = fake_anthropic(err)
        with pytest.raises(ClaudeError) as ei:
            call(AnthropicClaude(client=client))
        assert ei.value.retryable is retryable, type(err)
        assert ei.value.usage is None


def test_image_block():
    b = image_block(b"abc")
    assert b["source"]["data"] == base64.b64encode(b"abc").decode()
    assert b["source"]["media_type"] == "image/png"


# --------------------------------------------------------------- openai ----


class FakeOpenAI:
    def __init__(self):
        self.speech_kw = self.image_kw = self.stt_kw = None
        self.image_result = NS(data=[NS(b64_json=base64.b64encode(tiny_png()).decode())],
                               usage=NS(input_tokens=50, output_tokens=1000))
        self.error = None
        outer = self

        class Speech:
            def create(self, **kw):
                outer.speech_kw = kw
                if outer.error:
                    raise outer.error
                return NS(read=lambda: tiny_wav())

        class Images:
            def generate(self, **kw):
                outer.image_kw = kw
                return outer.image_result

        class Transcriptions:
            def create(self, **kw):
                outer.stt_kw = kw
                return NS(text="  happy ")

        self.audio = NS(speech=Speech(), transcriptions=Transcriptions())
        self.images = Images()


def test_openai_tts():
    pytest.importorskip("openai")
    fake = FakeOpenAI()
    m = OpenAIMedia(client=fake, voice="sage")
    res = m.tts("Hello Zozo")
    assert res.data.startswith(b"RIFF") and res.ext == "wav"
    kw = fake.speech_kw
    assert kw["model"] == "gpt-4o-mini-tts" and kw["voice"] == "sage"
    assert kw["input"] == "Hello Zozo" and kw["response_format"] == "wav"
    assert "gently" in kw["instructions"]
    assert res.usage.provider == "openai" and res.usage.input_units == len("Hello Zozo")


def test_openai_image():
    pytest.importorskip("openai")
    fake = FakeOpenAI()
    res = OpenAIMedia(client=fake).image("a cat")
    assert res.data == tiny_png()
    assert fake.image_kw["model"] == "gpt-image-2" and fake.image_kw["output_format"] == "png"
    assert res.usage.usd == pytest.approx((50 * 5 + 1000 * 40) / 1e6)
    fake.image_result = NS(data=[NS(b64_json=None)], usage=None)
    with pytest.raises(MediaError) as ei:
        OpenAIMedia(client=fake).image("a cat")
    assert ei.value.usage is not None  # billed anyway -> still logged


def test_openai_transcribe(tmp_path):
    pytest.importorskip("openai")
    fake = FakeOpenAI()
    p = tmp_path / "a.wav"
    p.write_bytes(tiny_wav(2.0))
    res = OpenAIMedia(client=fake).transcribe(p)
    assert res.text == "happy" and fake.stt_kw["model"] == "gpt-4o-mini-transcribe"
    assert res.usage.input_units == 2


def test_openai_errors_are_mapped():
    openai = pytest.importorskip("openai")
    import httpx2

    req = httpx2.Request("POST", "https://api.openai.com/v1/audio/speech")
    fake = FakeOpenAI()
    fake.error = openai.AuthenticationError("bad", response=httpx2.Response(401, request=req),
                                            body=None)
    with pytest.raises(MediaError) as ei:
        OpenAIMedia(client=fake).tts("x")
    assert ei.value.retryable is False and "OPENAI_API_KEY" in str(ei.value)
    fake.error = openai.APITimeoutError(request=req)
    with pytest.raises(MediaError, match="timed out"):
        OpenAIMedia(client=fake).tts("x")


# --------------------------------------------------------------- config ----


def test_config_from_env():
    c = ContentConfig.from_env({"ZOZOBOX_IMAGE_MODEL": "img-x", "ZOZOBOX_TTS_VOICE": "nova",
                                "ZOZOBOX_CONTENT_JOB_TIMEOUT_S": "60", "OTHER": "1"})
    assert c.image_model == "img-x" and c.voice == "nova" and c.job_timeout_s == 60
    assert c.claude_model == "claude-opus-5-5" and c.tts_model == "gpt-4o-mini-tts"
    assert ContentConfig.from_env({"ZOZOBOX_CONTENT_JOB_TIMEOUT_S": "x"}).job_timeout_s == 900


# -------------------------------------------------------------- weather ----


@pytest.mark.parametrize("code,wind,icon", [
    (0, 5, "sunny"), (1, None, "sunny"), (2, 5, "partly_cloudy"), (3, 5, "cloudy"),
    (45, 5, "fog"), (53, 5, "rain"), (65, 50, "rain"), (81, 5, "rain"), (73, 5, "snow"),
    (86, 5, "snow"), (95, 5, "storm"), (99, 5, "storm"), (0, 45, "wind"), (3, 40, "wind"),
    (123, 5, "cloudy"),
])
def test_wmo_icons(code, wind, icon):
    from zozobox.printer import WEATHER_ICONS

    assert W.icon_for(code, wind) == icon and icon in WEATHER_ICONS


def _om_body():
    return {"daily": {"time": ["2026-03-10", "2026-03-11"], "weather_code": [61, 0],
                      "temperature_2m_max": [8.5, 12], "temperature_2m_min": [1.0, 3],
                      "precipitation_probability_max": [80, 0],
                      "wind_speed_10m_max": [12.0, 8.0]}}


def test_parse_open_meteo():
    f = W.parse_open_meteo(_om_body(), date(2026, 3, 10))
    assert (f.code, f.hi_c, f.lo_c, f.precip_prob, f.icon) == (61, 8.5, 1.0, 80.0, "rain")
    with pytest.raises(W.WeatherError):
        W.parse_open_meteo(_om_body(), date(2026, 3, 20))
    with pytest.raises(W.WeatherError):
        W.parse_open_meteo({"nope": 1}, date(2026, 3, 10))
    b = _om_body()
    b["daily"]["weather_code"][0] = None
    with pytest.raises(W.WeatherError):
        W.parse_open_meteo(b, date(2026, 3, 10))


def test_open_meteo_request(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(_om_body()).encode()

    def urlopen(url, timeout):
        seen["url"], seen["timeout"] = url, timeout
        return Resp()

    monkeypatch.setattr(W.urllib.request, "urlopen", urlopen)
    f = W.OpenMeteo(timeout_s=7).forecast(40.7128, -74.006, date(2026, 3, 11))
    assert f.icon == "sunny" and seen["timeout"] == 7
    assert "latitude=40.7128" in seen["url"] and "weather_code" in seen["url"]

    def boom(url, timeout):
        raise OSError("offline")

    monkeypatch.setattr(W.urllib.request, "urlopen", boom)
    with pytest.raises(W.WeatherError, match="offline"):
        W.OpenMeteo().forecast(1, 2, date(2026, 3, 11))


def test_template_weather():
    f = W.DayForecast(date(2026, 3, 10), 61, 8, 2)
    assert W.template_weather(f) == ("Rainy and chilly",
                                     "Today it is rainy and chilly, so wear your raincoat and boots!")
    f = W.DayForecast(date(2026, 3, 10), 0, 28, 18)
    assert W.template_weather(f)[1] == "Today it is sunny and warm, so wear your sun hat!"


def test_tiny_wav_is_valid(tmp_path):
    p = tmp_path / "x.wav"
    p.write_bytes(tiny_wav(0.5))
    with wave.open(str(p)) as w:
        assert w.getnframes() == 8000
