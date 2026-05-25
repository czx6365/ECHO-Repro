from __future__ import annotations

from pathlib import Path
import shlex
import sys

from echo_repro.code_cleaner import clean_generated_python
from echo_repro.environment import EnvironmentRepairManager
from echo_repro.executor import run_harness, write_harness
from echo_repro.llm.base import BaseLLMClient
from echo_repro.models import (
    BugSpec,
    EnvironmentProfileResult,
    ExecutionResult,
    FeedbackLoopAttempt,
    HarnessCandidate,
    LLMCallMetadata,
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
        code = clean_generated_python(llm_client.strengthen_oracle(concise_context, harness_candidate.code, feedback))
        rationale = "Harness oracle strengthened from execution feedback."
    else:
        code = clean_generated_python(llm_client.repair_harness(concise_context, harness_candidate.code, feedback))
        rationale = "Harness repaired from execution feedback."
    return HarnessCandidate(
        filename=harness_candidate.filename,
        code=code,
        rationale=rationale,
    )


def _run_once(repo_path: Path, harness_candidate: HarnessCandidate, python_executable: Path) -> ExecutionResult:
    write_harness(repo_path, harness_candidate, filename=harness_candidate.filename)
    return run_harness(
        repo_path,
        command=f"{shlex.quote(str(python_executable))} {shlex.quote(harness_candidate.filename)}",
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
    initial_prompt_text: str = "",
    initial_llm_metadata: LLMCallMetadata | None = None,
    llm_provider: str = "",
    llm_model: str = "",
    llm_temperature: float | None = None,
    bug_spec_prompt: str = "",
    bug_spec_llm_metadata: LLMCallMetadata | None = None,
    environment_repair_manager: EnvironmentRepairManager | None = None,
    initial_python_executable: Path | None = None,
    environment_profile: EnvironmentProfileResult | None = None,
) -> PipelineResult:
    harness_candidate = initial_harness
    attempts: list[FeedbackLoopAttempt] = []
    validation: ValidationResult | None = None
    last_buggy_execution: ExecutionResult | None = None
    last_fixed_execution: ExecutionResult | None = None
    next_action = "initial_generation"
    next_note = "Initial harness generation."
    current_prompt_text = initial_prompt_text
    current_llm_metadata = initial_llm_metadata or LLMCallMetadata()
    python_executable = Path(initial_python_executable or sys.executable)

    for attempt_index in range(1, max_attempts + 1):
        buggy_execution = _run_once(Path(buggy_repo), harness_candidate, python_executable)
        buggy_status = classify_execution(buggy_execution)
        fixed_execution = None
        fixed_status = None
        environment_repair = None
        validation = validate_fail_to_pass(buggy_execution)

        if buggy_status == "reproduced" and fixed_repo:
            fixed_execution = _run_once(Path(fixed_repo), harness_candidate, python_executable)
            fixed_status = classify_execution(fixed_execution)
            validation = validate_fail_to_pass(buggy_execution, fixed_execution)

        if buggy_status == "dependency_error" and environment_repair_manager:
            environment_repair = environment_repair_manager.repair_dependency(buggy_execution)

        attempts.append(
            FeedbackLoopAttempt(
                attempt=attempt_index,
                action=next_action,
                note=next_note,
                prompt_text=current_prompt_text,
                llm_metadata=current_llm_metadata,
                harness_candidate=harness_candidate,
                buggy_execution=buggy_execution,
                buggy_status=buggy_status,
                fixed_execution=fixed_execution,
                fixed_status=fixed_status,
                environment_repair=environment_repair,
            )
        )

        last_buggy_execution = buggy_execution
        last_fixed_execution = fixed_execution

        if validation.success:
            break

        if attempt_index == max_attempts:
            break

        if buggy_status == "dependency_error" and environment_repair_manager:
            if environment_repair and environment_repair.success and environment_repair.python_path:
                python_executable = environment_repair.python_path
                next_action = "environment_repair"
                next_note = (
                    f"Installed {environment_repair.package} for missing module "
                    f"{environment_repair.missing_module}."
                )
                current_prompt_text = ""
                current_llm_metadata = LLMCallMetadata()
                continue
            break

        if buggy_status in {"dependency_error", "environment_error", "repo_error", "patch_error"}:
            break

        if buggy_status == "harness_error":
            harness_candidate = repair_harness(
                concise_context,
                harness_candidate,
                llm_client,
                feedback=_feedback_for_result(buggy_status, buggy_execution),
                strengthen_oracle=False,
            )
            next_action = "repair_harness"
            next_note = f"Repaired harness after {buggy_status}."
            current_prompt_text = llm_client.last_prompt
            current_llm_metadata = llm_client.last_call_metadata or LLMCallMetadata()
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
            current_prompt_text = llm_client.last_prompt
            current_llm_metadata = llm_client.last_call_metadata or LLMCallMetadata()
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
        current_prompt_text = llm_client.last_prompt
        current_llm_metadata = llm_client.last_call_metadata or LLMCallMetadata()

    assert validation is not None
    assert last_buggy_execution is not None
    return PipelineResult(
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_temperature=llm_temperature,
        bug_spec_prompt=bug_spec_prompt,
        bug_spec_llm_metadata=bug_spec_llm_metadata or LLMCallMetadata(),
        initial_harness_prompt=initial_prompt_text,
        initial_harness_llm_metadata=initial_llm_metadata or LLMCallMetadata(),
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        harness_candidate=harness_candidate,
        buggy_execution=last_buggy_execution,
        fixed_execution=last_fixed_execution,
        validation=validation,
        environment_profile=environment_profile,
        attempts=attempts,
    )
