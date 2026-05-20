from __future__ import annotations

from echo_repro.models import ExecutionResult, ValidationResult


def classify_execution(result: ExecutionResult) -> str:
    stdout = result.stdout.strip()
    stderr = result.stderr.strip().lower()
    if result.timed_out:
        return "timeout"
    if stdout == "Issue reproduced":
        return "reproduced"
    if stdout == "Issue resolved":
        return "resolved"
    if "syntaxerror" in stderr:
        return "syntax_error"
    if "importerror" in stderr or "modulenotfounderror" in stderr:
        return "import_error"
    if "filenotfounderror" in stderr or "no such file" in stderr:
        return "file_error"
    return "other"


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

