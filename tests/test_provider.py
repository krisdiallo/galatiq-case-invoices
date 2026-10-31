import io
import json
from urllib.error import HTTPError

import pytest

from invoices.providers import ChatProvider
from invoices.verification import Transcript


def response(message, finish="stop"):
    return io.BytesIO(
        json.dumps(
            {
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        ).encode()
    )


def test_unexpected_tool_call_is_rejected_without_dispatch(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "test-only")
    sent = []

    def send(request, timeout):
        body = json.loads(request.data)
        assert "tools" not in body
        sent.append(body)
        return response({"content": None, "tool_calls": [{"id": "unexpected"}]}, "tool_calls")

    monkeypatch.setattr("urllib.request.urlopen", send)
    provider = ChatProvider("test", api_key_env="TEST_MODEL_KEY")
    with pytest.raises(ValueError, match="Unexpected tool call"):
        provider.complete("Verify", {}, Transcript)
    assert len(sent) == len(provider.calls) == 1


def test_api_error_does_not_expose_body_or_secret(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "secret-value")

    def send(*args, **kwargs):
        raise HTTPError(
            "https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"secret-value")
        )

    monkeypatch.setattr("urllib.request.urlopen", send)
    with pytest.raises(RuntimeError, match="HTTP 401") as exc:
        ChatProvider("test", api_key_env="TEST_MODEL_KEY").complete("x", {}, Transcript)
    assert "secret-value" not in str(exc.value)


def test_call_budget_stops_before_network(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "test-only")
    with pytest.raises(ValueError, match="budget"):
        ChatProvider("test", api_key_env="TEST_MODEL_KEY", max_calls=0).complete(
            "x", {}, Transcript
        )


def test_truncated_model_output_rejected(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "test-only")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *args, **kwargs: response({"content": "{"}, "length")
    )
    with pytest.raises(ValueError, match="complete answer"):
        ChatProvider("test", api_key_env="TEST_MODEL_KEY").complete("x", {}, Transcript)


def test_anthropic_uses_schema_and_preserves_response_audit(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "test-only")
    sent = []

    def send(request, timeout):
        sent.append(json.loads(request.data))
        return response(
            {
                "content": json.dumps(
                    {"text": "source", "covered_regions": [], "unclear_regions": []}
                )
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", send)
    provider = ChatProvider("claude-test", "https://api.anthropic.com/v1", "TEST_MODEL_KEY")
    assert provider.complete("Transcribe", {}, Transcript).text == "source"
    assert sent[0]["response_format"]["type"] == "json_schema"
    assert sent[0]["response_format"]["json_schema"]["strict"] is True
    assert provider.calls[0]["finish_reason"] == "stop"
    assert "source" in provider.calls[0]["response"]["content"]


@pytest.mark.parametrize(
    "url", ["https://api.x.ai/v1", "https://api.openai.com/v1", "https://api.anthropic.com/v1"]
)
def test_no_output_token_cap_by_default(monkeypatch, url):
    monkeypatch.delenv("MODEL_OUTPUT_TOKENS", raising=False)
    monkeypatch.setenv("TEST_MODEL_KEY", "test-only")
    sent = []

    def send(request, timeout):
        sent.append(json.loads(request.data))
        return response(
            {"content": json.dumps({"text": "ok", "covered_regions": [], "unclear_regions": []})}
        )

    monkeypatch.setattr("urllib.request.urlopen", send)
    provider = ChatProvider("test", url, "TEST_MODEL_KEY")
    provider.complete("Transcribe", {}, Transcript)
    assert provider.output_tokens is None
    assert provider.request_policy["output_tokens"] is None
    assert "max_tokens" not in sent[0]
    assert "max_completion_tokens" not in sent[0]


def test_output_cap_is_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("MODEL_OUTPUT_TOKENS", "16000")
    assert ChatProvider("test").output_tokens == 16000
    monkeypatch.setenv("MODEL_OUTPUT_TOKENS", "")
    assert ChatProvider("test").output_tokens is None
    monkeypatch.setenv("MODEL_OUTPUT_TOKENS", "0")
    with pytest.raises(ValueError, match="Explicit output limit"):
        ChatProvider("test")
