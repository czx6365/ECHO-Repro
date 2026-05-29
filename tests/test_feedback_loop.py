from pathlib import Path

from echo_repro.bug_spec import extract_bug_spec
from echo_repro.context_builder import build_concise_context
from echo_repro.feedback_loop import run_feedback_loop
from echo_repro.harness_generator import generate_harness
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.models import EnvironmentRepairResult, ExecutionResult
import echo_repro.feedback_loop as feedback_loop_module
from echo_repro.feedback_loop import _feedback_for_result
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


class ResolvedThenRepairMockLLM(MockLLMClient):
    def generate_harness(self, concise_context: str) -> str:
        return 'print("Issue resolved")\n'


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

    assert result.attempts[0].buggy_status == "harness_error"
    assert any(attempt.action == "repair_harness" for attempt in result.attempts[1:])
    assert result.validation.success is True


def test_dependency_error_classification_detected():
    result = ExecutionResult(
        repo_path=Path("examples/mock_buggy_repo"),
        command="python reproduce.py",
        returncode=1,
        stdout="",
        stderr="ModuleNotFoundError: No module named 'missing_module'",
        timed_out=False,
    )
    assert classify_execution(result) == "dependency_error"


def test_environment_error_classification_detected():
    result = ExecutionResult(
        repo_path=Path("examples/mock_buggy_repo"),
        command="python reproduce.py",
        returncode=1,
        stdout="",
        stderr="ImportError: cannot import name '_parse_times' from partially initialized module 'astropy.time'",
        timed_out=False,
    )
    assert classify_execution(result) == "environment_error"


class MissingDependencyMockLLM(MockLLMClient):
    def generate_harness(self, concise_context: str) -> str:
        return "import missing_module\n"


def test_feedback_loop_does_not_repair_dependency_errors():
    _, _, bug_spec, retrieved_context, concise_context = _build_context()
    llm_client = MissingDependencyMockLLM()
    initial_harness = generate_harness(concise_context, llm_client)

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

    assert len(result.attempts) == 1
    assert result.attempts[0].buggy_status == "dependency_error"
    assert result.validation.success is False


class FakeEnvironmentRepairManager:
    def __init__(self, python_path: Path):
        self.python_path = python_path

    def repair_dependency(self, result: ExecutionResult) -> EnvironmentRepairResult:
        return EnvironmentRepairResult(
            attempted=True,
            success=True,
            missing_module="missing_module",
            package="missing-module",
            env_path=self.python_path.parent.parent,
            python_path=self.python_path,
            install_command=[str(self.python_path), "-m", "pip", "install", "missing-module"],
            returncode=0,
            stdout="installed",
            reason="Dependency installed.",
        )


def test_feedback_loop_retries_after_successful_environment_repair(tmp_path: Path, monkeypatch):
    _, _, bug_spec, retrieved_context, concise_context = _build_context()
    llm_client = MissingDependencyMockLLM()
    initial_harness = generate_harness(concise_context, llm_client)
    repaired_python = tmp_path / "envs" / "demo" / "bin" / "python"

    def fake_run_harness(repo_path: Path, command: str | None = None, timeout: int = 30) -> ExecutionResult:
        assert command is not None
        if str(repaired_python) not in command:
            return ExecutionResult(
                repo_path=repo_path,
                command=command,
                returncode=1,
                stdout="",
                stderr="ModuleNotFoundError: No module named 'missing_module'",
            )
        stdout = "Issue resolved\n" if repo_path.name == "mock_fixed_repo" else "Issue reproduced\n"
        return ExecutionResult(
            repo_path=repo_path,
            command=command,
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(feedback_loop_module, "run_harness", fake_run_harness)

    result = run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=initial_harness,
        llm_client=llm_client,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        max_attempts=3,
        environment_repair_manager=FakeEnvironmentRepairManager(repaired_python),
    )

    assert result.attempts[0].buggy_status == "dependency_error"
    assert result.attempts[0].environment_repair is not None
    assert result.attempts[0].environment_repair.success is True
    assert result.attempts[1].action == "environment_repair"
    assert result.validation.success is True


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
    assert result.validation.buggy_status == "oracle_error"


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
    assert result.attempts[0].buggy_status == "harness_error"
    assert result.attempts[-1].fixed_status == "resolved"


def test_feedback_loop_repairs_when_buggy_repo_looks_resolved():
    _, llm_client, bug_spec, retrieved_context, concise_context = _build_context()
    resolved_client = ResolvedThenRepairMockLLM()
    initial_harness = generate_harness(concise_context, resolved_client)

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

    assert result.attempts[0].buggy_status == "resolved"
    assert result.attempts[1].action == "repair_harness"
    assert result.validation.success is True


def test_feedback_for_result_includes_fixed_execution_output():
    buggy = ExecutionResult(
        repo_path=Path("examples/mock_buggy_repo"),
        command="python reproduce.py",
        returncode=0,
        stdout="Issue reproduced\n",
        stderr="",
    )
    fixed = ExecutionResult(
        repo_path=Path("examples/mock_fixed_repo"),
        command="python reproduce.py",
        returncode=0,
        stdout="Issue reproduced\n",
        stderr="fixed details",
    )

    feedback = _feedback_for_result("reproduced", buggy, fixed_status="reproduced", fixed_result=fixed)

    assert "Fixed repo status: reproduced" in feedback
    assert "Fixed repo stdout:\nIssue reproduced" in feedback
    assert "Fixed repo stderr:\nfixed details" in feedback
