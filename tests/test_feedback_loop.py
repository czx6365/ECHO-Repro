from pathlib import Path

from echo_repro.bug_spec import extract_bug_spec
from echo_repro.context_builder import build_concise_context
from echo_repro.feedback_loop import run_feedback_loop
from echo_repro.harness_generator import generate_harness
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.models import ExecutionResult
from echo_repro.retriever import retrieve_context
from echo_repro.validator import classify_execution


def _build_context():
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    llm_client = MockLLMClient()
    bug_spec = extract_bug_spec(issue_text, llm_client)
    retrieved_context = retrieve_context(Path("examples/mock_buggy_repo"), bug_spec)
    concise_context = build_concise_context(issue_text, bug_spec, retrieved_context)
    return issue_text, llm_client, bug_spec, retrieved_context, concise_context


class SyntaxThenRepairMockLLM(MockLLMClient):
    def generate_harness(self, concise_context: str) -> str:
        return "def main(:\n    pass\n"


class AlwaysOtherMockLLM(MockLLMClient):
    def generate_harness(self, concise_context: str) -> str:
        return 'print("Other issues")\n'

    def strengthen_oracle(self, concise_context: str, current_code: str, feedback: str) -> str:
        return current_code


def test_syntax_error_repair_path_succeeds():
    _, llm_client, bug_spec, retrieved_context, concise_context = _build_context()
    initial_harness = generate_harness(concise_context, SyntaxThenRepairMockLLM())

    result = run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=initial_harness,
        llm_client=llm_client,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        max_attempts=3,
    )

    assert result.attempts[0].buggy_status == "syntax_error"
    assert any(attempt.action == "repair_harness" for attempt in result.attempts[1:])
    assert result.validation.success is True


def test_import_error_classification_detected():
    result = ExecutionResult(
        repo_path=Path("examples/mock_buggy_repo"),
        command="python reproduce.py",
        returncode=1,
        stdout="",
        stderr="ModuleNotFoundError: No module named 'missing_module'",
        timed_out=False,
    )
    assert classify_execution(result) == "import_error"


def test_feedback_loop_stops_at_max_attempts():
    _, _, bug_spec, retrieved_context, concise_context = _build_context()
    llm_client = AlwaysOtherMockLLM()
    initial_harness = generate_harness(concise_context, llm_client)

    result = run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=initial_harness,
        llm_client=llm_client,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        max_attempts=2,
    )

    assert len(result.attempts) == 2
    assert result.validation.success is False
    assert result.validation.buggy_status == "other"


def test_successful_fail_to_pass_after_repair():
    _, _, bug_spec, retrieved_context, concise_context = _build_context()
    loop_client = SyntaxThenRepairMockLLM()
    initial_harness = generate_harness(concise_context, loop_client)

    result = run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=initial_harness,
        llm_client=loop_client,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        max_attempts=3,
    )

    assert result.validation.success is True
    assert result.validation.buggy_status == "reproduced"
    assert result.validation.fixed_status == "resolved"
    assert result.attempts[0].buggy_status == "syntax_error"
    assert result.attempts[-1].fixed_status == "resolved"
