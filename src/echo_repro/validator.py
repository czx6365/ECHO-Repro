from __future__ import annotations

import re

from echo_repro.models import ExecutionResult, ValidationResult


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _final_stdout_line(stdout: str) -> str:
    lines = [ANSI_RE.sub("", line).strip() for line in stdout.splitlines() if ANSI_RE.sub("", line).strip()]
    return lines[-1] if lines else ""


def classify_execution(result: ExecutionResult) -> str:
    stdout = _final_stdout_line(result.stdout)
    stderr = result.stderr.strip().lower()
    if stdout == "Issue reproduced":
        return "reproduced"
    if stdout == "Issue resolved":
        return "resolved"
    if result.timed_out:
        return "timeout"
    if "modulenotfounderror" in stderr or "no module named" in stderr:
        return "dependency_error"
    if "importerror" in stderr or "broken installation" in stderr or "cannot import name" in stderr:
        return "environment_error"
    if "syntaxerror" in stderr or "indentationerror" in stderr:
        return "harness_error"
    if "filenotfounderror" in stderr or "no such file" in stderr:
        return "harness_error"
    if stdout == "Other issues" and "traceback (most recent call last)" in stderr:
        return "harness_error"
    if stdout == "Other issues":
        return "oracle_error"
    if result.returncode not in (0, None):
        return "harness_error"
    return "oracle_error"


def validate_fail_to_pass(
    buggy_result: ExecutionResult,
    fixed_result: ExecutionResult | None = None,
) -> ValidationResult:
    buggy_status = classify_execution(buggy_result)
    fixed_status = classify_execution(fixed_result) if fixed_result else None
    if fixed_result is None:
        success = buggy_status == "reproduced"
        summary = "Buggy execution reproduced the issue." if success else "Buggy execution did not reproduce the issue."
        return ValidationResult(
            success=success,
            buggy_status=buggy_status,
            fixed_status=None,
            summary=summary,
        )

    success = buggy_status == "reproduced" and fixed_status == "resolved"
    summary = (
        "Fail-to-Pass satisfied: buggy repo reproduced the issue and fixed repo resolved it."
        if success
        else "Fail-to-Pass not satisfied."
    )
    return ValidationResult(
        success=success,
        buggy_status=buggy_status,
        fixed_status=fixed_status,
        summary=summary,
    )
