from echo_ci.ci_repair_loader import (
    sample_stratified_extension,
    stratified_failure_bucket,
)


def _row(instance_id: str, *labels: str) -> dict:
    return {"id": instance_id, "error_type": list(labels)}


def test_stratified_failure_bucket_uses_paper_categories():
    assert stratified_failure_bucket(_row("1", "Test Failure")) == "test"
    assert stratified_failure_bucket(_row("2", "Package Installation Error")) == "dependency_install"
    assert stratified_failure_bucket(_row("3", "Configuration Error")) == "build_workflow_config"
    assert stratified_failure_bucket(_row("4", "Code Linting")) == "lint"
    assert stratified_failure_bucket(_row("5", "Code Formatting")) == "format"


def test_stratified_extension_is_exact_disjoint_and_deterministic():
    rows = [
        _row("old", "Test Failure"),
        _row("t1", "Test Failure"),
        _row("t2", "Test Failure"),
        _row("d1", "Dependency Issues"),
        _row("d2", "Package Installation Error"),
        _row("c1", "Configuration Error"),
        _row("l1", "Code Linting"),
        _row("f1", "Code Formatting"),
    ]
    quotas = {
        "test": 1,
        "dependency_install": 1,
        "build_workflow_config": 1,
        "lint": 1,
        "format": 1,
    }

    first, manifest = sample_stratified_extension(
        rows,
        exclude_ids={"old"},
        quotas=quotas,
        seed=42,
    )
    second, second_manifest = sample_stratified_extension(
        rows,
        exclude_ids={"old"},
        quotas=quotas,
        seed=42,
    )

    assert len(first) == 5
    assert "old" not in {row["id"] for row in first}
    assert {name: len(ids) for name, ids in manifest.items()} == quotas
    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert manifest == second_manifest
