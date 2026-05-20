from __future__ import annotations

import re
import time

from echo_repro.llm.base import BaseLLMClient
from echo_repro.models import LLMCallMetadata
from echo_repro.prompts import (
    build_bug_spec_extraction_prompt,
    build_harness_generation_prompt,
    build_harness_repair_prompt,
    build_harness_strengthen_prompt,
)


class MockLLMClient(BaseLLMClient):
    def generate_text(self, prompt: str) -> str:
        started = time.perf_counter()
        if "strengthen its oracle" in prompt.lower():
            content = _mock_strengthen_code(prompt)
        elif "repair the following python bug reproduction harness" in prompt.lower():
            content = _mock_repair_code(prompt)
        else:
            content = _mock_generate_code(prompt)
        self.last_call_metadata = _fake_metadata("mock", "mock-llm", prompt, content, started)
        return content

    def generate_json(self, prompt: str) -> dict:
        started = time.perf_counter()
        issue_text = _extract_issue_text_from_prompt(prompt)
        content = self.extract_bug_spec(issue_text)
        self.last_call_metadata = _fake_metadata("mock", "mock-llm", prompt, str(content), started)
        return content

    def extract_bug_spec(self, issue_text: str) -> dict:
        self.last_prompt = build_bug_spec_extraction_prompt(issue_text)
        title = issue_text.strip().splitlines()[0].replace("Title:", "").strip()
        symbol_match = re.search(r"Relevant symbol:\s*(.+)", issue_text, flags=re.IGNORECASE)
        symbol = symbol_match.group(1).strip() if symbol_match else "divide"
        payload = {
            "title": title or "Issue reproduction target",
            "summary": "Structured mock BugSpec extracted from issue text.",
            "current_behavior": _extract(issue_text, "Current behavior") or "Observed incorrect behavior.",
            "expected_behavior": _extract(issue_text, "Expected behavior") or "Expected correct behavior.",
            "failure_signature": _extract(issue_text, "Failure signature") or "Behavior mismatch detected.",
            "reproduction_hint": f"Call {symbol} with the reported inputs and inspect whether the expected exception or value occurs.",
            "keywords": [symbol, "zero", "division", "error"],
            "suspect_symbols": [symbol],
        }
        self.last_call_metadata = _fake_metadata("mock", "mock-llm", self.last_prompt, str(payload), time.perf_counter())
        return payload

    def generate_harness(self, concise_context: str) -> str:
        self.last_prompt = build_harness_generation_prompt(concise_context)
        started = time.perf_counter()
        content = _mock_generate_code(concise_context)
        self.last_call_metadata = _fake_metadata("mock", "mock-llm", self.last_prompt, content, started)
        return content

    def repair_harness(self, concise_context: str, current_code: str, feedback: str) -> str:
        self.last_prompt = build_harness_repair_prompt(concise_context, current_code, feedback)
        started = time.perf_counter()
        content = _mock_repair_code(concise_context)
        self.last_call_metadata = _fake_metadata("mock", "mock-llm", self.last_prompt, content, started)
        return content

    def strengthen_oracle(self, concise_context: str, current_code: str, feedback: str) -> str:
        self.last_prompt = build_harness_strengthen_prompt(concise_context, current_code, feedback)
        started = time.perf_counter()
        content = _mock_strengthen_code(concise_context)
        self.last_call_metadata = _fake_metadata("mock", "mock-llm", self.last_prompt, content, started)
        return content


def _extract(text: str, label: str) -> str:
    match = re.search(rf"{label}:\s*(.+?)(?=\n[A-Z][A-Za-z ]+:\s|\Z)", text, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(match.group(1).strip().split()) if match else ""


def _extract_issue_text_from_prompt(prompt: str) -> str:
    marker = "Issue text:\n"
    if marker in prompt:
        return prompt.split(marker, maxsplit=1)[1].strip()
    return prompt


def _fake_metadata(provider: str, model: str, prompt: str, output: str, started: float) -> LLMCallMetadata:
    input_tokens = max(1, len(prompt) // 4)
    output_tokens = max(1, len(output) // 4)
    return LLMCallMetadata(
        provider=provider,
        model=model,
        latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        raw_usage={
            "input_chars": len(prompt),
            "output_chars": len(output),
        },
    )


def _mock_generate_code(context_or_prompt: str) -> str:
    symbol = "divide" if "divide" in context_or_prompt else "target_function"
    return f'''"""
WARNING: This generated harness is for local MVP evaluation only.
Production evaluation should use Docker or another sandbox boundary
before executing generated code against untrusted repositories.
"""

from buggy_module import {symbol}


def main() -> None:
    try:
        {symbol}(10, 0)
    except ZeroDivisionError:
        print("Issue resolved")
        return
    except Exception:
        print("Other issues")
        return
    print("Issue reproduced")


if __name__ == "__main__":
    main()
'''


def _mock_repair_code(context_or_prompt: str) -> str:
    return _mock_generate_code(context_or_prompt)


def _mock_strengthen_code(context_or_prompt: str) -> str:
    symbol = "divide" if "divide" in context_or_prompt else "target_function"
    return f'''"""
WARNING: This generated harness is for local MVP evaluation only.
Production evaluation should use Docker or another sandbox boundary
before executing generated code against untrusted repositories.
"""

from buggy_module import {symbol}


def main() -> None:
    try:
        result = {symbol}(10, 0)
    except ZeroDivisionError:
        print("Issue resolved")
        return
    except Exception:
        print("Other issues")
        return

    if result == 0:
        print("Issue reproduced")
        return
    print("Other issues")


if __name__ == "__main__":
    main()
'''
