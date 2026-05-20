import json
from pathlib import Path

from echo_repro.models import PreparedRepos
from echo_repro.pipeline import run_pipeline_with_feedback_loop
from echo_repro.result_writer import SCHEMA_VERSION, write_experiment_record


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
