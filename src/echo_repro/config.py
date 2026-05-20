from __future__ import annotations

import os
from pydantic import BaseModel, Field


class LLMSettings(BaseModel):
    base_url: str = Field(default_factory=lambda: os.getenv("ECHO_REPRO_LLM_BASE_URL", ""))
    api_key: str = Field(default_factory=lambda: os.getenv("ECHO_REPRO_LLM_API_KEY", ""))
    model: str = Field(default_factory=lambda: os.getenv("ECHO_REPRO_LLM_MODEL", "gpt-4o-mini"))
    timeout_seconds: int = Field(
        default_factory=lambda: int(os.getenv("ECHO_REPRO_LLM_TIMEOUT", "60"))
    )


def get_llm_settings() -> LLMSettings:
    return LLMSettings()

