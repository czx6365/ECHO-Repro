"""Offline tests for the P0 evaluation protocol (patch_blind / patch_aware)
and the strict execution classification.

No test here needs an API key, network access, or a real model. The feedback
loop tests monkeypatch run_harness/write_harness so no subprocess is spawned
and the example repos are never mutated.
"""

import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from typer.testing import CliRunner

import echo_repro.feedback_loop as feedback_loop_module
import echo_repro.pipeline as pipeline_module
from echo_repro.bug_spec import extract_bug_spec
from echo_repro.cli import app
from echo_repro.context_builder import build_concise_context
from echo_repro.feedback_loop import run_feedback_loop
from echo_repro.harness_generator import generate_harness
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.models import ExecutionResult, HarnessCandidate, LLMCallMetadata, PreparedRepos
from echo_repro.pipeline import run_pipeline, run_pipeline_with_feedback_loop
from echo_repro.result_writer import write_experiment_record
from echo_repro.retriever import retrieve_context
from echo_repro.validator import classify_execution, validate_fail_to_pass


def _result(stdout: str = "", stderr: str = "", returncode: int | None = 0, timed_out: bool = False) -> ExecutionResult:
    return ExecutionResult(
        repo_path=Path("examples/mock_buggy_repo"),
        command="python reproduce.py",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# Strict classification
# ---------------------------------------------------------------------------


def test_strict_marker_with_nonzero_returncode_is_not_reproduced():
    result = _result(stdout="Issue reproduced\n", returncode=1)
    assert classify_execution(result, strict=True) != "reproduced"
    assert classify_execution(result, strict=True) == "harness_error"
    # the refactored classifier applies the same precedence in legacy mode
    assert classify_execution(result) == "harness_error"


def test_none_returncode_with_marker_is_not_reproduced():
    # timed_out=False with returncode=None (e.g. a malformed ExecutionResult)
    # must never be accepted as reproduced/resolved.
    result = _result(stdout="Issue reproduced\n", returncode=None, timed_out=False)
    assert classify_execution(result, strict=True) == "harness_error"
    # legacy keeps only the marker-line tolerance; a missing return code is
    # still not a success
    assert classify_execution(result) == "harness_error"
    assert validate_fail_to_pass(result, strict=True).success is False


def test_timeout_with_none_returncode_still_classifies_as_timeout():
    result = _result(stdout="Issue reproduced\n", returncode=None, timed_out=True)
    assert classify_execution(result, strict=True) == "timeout"
    assert classify_execution(result) == "timeout"


def test_strict_marker_with_stderr_module_not_found_is_dependency_error():
    result = _result(
        stdout="Issue reproduced\n",
        stderr="ModuleNotFoundError: No module named 'missing_module'",
        returncode=1,
    )
    assert classify_execution(result, strict=True) == "dependency_error"


def test_strict_marker_with_stderr_traceback_is_not_success():
    result = _result(
        stdout="Issue reproduced\n",
        stderr="Traceback (most recent call last):\n  File 'reproduce.py', line 1, in <module>\nValueError: boom",
        returncode=1,
    )
    assert classify_execution(result, strict=True) == "harness_error"
    assert validate_fail_to_pass(result, strict=True).success is False


def test_strict_extra_stdout_with_marker_is_not_success():
    result = _result(stdout="tool log line\nIssue reproduced\n", returncode=0)
    assert classify_execution(result, strict=True) == "oracle_error"
    assert validate_fail_to_pass(result, strict=True).success is False


def test_strict_single_marker_line_classifies_success():
    reproduced = _result(stdout="Issue reproduced\n", returncode=0)
    resolved = _result(stdout="Issue resolved\n", returncode=0)
    assert classify_execution(reproduced, strict=True) == "reproduced"
    assert classify_execution(resolved, strict=True) == "resolved"
    assert validate_fail_to_pass(reproduced, resolved, strict=True).success is True


def test_strict_timeout_wins_over_marker():
    result = _result(stdout="Issue reproduced\n", returncode=None, timed_out=True)
    assert classify_execution(result, strict=True) == "timeout"


def test_strict_ansi_and_blank_lines_do_not_break_single_line_rule():
    result = _result(stdout="\n\x1b[32mIssue reproduced\x1b[0m\n\n", returncode=0)
    assert classify_execution(result, strict=True) == "reproduced"


def test_strict_other_issues_with_traceback_is_harness_error():
    result = _result(
        stdout="Other issues\n",
        stderr="Traceback (most recent call last):\nValueError: bad assumption",
        returncode=1,
    )
    assert classify_execution(result, strict=True) == "harness_error"


def test_strict_validate_fail_to_pass_rejects_noisy_fixed_output():
    buggy = _result(stdout="Issue reproduced\n", returncode=0)
    fixed = _result(stdout="noise\nIssue resolved\n", returncode=0)
    validation = validate_fail_to_pass(buggy, fixed, strict=True)
    assert validation.success is False
    assert validation.buggy_status == "reproduced"
    assert validation.fixed_status == "oracle_error"


def test_legacy_classification_keeps_marker_last_line_behavior():
    # legacy mode (patch_aware) keeps the historical tolerance for extra
    # stdout lines as long as the marker is the final non-empty line
    result = _result(stdout="tool log\nIssue reproduced\n", returncode=0)
    assert classify_execution(result) == "reproduced"


# ---------------------------------------------------------------------------
# Feedback loop protocol tests
# ---------------------------------------------------------------------------


def _build_context():
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    llm_client = MockLLMClient()
    bug_spec = extract_bug_spec(issue_text, llm_client)
    retrieved_context = retrieve_context(Path("examples/mock_buggy_repo"), bug_spec)
    concise_context = build_concise_context(issue_text, bug_spec, retrieved_context)
    return issue_text, llm_client, bug_spec, retrieved_context, concise_context


class RecordingMockLLM(MockLLMClient):
    """Mock client that records every prompt handed to the LLM."""

    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []

    def generate_harness(self, concise_context: str) -> str:
        code = super().generate_harness(concise_context)
        self.prompts.append(self.last_prompt)
        return code

    def repair_harness(self, concise_context: str, current_code: str, feedback: str) -> str:
        code = super().repair_harness(concise_context, current_code, feedback)
        self.prompts.append(self.last_prompt)
        return code

    def strengthen_oracle(self, concise_context: str, current_code: str, feedback: str) -> str:
        code = super().strengthen_oracle(concise_context, current_code, feedback)
        self.prompts.append(self.last_prompt)
        return code

    def repair_dependency(self, repo_slug, missing_module, stderr_tail, python_version, declaration_summary, feedback="", installed_packages=""):
        payload = super().repair_dependency(
            repo_slug, missing_module, stderr_tail, python_version, declaration_summary, feedback, installed_packages
        )
        self.prompts.append(self.last_prompt)
        return payload


FIXED_LEAK_MARKERS = (
    "FIXED_SECRET_OUTPUT",
    "FIXED_SECRET_ERR",
    "Fixed repo status:",
    "Fixed repo stdout:",
    "Fixed repo stderr:",
)


def _fake_execution(repo_path: Path, command: str, stdout: str, stderr: str = "", returncode: int = 0) -> ExecutionResult:
    return ExecutionResult(
        repo_path=Path(repo_path),
        command=command or "python reproduce.py",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _patch_executor(monkeypatch, buggy_stdout: str, fixed_stdout: str, fixed_stderr: str = ""):
    """Route buggy/fixed executions to fakes; count fixed executions."""
    fixed_calls: list[str] = []

    def fake_run_harness(repo_path, command=None, timeout=30):
        if Path(repo_path).name == "mock_fixed_repo":
            fixed_calls.append(command or "")
            return _fake_execution(repo_path, command, fixed_stdout, fixed_stderr)
        return _fake_execution(repo_path, command, buggy_stdout)

    monkeypatch.setattr(
        feedback_loop_module,
        "write_harness",
        lambda repo_path, harness_candidate, filename="reproduce.py": Path(repo_path) / filename,
    )
    monkeypatch.setattr(feedback_loop_module, "run_harness", fake_run_harness)
    return fixed_calls


def test_patch_blind_does_not_repair_when_fixed_does_not_resolve(monkeypatch):
    _, initial_client, bug_spec, retrieved_context, concise_context = _build_context()
    initial_harness = generate_harness(concise_context, initial_client)
    loop_client = RecordingMockLLM()
    fixed_calls = _patch_executor(
        monkeypatch,
        buggy_stdout="Issue reproduced\n",
        fixed_stdout="FIXED_SECRET_OUTPUT\n",
        fixed_stderr="FIXED_SECRET_ERR",
    )

    result = run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=initial_harness,
        llm_client=loop_client,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        max_attempts=5,
        initial_prompt_text=initial_client.last_prompt,
        feedback_mode="patch_blind",
    )

    assert result.validation.success is False
    assert result.validation.buggy_status == "reproduced"
    assert result.validation.fixed_status == "oracle_error"
    # the loop ends instead of repairing from the fixed execution
    assert len(result.attempts) == 1
    assert loop_client.prompts == []
    # fixed repo executed exactly once, only for the offline F2P evaluation
    assert len(fixed_calls) == 1


def test_patch_blind_runs_fixed_once_when_it_resolves(monkeypatch):
    _, initial_client, bug_spec, retrieved_context, concise_context = _build_context()
    initial_harness = generate_harness(concise_context, initial_client)
    loop_client = RecordingMockLLM()
    fixed_calls = _patch_executor(
        monkeypatch,
        buggy_stdout="Issue reproduced\n",
        fixed_stdout="Issue resolved\n",
    )

    result = run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=initial_harness,
        llm_client=loop_client,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        max_attempts=5,
        initial_prompt_text=initial_client.last_prompt,
        feedback_mode="patch_blind",
    )

    assert result.validation.success is True
    assert len(result.attempts) == 1
    assert len(fixed_calls) == 1
    assert loop_client.prompts == []


def test_patch_blind_never_runs_fixed_without_buggy_reproduction(monkeypatch):
    _, initial_client, bug_spec, retrieved_context, concise_context = _build_context()
    initial_harness = generate_harness(concise_context, initial_client)
    loop_client = RecordingMockLLM()
    fixed_calls = _patch_executor(
        monkeypatch,
        buggy_stdout="Other issues\n",
        fixed_stdout="Issue resolved\n",
    )

    result = run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=initial_harness,
        llm_client=loop_client,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        max_attempts=3,
        initial_prompt_text=initial_client.last_prompt,
        feedback_mode="patch_blind",
    )

    assert len(fixed_calls) == 0
    assert len(result.attempts) == 3
    assert all(attempt.fixed_execution is None for attempt in result.attempts)


def test_patch_blind_prompts_never_contain_fixed_execution_content(monkeypatch):
    _, initial_client, bug_spec, retrieved_context, concise_context = _build_context()
    initial_harness = generate_harness(concise_context, initial_client)
    loop_client = RecordingMockLLM()
    _patch_executor(
        monkeypatch,
        buggy_stdout="Issue reproduced\n",
        fixed_stdout="FIXED_SECRET_OUTPUT\n",
        fixed_stderr="FIXED_SECRET_ERR",
    )

    result = run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=initial_harness,
        llm_client=loop_client,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        max_attempts=5,
        initial_prompt_text=initial_client.last_prompt,
        feedback_mode="patch_blind",
    )

    all_prompts = [attempt.prompt_text for attempt in result.attempts] + loop_client.prompts
    assert all_prompts
    for prompt in all_prompts:
        for marker in FIXED_LEAK_MARKERS:
            assert marker not in prompt


def test_patch_aware_keeps_fixed_feedback_in_prompts(monkeypatch):
    _, initial_client, bug_spec, retrieved_context, concise_context = _build_context()
    initial_harness = generate_harness(concise_context, initial_client)
    loop_client = RecordingMockLLM()
    fixed_calls = _patch_executor(
        monkeypatch,
        buggy_stdout="Issue reproduced\n",
        fixed_stdout="FIXED_SECRET_OUTPUT\n",
        fixed_stderr="FIXED_SECRET_ERR",
    )

    result = run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=initial_harness,
        llm_client=loop_client,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        max_attempts=2,
        initial_prompt_text=initial_client.last_prompt,
        feedback_mode="patch_aware",
    )

    # the legacy protocol continues past a not-resolved fixed repo and hands
    # the fixed execution feedback to the oracle strengthening prompt
    assert len(result.attempts) == 2
    assert result.attempts[1].action == "strengthen_oracle"
    assert len(fixed_calls) == 2
    strengthen_prompt = result.attempts[1].prompt_text
    assert "FIXED_SECRET_OUTPUT" in strengthen_prompt
    assert "FIXED_SECRET_ERR" in strengthen_prompt
    assert "Fixed repo status: oracle_error" in strengthen_prompt
    assert loop_client.prompts == [strengthen_prompt]


# ---------------------------------------------------------------------------
# Non-feedback-loop pipeline protocol tests
# ---------------------------------------------------------------------------


def _crafted_pipeline_executions():
    # buggy output carries an extra stdout line before the marker, which only
    # the legacy classifier tolerates.
    buggy = ExecutionResult(
        repo_path=Path("examples/mock_buggy_repo"),
        command="python reproduce.py",
        returncode=0,
        stdout="tool log\nIssue reproduced\n",
        stderr="",
    )
    fixed = ExecutionResult(
        repo_path=Path("examples/mock_fixed_repo"),
        command="python reproduce.py",
        returncode=0,
        stdout="Issue resolved\n",
        stderr="",
    )
    return buggy, fixed


def _patch_pipeline_execution(monkeypatch, captured):
    canned = (
        HarnessCandidate(code="print('Issue reproduced')\n"),
        "initial prompt",
        LLMCallMetadata(),
    )
    buggy_execution, fixed_execution = _crafted_pipeline_executions()

    def fake_select(concise_context, llm_client, buggy_repo, python_executable, candidate_count=3, strict=False):
        captured["select_strict"] = strict
        return canned

    real_validate = pipeline_module.validate_fail_to_pass

    def spy_validate(buggy_result, fixed_result=None, *, strict=False):
        captured["validate_strict"] = strict
        return real_validate(buggy_result, fixed_result, strict=strict)

    def fake_run_harness(repo_path, command=None, timeout=30):
        return buggy_execution if Path(repo_path).name == "mock_buggy_repo" else fixed_execution

    monkeypatch.setattr(pipeline_module, "_select_initial_harness", fake_select)
    monkeypatch.setattr(pipeline_module, "validate_fail_to_pass", spy_validate)
    monkeypatch.setattr(
        pipeline_module,
        "write_harness",
        lambda repo_path, harness_candidate, filename="reproduce.py": Path(repo_path) / filename,
    )
    monkeypatch.setattr(pipeline_module, "run_harness", fake_run_harness)


def test_run_pipeline_patch_blind_uses_strict_validation(monkeypatch):
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    captured = {}
    _patch_pipeline_execution(monkeypatch, captured)

    result = run_pipeline(
        issue_text,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        use_mock_llm=True,
        feedback_mode="patch_blind",
    )

    assert result.feedback_mode == "patch_blind"
    assert captured["select_strict"] is True
    assert captured["validate_strict"] is True
    # strict classification rejects the marker behind an extra stdout line
    assert result.validation.success is False
    assert result.validation.buggy_status == "oracle_error"


def test_run_pipeline_default_keeps_patch_aware_legacy_tolerance(monkeypatch):
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    captured = {}
    _patch_pipeline_execution(monkeypatch, captured)

    result = run_pipeline(
        issue_text,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        use_mock_llm=True,
    )

    assert result.feedback_mode == "patch_aware"
    assert captured["select_strict"] is False
    assert captured["validate_strict"] is False
    # legacy accepts the marker as the final non-empty stdout line
    assert result.validation.success is True
    assert result.validation.buggy_status == "reproduced"
    assert result.validation.fixed_status == "resolved"


# ---------------------------------------------------------------------------
# Result artifacts
# ---------------------------------------------------------------------------


def test_result_json_records_feedback_mode(tmp_path: Path):
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    result = run_pipeline_with_feedback_loop(
        issue_text=issue_text,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        use_mock_llm=True,
        max_attempts=3,
        llm_provider="mock",
        feedback_mode="patch_blind",
    )
    prepared = PreparedRepos(
        instance_id="demo__repo-1",
        repo="demo/repo",
        base_commit="abc123",
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        patch_applied=True,
        repo_validated=True,
    )
    instance = {
        "instance_id": "demo__repo-1",
        "repo": "demo/repo",
        "base_commit": "abc123",
        "problem_statement": issue_text,
        "patch": "",
    }

    result_json_path = write_experiment_record(
        instance=instance,
        prepared=prepared,
        result=result,
        max_attempts=3,
        output_root=tmp_path / "outputs",
    )

    payload = json.loads(result_json_path.read_text(encoding="utf-8"))
    assert payload["run_config"]["feedback_mode"] == "patch_blind"
    assert payload["final_result"]["is_f2p"] is True


def _load_script_module(script_name: str):
    script_path = Path("scripts") / script_name
    spec = spec_from_file_location(script_name.removesuffix(".py"), script_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summarizer_reads_legacy_result_without_feedback_mode(tmp_path: Path):
    module = _load_script_module("summarize_swebench_experiment.py")
    result_dir = tmp_path / "outputs" / "demo__repo-1"
    result_dir.mkdir(parents=True)
    # pre-P0 result.json: run_config exists but has no feedback_mode key
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "instance_metadata": {
                    "repo": "demo/repo",
                    "repo_validated": True,
                    "patch_applied": True,
                },
                "run_config": {
                    "llm_provider": "anthropic",
                    "model": "deepseek-v4-pro",
                    "max_attempts": 5,
                    "retrieval_mode": "keyword",
                    "executor": "subprocess",
                },
                "environment_repairs": [{"success": True}],
                "attempts_summary": [{"llm_metadata": {"total_tokens": 10}}],
                "final_result": {
                    "buggy_status": "reproduced",
                    "fixed_status": "resolved",
                    "failure_category": None,
                },
            }
        ),
        encoding="utf-8",
    )

    row = module.summarize_instance(
        {"instance_id": "demo__repo-1", "repo": "demo/repo"},
        tmp_path / "outputs",
    )

    assert row["reproduced?"] == "yes"
    assert row["fixed passed?"] == "yes"
    assert row["cost"] == "10"


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_feedback_mode_option_exists():
    runner = CliRunner()
    help_result = runner.invoke(app, ["run-swebench-one", "--help"])
    assert help_result.exit_code == 0
    assert "--feedback-mode" in help_result.stdout
    assert "patch_blind" in help_result.stdout
    assert "patch_aware" in help_result.stdout


def test_cli_rejects_invalid_feedback_mode(tmp_path: Path):
    runner = CliRunner()
    instances_file = tmp_path / "instances.jsonl"
    instances_file.write_text(
        json.dumps(
            {
                "instance_id": "demo__repo-1",
                "repo": "demo/repo",
                "base_commit": "abc123",
                "problem_statement": "x",
                "patch": "y",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    workdir = tmp_path / "repos"
    workdir.mkdir()

    result = runner.invoke(
        app,
        [
            "run-swebench-one",
            "--instances-file",
            str(instances_file),
            "--instance-id",
            "demo__repo-1",
            "--workdir",
            str(workdir),
            "--feedback-mode",
            "patch-everything",
        ],
    )

    assert result.exit_code == 2
    assert "Invalid value" in result.output
