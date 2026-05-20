import sys
import types

import pytest

from echo_repro.config import LLMSettings
from echo_repro.llm.openai_client import OpenAICompatibleLLMClient


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("response_format"):
            return _FakeResponse('{"title":"demo","summary":"ok","current_behavior":"bad","expected_behavior":"good","failure_signature":"sig","reproduction_hint":"hint","keywords":["demo"],"suspect_symbols":["divide"]}')
        return _FakeResponse("print('Issue reproduced')")


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeOpenAIClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = _FakeChat()


def test_openai_client_is_mockable(monkeypatch):
    fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAIClient)
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    settings = LLMSettings(
        llm_provider="openai",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="gpt-4o-mini",
        temperature=0.1,
        timeout_seconds=60,
    )
    client = OpenAICompatibleLLMClient(settings=settings)

    text = client.generate_text("say hello")
    payload = client.generate_json("return json")

    assert text == "print('Issue reproduced')"
    assert payload["title"] == "demo"
    assert client.client.kwargs["api_key"] == "test-key"
    assert client.client.kwargs["base_url"] == "https://example.invalid/v1"


def test_openai_client_requires_api_key():
    settings = LLMSettings(
        llm_provider="openai",
        api_key="",
        base_url="",
        model="gpt-4o-mini",
        temperature=0.2,
        timeout_seconds=60,
    )
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAICompatibleLLMClient(settings=settings, client=object())
