from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from echo_repro.models import (
    NativeExecutionResult,
    NativeOracleResult,
    NativeTestCounts,
    PipelineResult,
    PreparedRepos,
)

NATIVE_ORACLE_MODES: tuple[str, ...] = ("off", "pytest")

# P1-A only supports pytest-style FAIL_TO_PASS targets:
#   path/to/test_file.py
#   path/to/test_file.py::test_name
#   path/to/test_file.py::ClassName::test_name
#   path/to/test_file.py::test_name[param-id]
# Django-style labels such as "test_name (test_utils.tests.SomeClass)" or
# "test_utils.tests.SomeClass.test_name" contain no ".py" path and are rejected.
_PYTEST_TARGET_RE = re.compile(r"^[^\s:()]+\.py(::[^\s:()]+)*$")

PATCH_TIMEOUT_SECONDS = 120


def parse_fail_to_pass(value: Any) -> list[str] | None:
    """Parse a SWE-bench FAIL_TO_PASS value into a list of pytest targets.

    Accepts a JSON-encoded list string (e.g. ``'["a.py::test_x"]'``) or an
    already parsed ``list[str]``. Returns ``None`` for empty values and
    invalid JSON — callers must surface that as an explicit
    unsupported/setup status, never as a plain failure.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(value, list):
        return None
    targets = [str(item).strip() for item in value if str(item).strip()]
    return targets or None


def is_pytest_target(target: str) -> bool:
    """True when the FAIL_TO_PASS entry looks like a pytest node ID or a
    pytest test-file path; False for Django unittest labels and other
    non-pytest runners."""
    return bool(_PYTEST_TARGET_RE.match(target))


def _oracle_python(pipeline_result: PipelineResult) -> Path:
    """Interpreter for the native runs: the ready environment profile's
    python when available, else the current interpreter."""
    profile = pipeline_result.environment_profile
    if profile and profile.ready and profile.python_path:
        return Path(profile.python_path)
    return Path(sys.executable)


def _copy_repo(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))


def _run_command(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout,
        shell=False,
    )


def _apply_test_patch(repo_dir: Path, test_patch: str) -> subprocess.CompletedProcess:
    """Apply the SWE-bench test_patch (a unified diff) to one isolated copy.
    ``--batch`` keeps patch from prompting; stdin carries the diff, so no
    shell or temporary patch file is involved."""
    return _run_command(
        ["patch", "-p1", "--batch", "--forward"],
        cwd=repo_dir,
        input_text=test_patch,
        timeout=PATCH_TIMEOUT_SECONDS,
    )


def _run_pytest(
    python: Path,
    repo_dir: Path,
    xml_path: Path,
    targets: list[str],
    timeout: int,
    role: str,
) -> tuple[NativeExecutionResult, NativeTestCounts | None]:
    cmd = [
        str(python),
        "-m",
        "pytest",
        "-q",
        "--disable-warnings",
        "--tb=short",
        f"--junitxml={xml_path}",
        "--",
        *targets,
    ]
    try:
        completed = _run_command(cmd, cwd=repo_dir, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (
            NativeExecutionResult(
                repo_role=role,  # type: ignore[arg-type]
                command=cmd,
                returncode=None,
                stdout="",
                stderr="",
                timed_out=True,
                counts=None,
            ),
            None,
        )
    counts = _parse_junit_counts(xml_path)
    return (
        NativeExecutionResult(
            repo_role=role,  # type: ignore[arg-type]
            command=cmd,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            counts=counts,
        ),
        counts,
    )


def _parse_junit_counts(xml_path: Path) -> NativeTestCounts | None:
    """Sum tests/failures/errors/skipped across every <testsuite> element.
    Returns None when the JUnit XML is missing or unparsable."""
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError):
        return None
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in tree.iter("testsuite"):
        for key in totals:
            try:
                totals[key] += int(suite.get(key, "0"))
            except (TypeError, ValueError):
                pass
    return NativeTestCounts(**totals)


def run_native_oracle(
    *,
    instance: dict,
    prepared: PreparedRepos,
    pipeline_result: PipelineResult,
    mode: str = "off",
    timeout_seconds: int = 600,
) -> NativeOracleResult:
    """Execute the native pytest oracle (P1-A).

    Runs strictly after the pipeline/feedback loop has finished: it copies the
    prepared repos into isolated TemporaryDirectory copies, applies test_patch
    to both, and checks the official FAIL_TO_PASS tests against JUnit XML.
    The outcome is never fed back into harness generation or the LLM.
    """
    if mode not in NATIVE_ORACLE_MODES:
        raise ValueError(f"Unknown native oracle mode: {mode!r}")
    if mode == "off":
        return NativeOracleResult(
            mode="off",
            status="not_run",
            native_f2p=None,
            reason="Native oracle disabled for this run.",
        )

    targets = parse_fail_to_pass(instance.get("FAIL_TO_PASS"))
    if targets is None:
        return NativeOracleResult(
            mode="pytest",
            status="unsupported",
            native_f2p=None,
            reason=f"FAIL_TO_PASS is missing, empty, or not valid JSON: {instance.get('FAIL_TO_PASS')!r}",
        )
    unsupported = [target for target in targets if not is_pytest_target(target)]
    if unsupported:
        return NativeOracleResult(
            mode="pytest",
            status="unsupported",
            target_tests=targets,
            native_f2p=None,
            reason=(
                "FAIL_TO_PASS contains non-pytest targets (P1-A supports pytest node IDs "
                f"only): {', '.join(unsupported)}"
            ),
        )
    test_patch = str(instance.get("test_patch") or "").strip()
    if not test_patch:
        return NativeOracleResult(
            mode="pytest",
            framework="pytest",
            status="setup_error",
            target_tests=targets,
            native_f2p=None,
            reason="test_patch is missing or empty; cannot set up the native oracle evaluation.",
        )

    python = _oracle_python(pipeline_result)

    with tempfile.TemporaryDirectory(prefix="echo_native_oracle_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        buggy_eval = tmp_root / "buggy_eval"
        fixed_eval = tmp_root / "fixed_eval"
        try:
            _copy_repo(prepared.buggy_repo, buggy_eval)
            _copy_repo(prepared.fixed_repo, fixed_eval)
        except Exception as exc:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="setup_error",
                target_tests=targets,
                native_f2p=None,
                reason=f"failed to copy prepared repos into isolated evaluation copies: {exc}",
            )

        try:
            buggy_patch = _apply_test_patch(buggy_eval, test_patch)
        except subprocess.TimeoutExpired:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="setup_error",
                target_tests=targets,
                native_f2p=None,
                reason="timed out applying test_patch on the buggy evaluation copy.",
            )
        if buggy_patch.returncode != 0:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="setup_error",
                target_tests=targets,
                test_patch_applied_buggy=False,
                native_f2p=None,
                reason=(
                    f"test_patch failed to apply on the buggy evaluation copy "
                    f"(returncode {buggy_patch.returncode}): {buggy_patch.stderr.strip()[:400]}"
                ),
            )
        try:
            fixed_patch = _apply_test_patch(fixed_eval, test_patch)
        except subprocess.TimeoutExpired:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="setup_error",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=False,
                native_f2p=None,
                reason="timed out applying test_patch on the fixed evaluation copy.",
            )
        if fixed_patch.returncode != 0:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="setup_error",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=False,
                native_f2p=None,
                reason=(
                    f"test_patch failed to apply on the fixed evaluation copy "
                    f"(returncode {fixed_patch.returncode}): {fixed_patch.stderr.strip()[:400]}"
                ),
            )

        buggy_execution, buggy_counts = _run_pytest(
            python, buggy_eval, tmp_root / "junit_buggy.xml", targets, timeout_seconds, role="buggy"
        )
        if buggy_execution.timed_out:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="timeout",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                native_f2p=None,
                reason="buggy native evaluation timed out.",
            )
        if buggy_counts is None:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="collection_error",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                native_f2p=None,
                reason="buggy JUnit XML is missing or unparsable; cannot confirm the expected failure.",
            )
        if buggy_counts.tests == 0:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="collection_error",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                native_f2p=None,
                reason="no tests collected on the buggy evaluation copy.",
            )
        if buggy_counts.errors > 0:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="collection_error",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                native_f2p=None,
                reason="buggy test errors (import/setup) — not a valid expected failure.",
            )
        if buggy_counts.skipped > 0:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="buggy_not_failed",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                native_f2p=False,
                reason="buggy evaluation skipped tests; expected failure not demonstrated.",
            )
        if buggy_execution.returncode not in (0, 1):
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="collection_error",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                native_f2p=None,
                reason=(
                    f"buggy pytest exited with returncode {buggy_execution.returncode} "
                    "(collection/internal error)."
                ),
            )
        if (
            buggy_execution.returncode == 0
            or buggy_counts.failures == 0
            or buggy_counts.failures != buggy_counts.tests
        ):
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="buggy_not_failed",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                native_f2p=False,
                reason="official FAIL_TO_PASS tests did not all fail on the buggy copy.",
            )

        # Buggy side confirmed the expected failure; only now run the fixed side.
        fixed_execution, fixed_counts = _run_pytest(
            python, fixed_eval, tmp_root / "junit_fixed.xml", targets, timeout_seconds, role="fixed"
        )
        if fixed_execution.timed_out:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="timeout",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                fixed_execution=fixed_execution,
                native_f2p=None,
                reason="fixed native evaluation timed out.",
            )
        if fixed_counts is None:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="collection_error",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                fixed_execution=fixed_execution,
                native_f2p=None,
                reason="fixed JUnit XML is missing or unparsable; cannot confirm the pass.",
            )
        if fixed_counts.tests == 0:
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="collection_error",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                fixed_execution=fixed_execution,
                native_f2p=None,
                reason="no tests collected on the fixed evaluation copy.",
            )
        if fixed_execution.returncode not in (0, 1):
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="collection_error",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                fixed_execution=fixed_execution,
                native_f2p=None,
                reason=(
                    f"fixed pytest exited with returncode {fixed_execution.returncode} "
                    "(collection/internal error)."
                ),
            )
        if (
            fixed_execution.returncode != 0
            or fixed_counts.failures > 0
            or fixed_counts.errors > 0
            or fixed_counts.skipped > 0
        ):
            return NativeOracleResult(
                mode="pytest",
                framework="pytest",
                status="fixed_not_passed",
                target_tests=targets,
                test_patch_applied_buggy=True,
                test_patch_applied_fixed=True,
                buggy_execution=buggy_execution,
                fixed_execution=fixed_execution,
                native_f2p=False,
                reason="official FAIL_TO_PASS tests did not all pass on the fixed copy.",
            )

        return NativeOracleResult(
            mode="pytest",
            framework="pytest",
            status="confirmed",
            target_tests=targets,
            test_patch_applied_buggy=True,
            test_patch_applied_fixed=True,
            buggy_execution=buggy_execution,
            fixed_execution=fixed_execution,
            native_f2p=True,
            reason="official FAIL_TO_PASS tests failed on buggy and passed on fixed.",
        )
