from echo_ci.ci_context_retriever import ContextBundle
from echo_ci.context_router import route_context


def _bundle(failure_type: str) -> ContextBundle:
    return ContextBundle(
        ci_log="Run ruff check .\nF401 unused import",
        workflow="jobs:\n  lint:\n    steps:\n      - run: ruff check .\n",
        source="### src/pkg/module.py\nimport os\n",
        tests="### tests/test_module.py\ndef test_x(): pass\n",
        env="### pyproject.toml\n[tool.ruff]\n",
        failure_spec={
            "failure_type": failure_type,
            "oracle_type": failure_type,
            "failed_command": "ruff check .",
            "error_signature": "F401 unused import",
        },
    )


def test_router_lint_keeps_command_target_and_config_but_drops_tests():
    routed, decision = route_context(_bundle("lint"))

    assert routed.ci_log
    assert routed.workflow
    assert routed.source
    assert routed.env
    assert routed.tests == ""
    assert decision.failure_type == "lint"
    assert decision.selected_context_sources == ["ci_log", "workflow", "source", "env"]
    assert decision.dropped_context_sources == ["tests"]
    assert decision.routed_context_size_chars < decision.original_context_size_chars


def test_router_dependency_keeps_install_context_and_drops_source_tests():
    bundle = _bundle("dependency")
    bundle.failure_spec["failed_command"] = "python -m pip install -e ."
    bundle.failure_spec["error_signature"] = "ModuleNotFoundError: No module named 'requests'"

    routed, decision = route_context(bundle)

    assert routed.ci_log
    assert routed.workflow
    assert routed.env
    assert routed.source == ""
    assert routed.tests == ""
    assert decision.selected_context_sources == ["ci_log", "workflow", "env"]
    assert decision.dropped_context_sources == ["source", "tests"]


def test_oracle_router_uses_override_label():
    bundle = _bundle("unknown")

    routed, decision = route_context(bundle, route_type="oracle_label", failure_type_override="test")

    assert decision.route_type == "oracle_label"
    assert decision.failure_type == "test"
    assert routed.tests
