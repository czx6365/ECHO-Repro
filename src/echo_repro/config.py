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
    temperature: float = Field(
        default_factory=lambda: float(os.getenv("OPENAI_TEMPERATURE", os.getenv("ECHO_REPRO_LLM_TEMPERATURE", "0.2")))
    )
    timeout_seconds: int = Field(
        default_factory=lambda: int(os.getenv("ECHO_REPRO_LLM_TIMEOUT", "60"))
    )


def get_llm_settings() -> LLMSettings:
    return LLMSettings()
