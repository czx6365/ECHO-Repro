from pathlib import Path
import shlex
import sys

from echo_repro.executor import run_harness, write_harness
from echo_repro.models import HarnessCandidate
from echo_repro.validator import classify_execution, validate_fail_to_pass


def test_executor_and_validator_classify_buggy_and_fixed_runs():
    harness = HarnessCandidate(
        code="""
from buggy_module import divide

try:
    divide(10, 0)
except ZeroDivisionError:
    print("Issue resolved")
else:
    print("Issue reproduced")
""".strip()
    )

    buggy_repo = Path("examples/mock_buggy_repo")
    fixed_repo = Path("examples/mock_fixed_repo")
    write_harness(buggy_repo, harness)
    write_harness(fixed_repo, harness)

    buggy_result = run_harness(buggy_repo)
    fixed_result = run_harness(fixed_repo)

    assert classify_execution(buggy_result) == "reproduced"
    assert classify_execution(fixed_result) == "resolved"

    validation = validate_fail_to_pass(buggy_result, fixed_result)
    assert validation.success is True
    assert validation.buggy_status == "reproduced"
    assert validation.fixed_status == "resolved"


def test_validator_prefers_stderr_environment_signal_over_other_issues_marker():
    result = run_harness(
        Path("examples/mock_buggy_repo"),
        command=(
            f"{shlex.quote(sys.executable)} -c \"import sys; "
            "print('Other issues'); "
            "print('ImportError: cannot import name _compiler', file=sys.stderr)\""
        ),
    )

    assert classify_execution(result) == "environment_error"


def test_validator_uses_final_stdout_marker_line():
    result = run_harness(
        Path("examples/mock_buggy_repo"),
        command=(
            f"{shlex.quote(sys.executable)} -c \""
            "print('tool log'); "
            "print('\\x1b[01mbuild succeeded\\x1b[39;49;00m'); "
            "print('Issue reproduced')\""
        ),
    )

    assert classify_execution(result) == "reproduced"


def test_validator_treats_other_issues_traceback_as_harness_error():
    result = run_harness(
        Path("examples/mock_buggy_repo"),
        command=(
            f"{shlex.quote(sys.executable)} -c \"import sys; "
            "print('Other issues'); "
            "print('Traceback (most recent call last):', file=sys.stderr); "
            "print('ValueError: bad harness assumption', file=sys.stderr)\""
        ),
    )

    assert classify_execution(result) == "harness_error"
