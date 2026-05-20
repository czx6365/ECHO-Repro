from __future__ import annotations

from pathlib import Path
import sys

from echo_repro.executor import run_harness, write_harness
from echo_repro.llm.base import BaseLLMClient
from echo_repro.models import (
    BugSpec,
    ExecutionResult,
    FeedbackLoopAttempt,
    HarnessCandidate,
    PipelineResult,
    RetrievedContext,
    ValidationResult,
)
from echo_repro.validator import classify_execution, validate_fail_to_pass


def repair_harness(
    concise_context: str,
    harness_candidate: HarnessCandidate,
    llm_client: BaseLLMClient,
    feedback: str,
    strengthen_oracle: bool = False,
) -> HarnessCandidate:
    if strengthen_oracle:
        code = llm_client.strengthen_oracle(concise_context, harness_candidate.code, feedback)
        rationale = "Harness oracle strengthened from execution feedback."
    else:
        code = llm_client.repair_harness(concise_context, harness_candidate.code, feedback)
        rationale = "Harness repaired from execution feedback."
    return HarnessCandidate(
        filename=harness_candidate.filename,
        code=code,
        rationale=rationale,
    )


def _run_once(repo_path: Path, harness_candidate: HarnessCandidate) -> ExecutionResult:
    write_harness(repo_path, harness_candidate, filename=harness_candidate.filename)
    return run_harness(
        repo_path,
        command=f"{sys.executable} {harness_candidate.filename}",
    )


def _feedback_for_result(status: str, result: ExecutionResult, fixed_status: str | None = None) -> str:
    parts = [
        f"Execution status: {status}",
        f"stdout:\n{result.stdout.strip()}",
        f"stderr:\n{result.stderr.strip()}",
    ]
    if fixed_status:
        parts.append(f"Fixed repo status: {fixed_status}")
    return "\n\n".join(parts)


def run_feedback_loop(
    *,
    bug_spec: BugSpec,
    retrieved_context: RetrievedContext,
    concise_context: str,
    initial_harness: HarnessCandidate,
    llm_client: BaseLLMClient,
    buggy_repo: Path,
    fixed_repo: Path | None = None,
    max_attempts: int = 3,
) -> PipelineResult:
    harness_candidate = initial_harness
    attempts: list[FeedbackLoopAttempt] = []
    validation: ValidationResult | None = None
    last_buggy_execution: ExecutionResult | None = None
    last_fixed_execution: ExecutionResult | None = None
    next_action = "initial_generation"
    next_note = "Initial harness generation."

    for attempt_index in range(1, max_attempts + 1):
        buggy_execution = _run_once(Path(buggy_repo), harness_candidate)
        buggy_status = classify_execution(buggy_execution)
        fixed_execution = None
        fixed_status = None
        validation = validate_fail_to_pass(buggy_execution)

        if buggy_status == "reproduced" and fixed_repo:
            fixed_execution = _run_once(Path(fixed_repo), harness_candidate)
            fixed_status = classify_execution(fixed_execution)
            validation = validate_fail_to_pass(buggy_execution, fixed_execution)

        attempts.append(
            FeedbackLoopAttempt(
                attempt=attempt_index,
                action=next_action,
                note=next_note,
                harness_candidate=harness_candidate,
                buggy_execution=buggy_execution,
                buggy_status=buggy_status,
                fixed_execution=fixed_execution,
                fixed_status=fixed_status,
            )
        )

        last_buggy_execution = buggy_execution
        last_fixed_execution = fixed_execution

        if validation.success:
            break

        if attempt_index == max_attempts:
            break

        if buggy_status in {"syntax_error", "import_error", "file_error"}:
            harness_candidate = repair_harness(
                concise_context,
                harness_candidate,
                llm_client,
                feedback=_feedback_for_result(buggy_status, buggy_execution),
                strengthen_oracle=False,
            )
            next_action = "repair_harness"
            next_note = f"Repaired harness after {buggy_status}."
            continue

        if buggy_status != "reproduced":
            harness_candidate = repair_harness(
                concise_context,
                harness_candidate,
                llm_client,
                feedback=_feedback_for_result(buggy_status, buggy_execution),
                strengthen_oracle=True,
            )
            next_action = "strengthen_oracle"
            next_note = f"Strengthened oracle after buggy status {buggy_status}."
            continue

        harness_candidate = repair_harness(
            concise_context,
            harness_candidate,
            llm_client,
            feedback=_feedback_for_result(
                buggy_status,
                buggy_execution,
                fixed_status=fixed_status,
            ),
            strengthen_oracle=True,
        )
        next_action = "strengthen_oracle"
        next_note = "Strengthened oracle because Fail-to-Pass was not yet satisfied."

    assert validation is not None
    assert last_buggy_execution is not None
    return PipelineResult(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        harness_candidate=harness_candidate,
        buggy_execution=last_buggy_execution,
        fixed_execution=last_fixed_execution,
        validation=validation,
        attempts=attempts,
    )
