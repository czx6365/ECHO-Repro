from pathlib import Path

from echo_repro.pipeline import run_pipeline


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
