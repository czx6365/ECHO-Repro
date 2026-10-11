from echo_ci.metrics import flatten_result, summarize_rows, summary_markdown


def test_summary_includes_env_policy_table():
    rows = [
        {
            "setting": "S3_log_only",
            "env_policy": "E2_tool_bootstrap",
            "classification": "fixed_missing_ci_tool",
            "executable": True,
            "buggy_failed": True,
            "fixed_passed": False,
            "f_to_p": False,
            "fake": False,
            "timeout": False,
            "attempts": 1,
            "duration": 0,
            "token_cost": 0,
            "instance_id": "1",
            "failure_type": "lint",
            "env_setups": [
                {
                    "tools_detected": ["flake8"],
                    "tools_missing": ["flake8"],
                    "tools_installed": ["flake8"],
                    "install_failed": [],
                }
            ],
        }
    ]
    markdown = summary_markdown(rows, {"overall": summarize_rows(rows)})

    assert "| setting | env_policy | executable | fixed_dependency_error | F->P | reproduced |" in markdown
    assert "| S3_log_only | E2_tool_bootstrap | 100.0% | 1 | 0.0% | 0 |" in markdown
    assert "| fixed_missing_ci_tool | 1 |" in markdown
    assert "| flake8 | 1 | 1 | 1 | 0 | 0 | 0 |" in markdown


def test_flatten_result_includes_routing_and_typed_feedback_fields():
    row = flatten_result(
        {
            "instance_id": "1",
            "setting": "S8_router_typed_refine",
            "routing_decision": {
                "failure_type": "lint",
                "selected_context_sources": ["ci_log", "workflow", "env"],
                "original_context_size_chars": 1000,
                "routed_context_size_chars": 400,
                "reduction_ratio": 0.6,
            },
            "generation": {"status": "generated"},
            "execution": {"classification": "oracle_error"},
            "attempts_detail": [
                {
                    "feedback_for_next_attempt": {
                        "typed_feedback_action": "repair_oracle",
                    }
                }
            ],
        }
    )

    assert row["routing_failure_type"] == "lint"
    assert row["selected_context_sources"] == "ci_log,workflow,env"
    assert row["context_size_chars"] == 400
    assert row["context_reduction_ratio"] == 0.6
    assert row["typed_feedback_actions"] == "repair_oracle"

    markdown = summary_markdown([row], {"overall": summarize_rows([row])})
    assert "## Routing Context Stats" in markdown
    assert "| S8_router_typed_refine | 1 | 400.0 | 60.0% | 0.0% |" in markdown
