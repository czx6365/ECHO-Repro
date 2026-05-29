from __future__ import annotations

import os
from pydantic import BaseModel, Field


class LLMSettings(BaseModel):
    llm_provider: str = Field(default_factory=lambda: os.getenv("ECHO_REPRO_LLM_PROVIDER", "mock"))
    api_key: str = Field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", os.getenv("ECHO_REPRO_LLM_API_KEY", ""))
    )
    base_url: str = Field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", os.getenv("ECHO_REPRO_LLM_BASE_URL", ""))
    )
    model: str = Field(
        default_factory=lambda: os.getenv("OPENAI_MODEL", os.getenv("ECHO_REPRO_LLM_MODEL", "gpt-4o-mini"))
    )
    anthropic_api_key: str = Field(
        default_factory=lambda: os.getenv(
            "ANTHROPIC_AUTH_TOKEN",
            os.getenv("ANTHROPIC_API_KEY", os.getenv("ECHO_REPRO_ANTHROPIC_API_KEY", "")),
        )
    )
    anthropic_base_url: str = Field(
        default_factory=lambda: os.getenv(
            "ANTHROPIC_BASE_URL",
            os.getenv("ECHO_REPRO_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        )
    )
    anthropic_model: str = Field(
        default_factory=lambda: os.getenv(
            "ANTHROPIC_MODEL",
            os.getenv(
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                os.getenv("ECHO_REPRO_ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
            ),
        )
    )
    temperature: float = Field(
        default_factory=lambda: float(os.getenv("OPENAI_TEMPERATURE", os.getenv("ECHO_REPRO_LLM_TEMPERATURE", "0.2")))
    )
    timeout_seconds: int = Field(
        default_factory=lambda: int(os.getenv("ECHO_REPRO_LLM_TIMEOUT", "60"))
    )
    max_tokens: int = Field(
        default_factory=lambda: int(os.getenv("ECHO_REPRO_LLM_MAX_TOKENS", "8192"))
    )


def get_llm_settings() -> LLMSettings:
    return LLMSettings()
