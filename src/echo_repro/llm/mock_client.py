from __future__ import annotations

import re

from echo_repro.llm.base import BaseLLMClient


class MockLLMClient(BaseLLMClient):
    def extract_bug_spec(self, issue_text: str) -> dict:
        title = issue_text.strip().splitlines()[0].replace("Title:", "").strip()
        symbol_match = re.search(r"Relevant symbol:\s*(.+)", issue_text, flags=re.IGNORECASE)
        symbol = symbol_match.group(1).strip() if symbol_match else "divide"
        return {
            "title": title or "Issue reproduction target",
            "summary": "Structured mock BugSpec extracted from issue text.",
            "current_behavior": _extract(issue_text, "Current behavior") or "Observed incorrect behavior.",
            "expected_behavior": _extract(issue_text, "Expected behavior") or "Expected correct behavior.",
            "failure_signature": _extract(issue_text, "Failure signature") or "Behavior mismatch detected.",
            "reproduction_hint": f"Call {symbol} with the reported inputs and inspect whether the expected exception or value occurs.",
            "keywords": [symbol, "zero", "division", "error"],
            "suspect_symbols": [symbol],
        }

    def generate_harness(self, concise_context: str) -> str:
        symbol = "divide" if "divide" in concise_context else "target_function"
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

    def repair_harness(self, concise_context: str, current_code: str, feedback: str) -> str:
        symbol = "divide" if "divide" in concise_context else "target_function"
        feedback_lower = feedback.lower()
        if "import" in feedback_lower:
            import_line = f"from buggy_module import {symbol}"
        else:
            import_line = f"from buggy_module import {symbol}"
        return f'''"""
WARNING: This generated harness is for local MVP evaluation only.
Production evaluation should use Docker or another sandbox boundary
before executing generated code against untrusted repositories.
"""

{import_line}


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

    def strengthen_oracle(self, concise_context: str, current_code: str, feedback: str) -> str:
        symbol = "divide" if "divide" in concise_context else "target_function"
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


def _extract(text: str, label: str) -> str:
    match = re.search(rf"{label}:\s*(.+?)(?=\n[A-Z][A-Za-z ]+:\s|\Z)", text, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(match.group(1).strip().split()) if match else ""
