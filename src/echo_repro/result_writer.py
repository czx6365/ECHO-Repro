from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path

from echo_repro.models import FeedbackLoopAttempt, PipelineResult, PreparedRepos
from echo_repro.validator import classify_execution

SCHEMA_VERSION = "0.2"


def _stage_for_action(action: str) -> str:
    if action == "initial_generation":
        return "initial_generation"
    if action == "repair_harness":
        return "repair"
    if action == "strengthen_oracle":
        return "strengthen_oracle"
    return action


def _prompt_filename(action: str, attempt: int) -> str:
    if action == "initial_generation":
        return f"generate_attempt_{attempt}.md"
    if action == "repair_harness":
        return f"repair_attempt_{attempt}.md"
    if action == "strengthen_oracle":
        return f"strengthen_oracle_attempt_{attempt}.md"
    return f"{action}_attempt_{attempt}.md"


def _attempt_records(result: PipelineResult) -> list[FeedbackLoopAttempt]:
    if result.attempts:
        return result.attempts
    synthesized = FeedbackLoopAttempt(
        attempt=1,
        action="initial_generation",
        note="Initial harness generation.",
        prompt_text=result.initial_harness_prompt,
        llm_metadata=result.initial_harness_llm_metadata,
        harness_candidate=result.harness_candidate,
        buggy_execution=result.buggy_execution,
        buggy_status=classify_execution(result.buggy_execution),
        fixed_execution=result.fixed_execution,
        fixed_status=classify_execution(result.fixed_execution) if result.fixed_execution else None,
    )
    return [synthesized]


def write_experiment_record(
    *,
    instance: dict,
    prepared: PreparedRepos,
    result: PipelineResult,
    max_attempts: int | None,
    retrieval_mode: str = "keyword",
    executor: str = "subprocess",
    output_root: Path = Path("outputs"),
) -> Path:
    output_dir = Path(output_root) / prepared.instance_id
    prompts_dir = output_dir / "prompts"
    attempts_dir = output_dir / "attempts"
    output_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)
    attempts_dir.mkdir(parents=True, exist_ok=True)

    concise_context_path = output_dir / "concise_context.md"
    concise_context_path.write_text(result.concise_context, encoding="utf-8")

    final_harness_path = output_dir / "final_reproduce.py"
    final_harness_path.write_text(result.harness_candidate.code, encoding="utf-8")

    bug_spec_prompt_path = prompts_dir / "bug_spec.md"
    bug_spec_prompt_path.write_text(result.bug_spec_prompt, encoding="utf-8")

    attempts = _attempt_records(result)
    attempt_summaries = []
    attempts_jsonl_path = output_dir / "attempts.jsonl"
    with attempts_jsonl_path.open("w", encoding="utf-8") as handle:
        for attempt in attempts:
            prompt_path = prompts_dir / _prompt_filename(attempt.action, attempt.attempt)
            prompt_path.write_text(attempt.prompt_text, encoding="utf-8")

            harness_path = attempts_dir / f"reproduce_attempt_{attempt.attempt}.py"
            harness_path.write_text(attempt.harness_candidate.code, encoding="utf-8")

            record = {
                "attempt_id": attempt.attempt,
                "stage": _stage_for_action(attempt.action),
                "prompt_path": str(prompt_path),
                "harness_path": str(harness_path),
                "buggy_execution": {
                    "status": attempt.buggy_status,
                    "returncode": attempt.buggy_execution.returncode,
                    "stdout": attempt.buggy_execution.stdout,
                    "stderr": attempt.buggy_execution.stderr,
                },
                "fixed_execution": (
                    {
                        "status": attempt.fixed_status,
                        "returncode": attempt.fixed_execution.returncode if attempt.fixed_execution else None,
                        "stdout": attempt.fixed_execution.stdout if attempt.fixed_execution else "",
                        "stderr": attempt.fixed_execution.stderr if attempt.fixed_execution else "",
                    }
                    if attempt.fixed_execution
                    else None
                ),
                "llm_metadata": attempt.llm_metadata.model_dump(mode="json"),
            }
            handle.write(json.dumps(record) + "\n")
            attempt_summaries.append(record)

    final_buggy_status = classify_execution(result.buggy_execution)
    final_fixed_status = classify_execution(result.fixed_execution) if result.fixed_execution else None
    final_result = {
        "buggy_status": final_buggy_status,
        "fixed_status": final_fixed_status,
        "is_f2p": result.validation.success,
        "failure_category": None if result.validation.success else final_buggy_status,
        "reason": result.validation.summary,
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "instance_metadata": {
            "instance_id": prepared.instance_id,
            "repo": prepared.repo,
            "base_commit": prepared.base_commit,
            "patch_applied": prepared.patch_applied,
        },
        "run_config": {
            "llm_provider": result.llm_provider,
            "model": result.llm_model,
            "temperature": result.llm_temperature,
            "max_attempts": max_attempts,
            "retrieval_mode": retrieval_mode,
            "executor": executor,
        },
        "bug_spec": result.bug_spec.model_dump(mode="json"),
        "retrieval": {
            "source_files": [str(path) for path in result.retrieved_context.source_files],
            "test_files": [str(path) for path in result.retrieved_context.test_files],
            "env_files": [str(path) for path in result.retrieved_context.env_files],
        },
        "artifacts": {
            "concise_context_path": str(concise_context_path),
            "final_harness_path": str(final_harness_path),
            "attempts_path": str(attempts_jsonl_path),
            "prompts_dir": str(prompts_dir),
        },
        "attempts_summary": attempt_summaries,
        "final_result": final_result,
    }
    result_json_path = output_dir / "result.json"
    result_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return result_json_path
