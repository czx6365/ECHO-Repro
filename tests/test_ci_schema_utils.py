from echo_ci.schema_utils import field, get_field, normalize_failure_type


def test_get_field_accepts_aliases_and_case_drift():
    instance = {
        "Repository": "demo/project",
        "fail_sha": "abc123",
        "labels": ["Dependency", "CI"],
    }

    assert get_field(instance, ["repo", "repository"]) == "demo/project"
    assert get_field(instance, ["failing_commit", "fail_sha"]) == "abc123"
    assert field(instance, "repo") == "demo/project"
    assert normalize_failure_type(field(instance, "failure_type")) == "dependency"


def test_get_field_supports_nested_paths():
    instance = {"metadata": {"repo": "owner/name"}}

    assert get_field(instance, ["metadata.repo"]) == "owner/name"
    assert get_field(instance, ["missing"], default="fallback") == "fallback"

