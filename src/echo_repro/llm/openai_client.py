from __future__ import annotations

import json
import time
from typing import Any

from echo_repro.config import LLMSettings, get_llm_settings
from echo_repro.llm.base import BaseLLMClient
from echo_repro.models import LLMCallMetadata


class OpenAICompatibleLLMClient(BaseLLMClient):
    def __init__(self, settings: LLMSettings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_llm_settings()
        if not self.settings.api_key:
            raise ValueError("OPENAI_API_KEY is required when using --llm openai.")

        if client is not None:
            self.client = client
            return

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ValueError(
                "The openai package is required for --llm openai. Install dependencies from pyproject.toml."
            ) from exc

        client_kwargs: dict[str, Any] = {"api_key": self.settings.api_key}
        if self.settings.base_url:
            client_kwargs["base_url"] = self.settings.base_url
        self.client = OpenAI(**client_kwargs)

    def generate_text(self, prompt: str) -> str:
        started = time.perf_counter()
        response = self.client.chat.completions.create(
            model=self.settings.model,
            temperature=self.settings.temperature,
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise software engineering assistant. Follow the user's output format exactly.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("OpenAI-compatible model returned empty text content.")
        self.last_call_metadata = self._build_metadata(response, started)
        return content.strip()

    def generate_json(self, prompt: str) -> dict:
        started = time.perf_counter()
        response = self.client.chat.completions.create(
            model=self.settings.model,
            temperature=self.settings.temperature,
            messages=[
                {
                    "role": "system",
                    "content": "You return only valid JSON objects with no markdown fences.",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("OpenAI-compatible model returned empty JSON content.")
        self.last_call_metadata = self._build_metadata(response, started)
        return json.loads(content)

    def _build_metadata(self, response: Any, started: float) -> LLMCallMetadata:
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        raw_usage = {}
        if usage is not None:
            raw_usage = {
                key: value
                for key, value in {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }.items()
                if value is not None
            }
        return LLMCallMetadata(
            provider="openai",
            model=self.settings.model,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            total_tokens=total_tokens,
            raw_usage=raw_usage,
        )
