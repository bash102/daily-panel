import json
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from zozobox.mood import ClaudeMoodLabeler, ServiceUnavailable
from zozobox.mood.labeler import FALLBACK_BETA, LABELS, SYSTEM_PROMPT

REQ = httpx2.Request("POST", "https://api.anthropic.test/v1/messages")


def resp(label="happy", stop="end_turn", model="claude-opus-5-5", text=None, usage=True):
    content = [NS(type="thinking", thinking=""),
               NS(type="text", text=text if text is not None else json.dumps({"label": label}))]
    u = NS(input_tokens=300, output_tokens=50, cache_creation_input_tokens=None,
           cache_read_input_tokens=0) if usage else None
    return NS(stop_reason=stop, stop_details=None, content=content, model=model, usage=u)


class FakeAnthropic:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_request_shape_follows_api_rules():
    fake = FakeAnthropic(resp())
    lab = ClaudeMoodLabeler(client=fake)
    assert lab.label("I happy").label == "happy"
    [kw] = fake.calls
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["effort"] == "low"
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["properties"]["label"]["enum"] == list(LABELS)
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    for banned in ("temperature", "top_p", "top_k", "tool_choice", "tools"):
        assert banned not in kw
    assert "budget_tokens" not in json.dumps(kw)
    assert kw["messages"][-1]["role"] == "user"  # no assistant prefill
    assert "<transcript>\nI happy\n</transcript>" in kw["messages"][0]["content"]


def test_privacy_only_nickname_and_age():
    text = json.dumps(ClaudeMoodLabeler(client=FakeAnthropic(resp())).request_params("sad"))
    assert "Zozo" in SYSTEM_PROMPT and "2-3" in SYSTEM_PROMPT
    for secret in ("Zoriaz", "location", "audio/wav", "base64"):
        assert secret not in text


def test_model_from_env(monkeypatch):
    monkeypatch.setenv("ZOZOBOX_CLAUDE_MODEL", "claude-x")
    assert ClaudeMoodLabeler(client=object()).model == "claude-x"


def test_spend_from_usage_uses_served_model():
    out = ClaudeMoodLabeler(client=FakeAnthropic(resp(model="claude-opus-4-8"))).label("x")
    assert out.spend.provider == "anthropic"
    assert out.spend.model == "claude-opus-4-8"
    assert out.spend.usd == pytest.approx((300 * 5 + 50 * 25) / 1e6)


def test_refusal_is_unknown_but_billed():
    r = resp(stop="refusal")
    r.stop_details = NS(category="other", explanation="")
    out = ClaudeMoodLabeler(client=FakeAnthropic(r)).label("x")
    assert out.label == "unknown" and out.refused and out.spend is not None


@pytest.mark.parametrize("stop", ["max_tokens", "pause_turn", None])
def test_cut_off_answer_is_truncated_not_unknown(stop):
    """Regression: a cut-off answer must stay pending, not become a final unknown."""
    with pytest.raises(ServiceUnavailable) as ei:
        ClaudeMoodLabeler(client=FakeAnthropic(resp(stop=stop))).label("x")
    e = ei.value
    assert (e.provider, e.kind) == ("anthropic", "truncated") and e.per_take
    assert e.spend is not None and e.spend.usd > 0  # the call was billed


def test_max_tokens_leaves_room_for_thinking():
    lab = ClaudeMoodLabeler(client=object())
    assert lab.request_params("x")["max_tokens"] >= 16000
    assert "timeout" not in lab.request_params("x")
    assert lab.request_params("x", timeout_s=4.5)["timeout"] == 4.5


def test_timeout_override_reaches_sdk():
    fake = FakeAnthropic(resp())
    ClaudeMoodLabeler(client=fake).label("x", timeout_s=3.0)
    assert fake.calls[-1]["timeout"] == 3.0


def test_missing_credentials_typeerror_is_auth():
    """The real SDK raises TypeError at request time without a key."""
    client = anthropic.Anthropic(max_retries=0)  # ANTHROPIC_API_KEY removed by fixture
    with pytest.raises(ServiceUnavailable) as ei:
        ClaudeMoodLabeler(client=client).label("x")
    assert ei.value.kind == "auth" and ei.value.config_error


@pytest.mark.parametrize("text", ["not json", json.dumps({"label": "joyful"}),
                                  json.dumps(["happy"])])
def test_bad_output_unknown(text):
    assert ClaudeMoodLabeler(client=FakeAnthropic(resp(text=text))).label("x").label == "unknown"


def test_no_usage_no_spend():
    assert ClaudeMoodLabeler(client=FakeAnthropic(resp(usage=False))).label("x").spend is None


def _status(cls, code):
    return cls("boom", response=httpx2.Response(code, request=REQ), body=None)


@pytest.mark.parametrize("exc,kind", [
    (lambda: _status(anthropic.AuthenticationError, 401), "auth"),
    (lambda: _status(anthropic.PermissionDeniedError, 403), "auth"),
    (lambda: _status(anthropic.NotFoundError, 404), "not_found"),
    (lambda: _status(anthropic.BadRequestError, 400), "bad_request"),
    (lambda: _status(anthropic.RateLimitError, 429), "rate_limit"),
    (lambda: _status(anthropic.InternalServerError, 500), "server"),
    (lambda: anthropic.APITimeoutError(request=REQ), "timeout"),
    (lambda: anthropic.APIConnectionError(request=REQ), "network"),
    (lambda: RuntimeError("weird"), "unexpected"),
])
def test_errors_map_to_service_unavailable(exc, kind):
    with pytest.raises(ServiceUnavailable) as ei:
        ClaudeMoodLabeler(client=FakeAnthropic(exc())).label("x")
    assert ei.value.kind == kind and ei.value.provider == "anthropic"


def test_real_sdk_request_serialization():
    """Drive the real SDK over a mock transport: checks the SDK accepts our
    parameters and what goes on the wire (no network)."""
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "claude-opus-5-5", "stop_reason": "end_turn", "stop_sequence": None,
            "content": [{"type": "text", "text": "{\"label\": \"mad\"}"}],
            "usage": {"input_tokens": 200, "output_tokens": 20},
        })

    client = anthropic.Anthropic(
        api_key="test-key", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    out = ClaudeMoodLabeler(client=client).label("grr mad")
    assert out.label == "mad"
    body = seen["body"]
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "low"
    assert FALLBACK_BETA in seen["headers"]["anthropic-beta"]
    assert "temperature" not in body


def test_real_sdk_auth_error_maps_to_auth():
    client = anthropic.Anthropic(
        api_key="bad", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(
            lambda r: httpx2.Response(401, json={"type": "error", "error": {
                "type": "authentication_error", "message": "invalid x-api-key"}}))),
    )
    with pytest.raises(ServiceUnavailable) as ei:
        ClaudeMoodLabeler(client=client).label("x")
    assert ei.value.kind == "auth"
    assert "bad" not in str(ei.value)
