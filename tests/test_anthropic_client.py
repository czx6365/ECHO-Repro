import pytest
import urllib.error

from echo_repro.config import LLMSettings
from echo_repro.llm.anthropic_client import AnthropicCompatibleLLMClient, _default_transport


def test_anthropic_client_calls_messages_endpoint():
    calls = []

    def fake_transport(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        text = '{"title":"demo","summary":"ok","current_behavior":"bad","expected_behavior":"good","failure_signature":"sig","reproduction_hint":"hint","keywords":["demo"],"suspect_symbols":["divide"]}'
        return {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }

    settings = LLMSettings(
        anthropic_api_key="test-key",
        anthropic_base_url="https://example.invalid/anthropic",
        anthropic_model="deepseek-v4-pro[1m]",
        temperature=0.1,
        timeout_seconds=30,
    )
    client = AnthropicCompatibleLLMClient(settings=settings, transport=fake_transport)

    payload = client.generate_json("return json")

    assert payload["title"] == "demo"
    assert calls[0][0] == "https://example.invalid/anthropic/v1/messages"
    assert calls[0][1]["x-api-key"] == "test-key"
    assert calls[0][2]["model"] == "deepseek-v4-pro[1m]"
    assert calls[0][2]["max_tokens"] == settings.max_tokens
    assert client.last_call_metadata.total_tokens == 7


def test_anthropic_client_retries_empty_text_response(monkeypatch):
    monkeypatch.setattr("echo_repro.llm.anthropic_client.time.sleep", lambda _: None)
    calls = []

    def fake_transport(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        if len(calls) == 1:
            return {"content": [], "stop_reason": "end_turn"}
        return {
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    settings = LLMSettings(
        anthropic_api_key="test-key",
        anthropic_base_url="https://example.invalid/anthropic",
        anthropic_model="deepseek-v4-pro[1m]",
    )
    client = AnthropicCompatibleLLMClient(settings=settings, transport=fake_transport)

    assert client.generate_text("say hello") == "hello"
    assert len(calls) == 2


def test_anthropic_client_requires_api_key():
    settings = LLMSettings(anthropic_api_key="")
    with pytest.raises(ValueError, match="ANTHROPIC_AUTH_TOKEN"):
        AnthropicCompatibleLLMClient(settings=settings, transport=lambda *args: {})


def test_default_transport_hung_urlopen_fails_fast_without_retry(monkeypatch):
    import time

    monkeypatch.setattr("echo_repro.llm.anthropic_client._HANG_GRACE_SECONDS", 1)
    calls = []

    def hung_urlopen(request, timeout):
        calls.append(True)
        time.sleep(600)
        raise AssertionError("unreachable")

    monkeypatch.setattr("echo_repro.llm.anthropic_client.urllib.request.urlopen", hung_urlopen)

    started = time.monotonic()
    with pytest.raises(ValueError, match="hung"):
        _default_transport("https://example.invalid/v1/messages", {}, {"model": "demo"}, 1)

    # 墙钟上限:1s 超时 + 1s 宽限,而不是等 600s 的假 urlopen 返回
    assert time.monotonic() - started < 5
    # 挂死直接失败,不进入重试循环
    assert calls == [True]


def test_default_transport_retries_transient_url_errors(monkeypatch):
    monkeypatch.setattr("echo_repro.llm.anthropic_client.time.sleep", lambda _: None)
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return b'{"content":[{"type":"text","text":"ok"}]}'

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            raise urllib.error.URLError("temporary disconnect")
        return FakeResponse()

    monkeypatch.setattr("echo_repro.llm.anthropic_client.urllib.request.urlopen", fake_urlopen)

    response = _default_transport(
        "https://example.invalid/v1/messages",
        {"content-type": "application/json"},
        {"model": "demo"},
        30,
    )

    assert response["content"][0]["text"] == "ok"
    assert len(calls) == 2
