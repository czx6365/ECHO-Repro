from __future__ import annotations

from abc import ABC, abstractmethod

from echo_repro.prompts import (
    build_bug_spec_extraction_prompt,
    build_harness_generation_prompt,
    build_harness_repair_prompt,
    build_harness_strengthen_prompt,
)


class BaseLLMClient(ABC):
    last_prompt: str = ""
    last_call_metadata = None

    @abstractmethod
    def generate_text(self, prompt: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def generate_json(self, prompt: str) -> dict:
        raise NotImplementedError

    def extract_bug_spec(self, issue_text: str) -> dict:
        prompt = build_bug_spec_extraction_prompt(issue_text)
        self.last_prompt = prompt
        return self.generate_json(prompt)

    def generate_harness(self, concise_context: str) -> str:
        prompt = build_harness_generation_prompt(concise_context)
        self.last_prompt = prompt
        return self.generate_text(prompt)

    def repair_harness(self, concise_context: str, current_code: str, feedback: str) -> str:
        prompt = build_harness_repair_prompt(
            concise_context=concise_context,
            current_code=current_code,
            feedback=feedback,
        )
        self.last_prompt = prompt
        return self.generate_text(prompt)

    def strengthen_oracle(self, concise_context: str, current_code: str, feedback: str) -> str:
        prompt = build_harness_strengthen_prompt(
            concise_context=concise_context,
            current_code=current_code,
            feedback=feedback,
        )
        self.last_prompt = prompt
        return self.generate_text(prompt)
