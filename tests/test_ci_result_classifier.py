from echo_ci.result_classifier import (
    CommandExecution,
    classify_failing_error,
    classify_fixed_error,
    classify_result,
    detect_fake_reproduction,
)


def test_detect_fake_reproduction_unconditional_assertion():
    fake, reason = detect_fake_reproduction('raise AssertionError("Test failed")\n')

    assert fake is True
    assert "unconditional" in reason


def test_detect_fake_reproduction_allows_conditional_file_check():
    code = """
import sys

found = False
with open("package/module.py") as handle:
    for line in handle:
        if line.rstrip("\\n") != line.rstrip("\\n").rstrip():
            print("trim trailing whitespace.................................................Failed")
            found = True

if found:
    sys.exit(1)
sys.exit(0)
"""

    fake, reason = detect_fake_reproduction(code, error_signature="trim trailing whitespace")

    assert fake is False
    assert reason == ""


def test_classify_reproduced_when_failing_matches_and_fixed_passes():
    failing = CommandExecution(
        phase="failing",
        exit_code=1,
        stdout="AssertionError: expected 2",
    )
    fixed = CommandExecution(phase="fixed", exit_code=0, stdout="")
    oracle = {
        "expected_failing_exit_code": "nonzero",
        "expected_passing_exit_code": 0,
        "must_contain_on_failing": ["AssertionError"],
        "must_not_contain_on_fixed": ["AssertionError"],
    }

    assert classify_result(failing=failing, fixed=fixed, oracle=oracle) == "reproduced"


def test_classify_reproduced_allows_pass_message_with_forbidden_keyword():
    failing = CommandExecution(
        phase="failing",
        exit_code=1,
        stdout="FAIL: trailing whitespace detected",
    )
    fixed = CommandExecution(phase="fixed", exit_code=0, stdout="PASS: no trailing whitespace")
    oracle = {
        "expected_failing_exit_code": 1,
        "expected_passing_exit_code": 0,
        "must_contain_on_failing": ["trailing whitespace"],
        "must_not_contain_on_fixed": ["trailing whitespace"],
    }

    assert classify_result(failing=failing, fixed=fixed, oracle=oracle) == "reproduced"


def test_classify_fixed_checkout_error():
    fixed = CommandExecution(
        phase="fixed",
        error="error: pathspec 'abc123' did not match any file(s) known to git",
        error_kind="fixed_checkout_error",
    )

    assert classify_fixed_error(fixed)[0] == "fixed_checkout_error"
    assert classify_result(
        failing=CommandExecution(phase="failing", exit_code=1, stdout="failed"),
        fixed=fixed,
        oracle={"expected_failing_exit_code": "nonzero"},
    ) == "fixed_checkout_error"


def test_classify_failing_checkout_before_fixed_error():
    failing = CommandExecution(
        phase="failing",
        error="fatal: unable to read tree (abc123)",
    )

    assert classify_failing_error(failing)[0] == "failing_checkout_error"
    assert classify_result(
        failing=failing,
        fixed=None,
        oracle={"expected_failing_exit_code": "nonzero"},
    ) == "failing_checkout_error"


def test_classify_fixed_missing_third_party_package():
    fixed = CommandExecution(
        phase="fixed",
        exit_code=1,
        stderr="ModuleNotFoundError: No module named 'requests'",
    )

    assert classify_fixed_error(fixed)[0] == "fixed_missing_third_party_package"


def test_classify_fixed_missing_ci_tool():
    fixed = CommandExecution(
        phase="fixed",
        exit_code=127,
        stderr=".echo_repro/run.sh: line 5: flake8: command not found",
    )

    assert classify_fixed_error(fixed)[0] == "fixed_missing_ci_tool"


def test_classify_fixed_missing_project_package():
    fixed = CommandExecution(
        phase="fixed",
        exit_code=1,
        stderr="ModuleNotFoundError: No module named 'aiohttp'",
        project_name="aio-libs/aiohttp",
    )

    assert classify_fixed_error(fixed)[0] == "fixed_missing_project_package"


def test_classify_fixed_wrong_run_sh_path():
    fixed = CommandExecution(
        phase="fixed",
        exit_code=2,
        stderr="python3: can't open file '.echo_repro/reproduce.py': [Errno 2] No such file or directory",
        preflight={
            "echo_repro_dir_exists": True,
            "run_sh_exists": True,
            "reproduce_py_exists": True,
            "run_sh_content": "python3 .echo_repro/reproduce.py",
        },
    )

    assert classify_fixed_error(fixed)[0] == "fixed_wrong_run_sh_path"


def test_classify_fixed_missing_reproduce_py():
    fixed = CommandExecution(
        phase="fixed",
        exit_code=None,
        error="Missing .echo_repro/reproduce.py.",
        preflight={
            "echo_repro_dir_exists": True,
            "run_sh_exists": True,
            "reproduce_py_exists": False,
            "run_sh_content": "python3 reproduce.py",
        },
    )

    assert classify_fixed_error(fixed)[0] == "fixed_missing_reproduce_py"


def test_classify_fixed_missing_project_target_file():
    fixed = CommandExecution(
        phase="fixed",
        exit_code=1,
        stderr="FileNotFoundError: [Errno 2] No such file or directory: 'pkg/module.py'",
        preflight={
            "echo_repro_dir_exists": True,
            "run_sh_exists": True,
            "reproduce_py_exists": True,
            "run_sh_content": 'python3 "$REPO_ROOT/.echo_repro/reproduce.py"',
        },
    )

    assert classify_fixed_error(fixed)[0] == "fixed_missing_project_target_file"


def test_classify_fixed_command_fail():
    fixed = CommandExecution(
        phase="fixed",
        exit_code=1,
        stdout="lint still failed on fixed commit",
    )

    assert classify_fixed_error(fixed)[0] == "fixed_command_fail"


def test_portable_formatter_oracle_accepts_semantic_variants_and_paths():
    failing = CommandExecution(
        phase="failing",
        exit_code=1,
        stdout=(
            "would reformat src/axolotl/cli/train.py\n"
            "ERROR: /Users/me/work/failing_repo/src/axolotl/cli/train.py "
            "Imports are incorrectly sorted and/or formatted."
        ),
        preflight={"repo_root": "/Users/me/work/failing_repo"},
    )
    fixed = CommandExecution(phase="fixed", exit_code=0, stdout="")
    oracle = {
        "expected_failing_exit_code": "nonzero",
        "expected_passing_exit_code": 0,
        "must_contain_on_failing": [
            "reformatted src/axolotl/cli/train.py",
            "Fixing /home/runner/work/axolotl/axolotl/src/axolotl/cli/train.py",
        ],
        "must_not_contain_on_fixed": ["reformatted src/axolotl/cli/train.py"],
        "oracle_type": "format",
    }

    assert classify_result(failing=failing, fixed=fixed, oracle=oracle) == "reproduced"
