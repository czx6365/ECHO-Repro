import json
from pathlib import Path

from echo_repro.swebench_adapter import (
    extract_issue_text,
    get_repo_metadata,
    load_instances,
    prepare_instance_stub,
    select_instance,
)


def test_swebench_adapter_loads_and_prepares_instance(tmp_path: Path):
    fixture = tmp_path / "instances.jsonl"
    rows = [
        {
            "instance_id": "django__django-12345",
            "repo": "django/django",
            "version": "lite",
            "base_commit": "abc123",
            "problem_statement": "QuerySet.count() returns the wrong value.",
            "hints_text": "Look at aggregation edge cases.",
            "patch": "diff --git a/x b/x",
            "test_patch": "diff --git a/tests b/tests",
        },
        {
            "instance_id": "pallets__flask-67890",
            "repo": "pallets/flask",
            "version": "lite",
            "base_commit": "def456",
            "problem_statement": "Second issue statement.",
        },
    ]
    fixture.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    instances = load_instances(fixture)
    instance = select_instance(instances, "django__django-12345")
    issue_text = extract_issue_text(instance)
    metadata = get_repo_metadata(instance)
    stub = prepare_instance_stub(instance)

    assert len(instances) == 2
    assert instance["repo"] == "django/django"
    assert "QuerySet.count()" in issue_text
    assert "Additional hints" in issue_text
    assert metadata["base_commit"] == "abc123"
    assert stub["instance_id"] == "django__django-12345"
    assert stub["repo_metadata"]["repo"] == "django/django"


def test_select_instance_raises_for_missing_id(tmp_path: Path):
    fixture = tmp_path / "instances.jsonl"
    fixture.write_text(
        json.dumps(
            {
                "instance_id": "only-one",
                "repo": "example/repo",
                "problem_statement": "Example problem.",
            }
        ),
        encoding="utf-8",
    )

    instances = load_instances(fixture)
    try:
        select_instance(instances, "missing-id")
    except ValueError as exc:
        assert "missing-id" in str(exc)
    else:
        raise AssertionError("Expected ValueError for missing instance id")
