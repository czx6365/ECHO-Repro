from __future__ import annotations

import re

from echo_repro.models import ExecutionResult, ValidationResult


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_stdout_lines(stdout: str) -> list[str]:
    """Non-empty stdout lines with ANSI escape sequences removed."""
    lines = []
    for line in stdout.splitlines():
        cleaned = ANSI_RE.sub("", line).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _final_stdout_line(stdout: str) -> str:
    lines = _clean_stdout_lines(stdout)
    return lines[-1] if lines else ""


def classify_execution(result: ExecutionResult, *, strict: bool = False) -> str:
    """Classify an execution by error precedence; protocol markers never mask
    real failures.

    Priority (applies in both modes):
    1. timed_out -> timeout
    2. ModuleNotFoundError / "no module named" -> dependency_error
    3. ImportError / "broken installation" / "cannot import name" -> environment_error
    4. SyntaxError / IndentationError / FileNotFoundError -> harness_error
    5. traceback -> harness_error (cannot be overridden by a marker)
    6. returncode None (without timeout) or nonzero -> harness_error (cannot be
       overridden by a marker)
    7. Only with timed_out == False, returncode == 0, no errors above, and a
       legal stdout marker:
       - "Issue reproduced" -> reproduced
       - "Issue resolved" -> resolved
       - "Other issues" -> harness_error when stderr carries a traceback
         (already caught above), otherwise oracle_error
    8. strict mode: after removing blank lines and ANSI, stdout must contain
       exactly one non-empty protocol line; extra stdout -> oracle_error.

    Legacy mode (strict=False) keeps only the historical tolerance of
    accepting a marker as the last non-empty stdout line even when other
    lines exist; it still rejects None/nonzero returncodes and real
    tracebacks/import errors.
    """
    stdout_lines = _clean_stdout_lines(result.stdout)
    final_line = stdout_lines[-1] if stdout_lines else ""
    stderr = result.stderr.strip().lower()

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
    if "traceback (most recent call last)" in stderr:
        return "harness_error"
    if result.returncode is None:
        # not timed_out (checked first) and no return code at all: the run
        # crashed in a way that never produced a clean marker acceptance
        return "harness_error"
    if result.returncode != 0:
        return "harness_error"

    if final_line in {"Issue reproduced", "Issue resolved"}:
        if strict and len(stdout_lines) != 1:
            return "oracle_error"
        return "reproduced" if final_line == "Issue reproduced" else "resolved"
    if final_line == "Other issues":
        return "oracle_error"
    return "oracle_error"


def validate_fail_to_pass(
    buggy_result: ExecutionResult,
    fixed_result: ExecutionResult | None = None,
    *,
    strict: bool = False,
) -> ValidationResult:
    buggy_status = classify_execution(buggy_result, strict=strict)
    fixed_status = classify_execution(fixed_result, strict=strict) if fixed_result else None
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
