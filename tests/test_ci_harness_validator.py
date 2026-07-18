from echo_ci.ci_context_retriever import ContextBundle
from echo_ci.harness_validator import validate_harness


def _context(failure_type: str = "format", failed_command: str = "pre-commit run --all-files"):
    return ContextBundle(
        failure_spec={
            "failure_type": failure_type,
            "oracle_type": failure_type,
            "failed_command": failed_command,
            "error_signature": "trim trailing whitespace failed",
        }
    )


def test_validator_rejects_unconditional_shell_exit():
    result = validate_harness(
        reproduce_text='print("trim trailing whitespace failed")\n',
        run_text="#!/usr/bin/env bash\necho failed\nexit 1\n",
        oracle={"oracle_type": "format", "must_contain_on_failing": ["trim trailing whitespace"]},
        context=_context(),
    )

    assert result["valid"] is False
    assert any("unconditional exit 1" in issue for issue in result["issues"])


def test_validator_rejects_install_for_format_failure():
    result = validate_harness(
        reproduce_text='import subprocess\nsubprocess.run(["pre-commit", "run", "--all-files"])\n',
        run_text="#!/usr/bin/env bash\npython3 -m pip install pre-commit\npython3 .echo_repro/reproduce.py\n",
        oracle={"oracle_type": "format", "must_contain_on_failing": ["trim trailing whitespace"]},
        context=_context(),
    )

    assert result["valid"] is False
    assert any("pip install" in issue for issue in result["issues"])


def test_validator_accepts_minimal_project_file_check():
    result = validate_harness(
        reproduce_text="""
import sys
with open("pkg/module.py") as handle:
    bad = any(line.rstrip("\\n") != line.rstrip() for line in handle)
if bad:
    print("trim trailing whitespace")
    sys.exit(1)
sys.exit(0)
""",
        run_text="#!/usr/bin/env bash\npython3 .echo_repro/reproduce.py\n",
        oracle={"oracle_type": "format", "must_contain_on_failing": ["trim trailing whitespace"]},
        context=_context(failed_command=""),
    )

    assert result["valid"] is True


def test_s5_validator_rejects_dropped_workflow_working_directory():
    context = _context(failed_command="ruff check .")
    context.workflow = """
jobs:
  lint:
    defaults:
      run:
        working-directory: libs/agno
    steps:
      - run: ruff check .
"""
    result = validate_harness(
        reproduce_text='import subprocess\nsubprocess.run(["ruff", "check", "."])\n',
        run_text='#!/usr/bin/env bash\nruff check .\n',
        oracle={"oracle_type": "lint", "must_contain_on_failing": ["F401"]},
        context=context,
        setting="S5_full_context",
        grounding={"selected_command": "ruff check ."},
    )

    assert result["valid"] is False
    assert any("working-directory `libs/agno`" in issue for issue in result["issues"])


def test_s5_validator_accepts_preserved_workflow_working_directory():
    context = _context(failed_command="ruff check .")
    context.workflow = """
jobs:
  lint:
    defaults:
      run:
        working-directory: libs/agno
    steps:
      - run: ruff check .
"""
    result = validate_harness(
        reproduce_text='import subprocess\nsubprocess.run(["ruff", "check", "."])\n',
        run_text='#!/usr/bin/env bash\ncd "$REPO_ROOT/libs/agno"\nruff check .\n',
        oracle={"oracle_type": "lint", "must_contain_on_failing": ["F401"]},
        context=context,
        setting="S5_full_context",
        grounding={"selected_command": "ruff check ."},
    )

    assert result["valid"] is True
    assert result["workflow_signal_preservation"]["preserved"] is True


def test_s1_validation_does_not_expose_workflow_signal():
    context = _context(failed_command="ruff check .")
    context.workflow = "jobs:\n  lint:\n    steps:\n      - run: ruff check .\n"
    result = validate_harness(
        reproduce_text="",
        run_text="ruff check .\n",
        oracle={"oracle_type": "lint", "must_contain_on_failing": ["F401"]},
        context=context,
        setting="S1_issue_only",
        grounding={"selected_command": "ruff check ."},
    )

    assert result["workflow_signal_preservation"]["applicable"] is False
    assert result["workflow_signal_preservation"]["workflow_signal"] == {}


def test_router_validation_exposes_workflow_signal_when_routed():
    context = _context(failed_command="ruff check .")
    context.workflow = """
jobs:
  lint:
    defaults:
      run:
        working-directory: libs/agno
    steps:
      - run: ruff check .
"""
    result = validate_harness(
        reproduce_text='import subprocess\nsubprocess.run(["ruff", "check", "."])\n',
        run_text='#!/usr/bin/env bash\ncd "$REPO_ROOT/libs/agno"\nruff check .\n',
        oracle={"oracle_type": "lint", "must_contain_on_failing": ["F401"]},
        context=context,
        setting="S7_router",
        grounding={"selected_command": "ruff check ."},
    )

    assert result["workflow_signal_preservation"]["applicable"] is True
    assert result["workflow_signal_preservation"]["preserved"] is True
