from echo_ci.failure_spec import extract_failure_spec_rule_based


def test_failure_spec_rule_based_extracts_command_and_signature():
    instance = {
        "id": "demo-1",
        "repo": "demo/project",
        "fail_sha": "bad",
        "pass_sha": "good",
        "ci_log": """
Run pytest tests/test_math.py::test_divide
============================= test session starts =============================
FAILED tests/test_math.py::test_divide - AssertionError: expected 2
Traceback (most recent call last):
  File "tests/test_math.py", line 4, in test_divide
    assert divide(4, 2) == 2
AssertionError: expected 2
""",
        "workflow_yaml": "jobs:\n  test:\n    steps:\n      - run: pytest tests/test_math.py::test_divide\n",
    }

    spec = extract_failure_spec_rule_based(instance)

    assert spec.instance_id == "demo-1"
    assert spec.failed_command == "pytest tests/test_math.py::test_divide"
    assert "AssertionError: expected 2" in spec.error_signature
    assert spec.failure_type == "test"
    assert spec.oracle_type in {"assertion", "test"}
    assert "tests/test_math.py" in spec.suspected_artifacts

