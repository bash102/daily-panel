import json
from types import SimpleNamespace as NS

import httpx2
import openai
import pytest

from zozobox.mood import OpenAITranscriber, ServiceUnavailable
from zozobox.mood.stt import DEFAULT_STT_MODEL, wav_seconds

REQ = httpx2.Request("POST", "https://api.openai.test/v1/audio/transcriptions")


class FakeOpenAI:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.audio = NS(transcriptions=NS(create=self._create))

    def _create(self, **kw):
        kw["file_bytes"] = kw["file"].read()
        self.calls.append(kw)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_transcribe_sends_clip_with_short_timeout(wav):
    fake = FakeOpenAI(NS(text="  I happy ", usage=NS(type="duration", seconds=2.0)))
    tr = OpenAITranscriber(client=fake, timeout_s=5).transcribe(wav)
    assert tr.text == "I happy"
    [kw] = fake.calls
    assert kw["model"] == DEFAULT_STT_MODEL == "gpt-4o-mini-transcribe"
    assert kw["timeout"] == 5
    assert kw["file_bytes"] == wav.read_bytes()
    assert "Zozo" not in kw["prompt"] and "Zoriaz" not in kw["prompt"]
    assert tr.spend.provider == "openai" and tr.spend.unit == "seconds"
    assert tr.spend.usd == pytest.approx(2 / 60 * 0.003)


def test_token_usage(wav):
    fake = FakeOpenAI(NS(text="sad", usage=NS(type="tokens", input_tokens=100, output_tokens=5)))
    tr = OpenAITranscriber(client=fake).transcribe(wav)
    assert tr.spend.unit == "tokens"
    assert (tr.spend.input_units, tr.spend.output_units) == (100, 5)


def test_missing_usage_estimates_from_wav(wav):
    tr = OpenAITranscriber(client=FakeOpenAI(NS(text="ok", usage=None))).transcribe(wav)
    assert tr.spend.input_units == pytest.approx(2.0)


def test_wav_seconds_bad_file(tmp_path):
    p = tmp_path / "x.wav"
    p.write_bytes(b"junk")
    assert wav_seconds(p) == 0.0


def test_model_env(monkeypatch):
    monkeypatch.setenv("ZOZOBOX_STT_MODEL", "gpt-x")
    assert OpenAITranscriber(client=object()).model == "gpt-x"


def _status(cls, code):
    return cls("boom", response=httpx2.Response(code, request=REQ), body=None)


@pytest.mark.parametrize("exc,kind", [
    (lambda: _status(openai.AuthenticationError, 401), "auth"),
    (lambda: _status(openai.PermissionDeniedError, 403), "auth"),
    (lambda: _status(openai.NotFoundError, 404), "not_found"),
    (lambda: _status(openai.BadRequestError, 400), "bad_request"),
    (lambda: _status(openai.RateLimitError, 429), "rate_limit"),
    (lambda: _status(openai.InternalServerError, 500), "server"),
    (lambda: openai.APITimeoutError(request=REQ), "timeout"),
    (lambda: openai.APIConnectionError(request=REQ), "network"),
    (lambda: ValueError("x"), "unexpected"),
])
def test_errors(wav, exc, kind):
    with pytest.raises(ServiceUnavailable) as ei:
        OpenAITranscriber(client=FakeOpenAI(exc())).transcribe(wav)
    assert ei.value.kind == kind


def test_unreadable_file(tmp_path):
    with pytest.raises(ServiceUnavailable):
        OpenAITranscriber(client=FakeOpenAI(NS(text="x"))).transcribe(tmp_path / "nope.wav")


def test_no_api_key_is_auth(wav):
    # OPENAI_API_KEY is removed by the autouse fixture.
    with pytest.raises(ServiceUnavailable) as ei:
        OpenAITranscriber().transcribe(wav)
    assert ei.value.kind == "auth"


def test_real_sdk_over_mock_transport(wav):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx2.Response(200, json={
            "text": "mad!", "usage": {"type": "duration", "seconds": 2}})

    client = openai.OpenAI(api_key="test-key", max_retries=0,
                           http_client=openai.DefaultHttpxClient(
                               transport=httpx2.MockTransport(handler)))
    tr = OpenAITranscriber(client=client).transcribe(wav)
    assert tr.text == "mad!"
    assert seen["url"].endswith("/audio/transcriptions")
    assert b"gpt-4o-mini-transcribe" in seen["body"]
    assert tr.spend.input_units == 2
