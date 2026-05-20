from __future__ import annotations

import re

from echo_repro.llm.base import BaseLLMClient
from echo_repro.models import BugSpec


def _extract_section(text: str, label: str) -> str:
    pattern = rf"{label}:\s*(.+?)(?=\n[A-Z][A-Za-z ]+:\s|\Z)"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return " ".join(match.group(1).strip().split())
    return ""


def _fallback_bug_spec(issue_text: str) -> BugSpec:
    title = _extract_section(issue_text, "Title") or issue_text.strip().splitlines()[0].strip()
    current_behavior = _extract_section(issue_text, "Current behavior") or issue_text.strip()
    expected_behavior = _extract_section(issue_text, "Expected behavior") or "Behavior should match the issue expectations."
    failure_signature = _extract_section(issue_text, "Failure signature") or current_behavior
    relevant_symbol = _extract_section(issue_text, "Relevant symbol")
    suspect_symbols = [relevant_symbol] if relevant_symbol else []

    keyword_candidates = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", issue_text.lower())
    stop_words = {"the", "and", "when", "with", "that", "should", "current", "expected", "behavior", "title"}
    keywords = []
    for token in keyword_candidates:
        if token not in stop_words and token not in keywords:
            keywords.append(token)
    return BugSpec(
        title=title,
        summary=f"{title}. {failure_signature}".strip(),
        current_behavior=current_behavior,
        expected_behavior=expected_behavior,
        failure_signature=failure_signature,
        reproduction_hint="Create a minimal harness that exercises the suspected behavior directly.",
        keywords=keywords[:8],
        suspect_symbols=suspect_symbols,
    )


def extract_bug_spec(issue_text: str, llm_client: BaseLLMClient) -> BugSpec:
    llm_payload = llm_client.extract_bug_spec(issue_text)
    if llm_payload:
        merged = _fallback_bug_spec(issue_text).model_dump()
        merged.update({key: value for key, value in llm_payload.items() if value})
        return BugSpec(**merged)
    return _fallback_bug_spec(issue_text)

