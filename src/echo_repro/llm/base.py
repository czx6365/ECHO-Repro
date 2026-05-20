from __future__ import annotations

from abc import ABC, abstractmethod


class BaseLLMClient(ABC):
    @abstractmethod
    def extract_bug_spec(self, issue_text: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def generate_harness(self, concise_context: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def repair_harness(self, concise_context: str, current_code: str, feedback: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def strengthen_oracle(self, concise_context: str, current_code: str, feedback: str) -> str:
        raise NotImplementedError
