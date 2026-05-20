from echo_repro.bug_spec import extract_bug_spec
from echo_repro.llm.mock_client import MockLLMClient


def test_extract_bug_spec_returns_non_empty_fields():
    issue_text = """
Title: divide(a, b) should raise ZeroDivisionError when b is zero

Current behavior:
Calling divide(10, 0) returns 0.

Expected behavior:
Calling divide(10, 0) should raise ZeroDivisionError.

Failure signature:
The function silently swallows division by zero.

Relevant symbol:
divide
"""
    bug_spec = extract_bug_spec(issue_text, MockLLMClient())
    assert bug_spec.title
    assert bug_spec.current_behavior
    assert bug_spec.expected_behavior
    assert bug_spec.failure_signature
    assert "divide" in bug_spec.suspect_symbols

