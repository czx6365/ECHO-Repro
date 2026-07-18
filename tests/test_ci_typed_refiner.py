from echo_ci.typed_refiner import build_typed_feedback


def test_typed_feedback_repairs_fake_reproduction():
    feedback = build_typed_feedback(
        {
            "classification": "fake_reproduction",
            "fake_reproduction_reason": "unconditional exit",
        }
    )

    assert feedback["feedback_style"] == "failure_type_aware"
    assert feedback["typed_feedback_action"] == "replace_fake_reproduction"
    assert any("real project command" in item for item in feedback["repair_instructions"])


def test_typed_feedback_repairs_target_selection():
    feedback = build_typed_feedback(
        {
            "classification": "fixed_target_hallucinated",
            "fixed": {
                "stderr": "FileNotFoundError: pkg/missing.py",
                "target_preflight": {"has_invalid_target": True},
            },
        }
    )

    assert feedback["typed_feedback_action"] == "repair_target_selection"
    assert feedback["fixed"]["target_preflight"] == {"has_invalid_target": True}


def test_typed_feedback_repairs_oracle_without_changing_command_first():
    feedback = build_typed_feedback(
        {
            "classification": "oracle_error",
            "failing": {"exit_code": 1, "stderr": "AssertionError: expected 2"},
            "fixed": {"exit_code": 0, "stdout": "passed"},
        }
    )

    assert feedback["typed_feedback_action"] == "repair_oracle"
    assert "AssertionError" in feedback["failing"]["stderr_tail"]
    assert any("Keep the command stable" in item for item in feedback["repair_instructions"])
