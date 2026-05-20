from pathlib import Path

from echo_repro.bug_spec import extract_bug_spec
from echo_repro.context_builder import build_concise_context
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.retriever import retrieve_context


def test_context_builder_contains_key_sections():
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    bug_spec = extract_bug_spec(issue_text, MockLLMClient())
    retrieved_context = retrieve_context(Path("examples/mock_buggy_repo"), bug_spec)
    concise_context = build_concise_context(issue_text, bug_spec, retrieved_context)

    assert "Bug Summary" in concise_context
    assert "Current Behavior" in concise_context
    assert "Expected Behavior" in concise_context
    assert "Failure Signature" in concise_context
    assert "Suspicious Source Context" in concise_context
    assert "Related Test / Fixture Context" in concise_context
    assert "Environment Context" in concise_context
    assert "Task Instruction" in concise_context

