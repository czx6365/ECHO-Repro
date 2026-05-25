from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from echo_repro.config import LLMSettings, get_llm_settings
from echo_repro.llm.base import BaseLLMClient
from echo_repro.models import LLMCallMetadata

Transport = Callable[[str, dict[str, str], dict[str, Any], int], dict[str, Any]]


def _messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def _default_transport(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc


class AnthropicCompatibleLLMClient(BaseLLMClient):
    def __init__(self, settings: LLMSettings | None = None, transport: Transport | None = None) -> None:
        self.settings = settings or get_llm_settings()
        self.model_name = self.settings.anthropic_model
        self.temperature = self.settings.temperature
        if not self.settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY is required when using --llm anthropic.")
        self.transport = transport or _default_transport

    def generate_text(self, prompt: str) -> str:
        response = self._create_message(prompt, system="You are a precise software engineering assistant.")
        content = self._extract_text(response)
        if not content:
            raise ValueError("Anthropic-compatible model returned empty text content.")
        return content.strip()

    def generate_json(self, prompt: str) -> dict:
        response = self._create_message(
            prompt,
            system="You return only valid JSON objects with no markdown fences.",
        )
        content = self._extract_text(response)
        if not content:
            raise ValueError("Anthropic-compatible model returned empty JSON content.")
        return json.loads(content.strip())

    def _create_message(self, prompt: str, system: str) -> dict[str, Any]:
        started = time.perf_counter()
        payload = {
            "model": self.settings.anthropic_model,
            "max_tokens": 4096,
            "temperature": self.settings.temperature,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        }
        response = self.transport(
            _messages_url(self.settings.anthropic_base_url),
            headers,
            payload,
            self.settings.timeout_seconds,
        )
        self.last_call_metadata = self._build_metadata(response, started)
        return response

    def _extract_text(self, response: dict[str, Any]) -> str:
        parts = response.get("content") or []
        texts = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(str(part.get("text") or ""))
        return "\n".join(texts)

    def _build_metadata(self, response: dict[str, Any], started: float) -> LLMCallMetadata:
        usage = response.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = None
        if input_tokens is not None and output_tokens is not None:
            total_tokens = int(input_tokens) + int(output_tokens)
        return LLMCallMetadata(
            provider="anthropic",
            model=self.settings.anthropic_model,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            raw_usage=usage,
        )
