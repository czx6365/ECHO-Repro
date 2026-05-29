from pathlib import Path

import echo_repro.pipeline as pipeline_module
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.models import LLMCallMetadata
from echo_repro.pipeline import run_pipeline, run_pipeline_with_feedback_loop


def test_full_pipeline_succeeds_with_mock_llm():
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    result = run_pipeline(
        issue_text=issue_text,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        use_mock_llm=True,
    )

    assert result.validation.success is True
    assert result.buggy_execution.stdout.strip() == "Issue reproduced"
    assert result.fixed_execution is not None
    assert result.fixed_execution.stdout.strip() == "Issue resolved"


class ResolvedThenReproducedMockLLM(MockLLMClient):
    def __init__(self):
        self.generated = 0

    def generate_harness(self, concise_context: str) -> str:
        self.generated += 1
        self.last_prompt = f"candidate {self.generated}"
        self.last_call_metadata = LLMCallMetadata(provider="mock", model="candidate", total_tokens=1)
        if self.generated == 1:
            return 'print("Issue resolved")\n'
        return """
from buggy_module import divide

try:
    divide(10, 0)
except ZeroDivisionError:
    print("Issue resolved")
else:
    print("Issue reproduced")
""".strip()


def test_feedback_pipeline_selects_backup_harness_when_first_resolves_on_buggy(monkeypatch):
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    client = ResolvedThenReproducedMockLLM()

    monkeypatch.setattr(pipeline_module, "_make_llm_client", lambda use_mock_llm=True, llm_provider=None: client)

    result = run_pipeline_with_feedback_loop(
        issue_text=issue_text,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        use_mock_llm=True,
        max_attempts=1,
    )

    assert client.generated == 2
    assert result.validation.success is True
    assert result.initial_harness_prompt == "candidate 2"
