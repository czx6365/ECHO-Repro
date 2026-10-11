import json
from pathlib import Path

from echo_repro.models import NativeOracleResult, PreparedRepos
from echo_repro.pipeline import run_pipeline_with_feedback_loop
from echo_repro.result_writer import SCHEMA_VERSION, write_experiment_record, write_preparation_failure_record


def test_result_writer_creates_research_artifacts(tmp_path: Path):
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    result = run_pipeline_with_feedback_loop(
        issue_text=issue_text,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        use_mock_llm=True,
        max_attempts=3,
        llm_provider="mock",
    )
    prepared = PreparedRepos(
        instance_id="demo__repo-1",
        repo="demo/repo",
        base_commit="abc123",
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        patch_applied=True,
        repo_validated=True,
        buggy_commit="abc123",
        fixed_commit="abc123",
        fixed_diff_stat="sample.txt | 2 +-",
        repo_cache_path=Path("repos/cache/demo__repo"),
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

    output_dir = result_json_path.parent
    payload = json.loads(result_json_path.read_text(encoding="utf-8"))

    assert result_json_path.exists()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["instance_metadata"]["repo_validated"] is True
    assert payload["instance_metadata"]["buggy_commit"] == "abc123"
    assert payload["instance_metadata"]["fixed_diff_stat"] == "sample.txt | 2 +-"
    assert payload["instance_metadata"]["repo_cache_path"] == "repos/cache/demo__repo"
    assert (output_dir / "concise_context.md").exists()
    assert (output_dir / "final_reproduce.py").exists()
    assert (output_dir / "attempts.jsonl").exists()
    assert (output_dir / "prompts" / "bug_spec.md").exists()
    assert (output_dir / "prompts" / "generate_attempt_1.md").exists()
    assert (output_dir / "attempts" / "reproduce_attempt_1.py").exists()
    attempts_lines = (output_dir / "attempts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert attempts_lines
    attempt_record = json.loads(attempts_lines[0])
    assert "llm_metadata" in attempt_record
    assert attempt_record["stage"] == "initial_generation"


def test_result_writer_creates_preparation_failure_record(tmp_path: Path):
    instance = {
        "instance_id": "demo__repo-fail",
        "repo": "demo/repo",
        "base_commit": "abc123",
    }

    result_json_path = write_preparation_failure_record(
        instance=instance,
        error=RuntimeError("clone timed out"),
        workdir=tmp_path / "repos",
        cache_dir=tmp_path / "repos" / "cache",
        output_root=tmp_path / "outputs",
    )

    payload = json.loads(result_json_path.read_text(encoding="utf-8"))
    assert payload["instance_metadata"]["repo_validated"] is False
    assert payload["instance_metadata"]["cache_dir"] == str(tmp_path / "repos" / "cache")
    assert payload["final_result"]["buggy_status"] == "repo_error"
    assert payload["final_result"]["failure_category"] == "repo_error"
    assert "clone timed out" in payload["final_result"]["reason"]
    # native oracle fields are present with explicit null/false states
    assert payload["run_config"]["native_oracle"] == "off"
    assert payload["run_config"]["native_test_timeout"] == 600
    assert payload["native_oracle"] is None
    assert payload["final_result"]["harness_f2p"] is False
    assert payload["final_result"]["native_f2p"] is None
    assert payload["final_result"]["verified_f2p"] is None


def test_result_writer_records_native_oracle_metrics(tmp_path: Path):
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    result = run_pipeline_with_feedback_loop(
        issue_text=issue_text,
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        use_mock_llm=True,
        max_attempts=3,
        llm_provider="mock",
    )
    harness_f2p = bool(result.validation.success)
    prepared = PreparedRepos(
        instance_id="demo__repo-n1",
        repo="demo/repo",
        base_commit="abc123",
        buggy_repo=Path("examples/mock_buggy_repo"),
        fixed_repo=Path("examples/mock_fixed_repo"),
        patch_applied=True,
    )
    instance = {
        "instance_id": "demo__repo-n1",
        "repo": "demo/repo",
        "base_commit": "abc123",
        "problem_statement": issue_text,
        "patch": "",
    }

    cases = [
        ("confirmed", True),
        ("fixed_not_passed", False),
        ("unsupported", None),
    ]
    for status, native_f2p in cases:
        expected_verified = None if native_f2p is None else (harness_f2p and native_f2p)
        with_native = result.model_copy(
            update={
                "native_oracle": NativeOracleResult(
                    mode="pytest",
                    framework="pytest",
                    status=status,
                    target_tests=["tests/test_x.py::test_y"],
                    native_f2p=native_f2p,
                    reason="t",
                )
            }
        )
        result_json_path = write_experiment_record(
            instance=instance,
            prepared=prepared.model_copy(update={"instance_id": f"demo__repo-n1-{status}"}),
            result=with_native,
            max_attempts=3,
            output_root=tmp_path / "outputs",
        )
        payload = json.loads(result_json_path.read_text(encoding="utf-8"))

        assert payload["schema_version"] == "0.3"
        assert payload["run_config"]["native_oracle"] == "pytest"
        assert payload["run_config"]["native_test_timeout"] == 600
        assert payload["native_oracle"]["status"] == status
        assert payload["native_oracle"]["native_f2p"] == native_f2p
        assert payload["final_result"]["is_f2p"] == harness_f2p
        assert payload["final_result"]["harness_f2p"] == harness_f2p
        assert payload["final_result"]["native_f2p"] == native_f2p
        assert payload["final_result"]["verified_f2p"] == expected_verified

    # a result without any attached native oracle (legacy library caller)
    result_json_path = write_experiment_record(
        instance=instance,
        prepared=prepared.model_copy(update={"instance_id": "demo__repo-n1-none"}),
        result=result,
        max_attempts=3,
        output_root=tmp_path / "outputs",
    )
    payload = json.loads(result_json_path.read_text(encoding="utf-8"))
    assert payload["native_oracle"] is None
    assert payload["run_config"]["native_oracle"] == "off"
    assert payload["final_result"]["harness_f2p"] == harness_f2p
    assert payload["final_result"]["native_f2p"] is None
    assert payload["final_result"]["verified_f2p"] is None
