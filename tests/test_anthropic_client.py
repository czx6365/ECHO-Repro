import pytest

from echo_repro.config import LLMSettings
from echo_repro.llm.anthropic_client import AnthropicCompatibleLLMClient


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
    assert client.last_call_metadata.total_tokens == 7


def test_anthropic_client_requires_api_key():
    settings = LLMSettings(anthropic_api_key="")
    with pytest.raises(ValueError, match="ANTHROPIC_AUTH_TOKEN"):
        AnthropicCompatibleLLMClient(settings=settings, transport=lambda *args: {})
