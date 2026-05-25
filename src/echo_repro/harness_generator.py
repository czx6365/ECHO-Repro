from __future__ import annotations

from echo_repro.code_cleaner import clean_generated_python
from echo_repro.llm.base import BaseLLMClient
from echo_repro.models import HarnessCandidate


def generate_harness(concise_context: str, llm_client: BaseLLMClient) -> HarnessCandidate:
    code = clean_generated_python(llm_client.generate_harness(concise_context))
    return HarnessCandidate(
        filename="reproduce.py",
        code=code,
        rationale="Generated from concise context using the configured LLM client.",
    )
