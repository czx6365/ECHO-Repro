from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from echo_ci.ci_context_retriever import retrieve_context_bundle, save_context_bundle
from echo_ci.context_router import route_context, write_routing_decision
from echo_ci.ci_repair_loader import DEFAULT_HF_NAME, load_instances
from echo_ci.env_policy import validate_env_policy
from echo_ci.failure_spec import extract_failure_spec
from echo_ci.harness_executor import execute_generated_harness
from echo_ci.harness_generator import generate_harness, preserve_workflow_execution_signals
from echo_ci.harness_validator import validate_harness
from echo_ci.metrics import summarize_result_dir
from echo_ci.prompts import INPUT_SETTINGS, REFINE_SETTINGS, ROUTER_SETTINGS
from echo_ci.repo_manager import RobustRepoManager
from echo_ci.schema_utils import field, get_field, normalize_failure_type, safe_instance_id
from echo_ci.typed_refiner import build_typed_feedback


def parse_settings(value: str) -> list[str]:
    settings = [item.strip() for item in value.split(",") if item.strip()]
    invalid = [setting for setting in settings if setting not in INPUT_SETTINGS]
    if invalid:
        raise ValueError(f"Unknown settings: {', '.join(invalid)}")
    return settings


def prepare_context_repo(instance: dict[str, Any], work_dir: Path, *, clone_context: bool) -> tuple[Path | None, str]:
    local_repo = get_field(instance, ["local_repo_path", "repo_path", "checkout_path"], default=None)
    if local_repo and Path(str(local_repo)).exists():
        return Path(str(local_repo)), ""
    if not clone_context:
        return None, "Context clone disabled."
    repo = str(field(instance, "repo", default=""))
    if not repo:
        return None, "Missing repo field."
    failing_commit = str(field(instance, "failing_commit", default=""))
    manager = RobustRepoManager(
        cache_dir=Path(work_dir) / "repo_cache",
        work_dir=Path(work_dir) / "context",
        timeout=300,
    )
    repo_path = Path(work_dir) / "context" / safe_instance_id(instance)
    checkout = manager.checkout_commit(repo, failing_commit, repo_path)
    if not checkout.success:
        return repo_path if repo_path.exists() else None, checkout.checkout_error
    return repo_path, ""


def copy_generated_to_root(attempt_generated: Path, root_generated: Path) -> None:
    if attempt_generated.resolve() == root_generated.resolve():
        return
    root_generated.mkdir(parents=True, exist_ok=True)
    for name in ("reproduce.py", "run.sh", "oracle.json", "prompt.txt", "generation_metadata.json"):
        src = attempt_generated / name
        if src.exists():
            shutil.copy2(src, root_generated / name)


def write_final_result(
    *,
    output_dir: Path,
    instance: dict[str, Any],
    setting: str,
    failure_spec: dict[str, Any],
    generation: dict[str, Any],
    execution: dict[str, Any],
    attempts: list[dict[str, Any]] | None = None,
    context_error: str = "",
    routing_decision: dict[str, Any] | None = None,
) -> None:
    payload = {
        "instance_id": failure_spec.get("instance_id") or safe_instance_id(instance),
        "repo": failure_spec.get("repo") or field(instance, "repo", default=""),
        "setting": setting,
        "failure_type": failure_spec.get("failure_type", "unknown"),
        "env_policy": execution.get("env_policy", ""),
        "failure_spec": failure_spec,
        "generation": generation,
        "execution": execution,
        "classification": execution.get("classification", "unknown"),
        "attempts": len(attempts or []) or 1,
        "attempts_detail": attempts or [],
        "context_error": context_error,
    }
    if routing_decision:
        payload["routing_decision"] = routing_decision
        payload["selected_context_sources"] = routing_decision.get("selected_context_sources", [])
        payload["context_size_chars"] = routing_decision.get("routed_context_size_chars", 0)
    (output_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_instance_setting(
    *,
    instance: dict[str, Any],
    setting: str,
    output_root: Path,
    work_dir: Path,
    timeout_seconds: int,
    max_log_chars: int,
    max_snippet_chars: int,
    max_attempts: int,
    clone_context: bool,
    reuse_generated_from: Path | None = None,
    env_policy: str = "E0_no_setup",
    cleanup_worktrees: bool = False,
) -> dict[str, Any]:
    instance_id = safe_instance_id(instance)
    output_dir = Path(output_root) / instance_id / setting
    output_dir.mkdir(parents=True, exist_ok=True)
    env_policy = validate_env_policy(env_policy)

    failure_spec = extract_failure_spec(instance)
    failure_spec.write_json(output_dir / "failure_spec.json")

    needs_repo_context = setting in {"S2_code_only", "S5_full_context", "S6_full_refine"} or setting in ROUTER_SETTINGS
    repo_path, context_error = prepare_context_repo(
        instance,
        work_dir,
        clone_context=clone_context and needs_repo_context,
    )
    full_context = retrieve_context_bundle(
        instance=instance,
        failure_spec=failure_spec,
        repo_path=repo_path,
        max_log_chars=max_log_chars,
        max_snippet_chars=max_snippet_chars,
    )
    routing_decision_payload: dict[str, Any] | None = None
    if setting in ROUTER_SETTINGS:
        save_context_bundle(full_context, output_dir / "context_full")
        override = None
        route_type = "failure_type"
        if setting == "S9_oracle_router":
            override = normalize_failure_type(field(instance, "failure_type", default=None))
            route_type = "oracle_label"
        context, routing_decision = route_context(
            full_context,
            route_type=route_type,
            failure_type_override=override,
        )
        routing_decision_payload = routing_decision.to_dict()
        save_context_bundle(context, output_dir / "context")
        write_routing_decision(routing_decision, output_dir / "context")
    else:
        context = full_context
        save_context_bundle(context, output_dir / "context")

    attempts: list[dict[str, Any]] = []
    root_generated = output_dir / "generated"
    feedback: dict[str, Any] | None = None
    loop_attempts = max_attempts if setting in REFINE_SETTINGS else 1
    final_generation: dict[str, Any] = {}
    final_execution: dict[str, Any] = {}
    prompt_metadata = dict(instance)
    extra_generation_metadata: dict[str, Any] = {}
    if routing_decision_payload:
        prompt_metadata["routing_decision"] = routing_decision_payload
        extra_generation_metadata["routing_decision"] = routing_decision_payload

    for attempt_index in range(1, loop_attempts + 1):
        generated_dir = (
            output_dir / "attempts" / f"attempt_{attempt_index}" / "generated"
            if setting in REFINE_SETTINGS
            else root_generated
        )
        if reuse_generated_from is not None:
            source_generated = Path(reuse_generated_from) / instance_id / setting / "generated"
            generated_dir.mkdir(parents=True, exist_ok=True)
            missing = []
            for name in ("reproduce.py", "run.sh", "oracle.json", "generation_metadata.json"):
                src = source_generated / name
                if src.exists():
                    shutil.copy2(src, generated_dir / name)
                elif name != "generation_metadata.json":
                    missing.append(name)
            if missing:
                generation = generate_harness(
                    setting=setting,
                    context=context,
                    output_dir=generated_dir,
                    metadata=prompt_metadata,
                    refinement_feedback=feedback,
                    extra_generation_metadata=extra_generation_metadata,
                )
                generation_payload = generation.to_dict()
                generation_payload.setdefault("validation", {}).setdefault("warnings", [])
                generation_payload["validation"]["warnings"].append(
                    f"reuse_missing_generated_artifacts:{','.join(missing)}"
                )
                generation_payload["reused_generated_from"] = ""
                generation_status = generation.status
            else:
                try:
                    reused_metadata = json.loads(
                        (generated_dir / "generation_metadata.json").read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    reused_metadata = {}
                grounding = reused_metadata.get("grounding") if isinstance(reused_metadata, dict) else {}
                grounding = grounding if isinstance(grounding, dict) else {}
                reproduce_text = (generated_dir / "reproduce.py").read_text(encoding="utf-8", errors="replace")
                run_text = (generated_dir / "run.sh").read_text(encoding="utf-8", errors="replace")
                oracle = json.loads((generated_dir / "oracle.json").read_text(encoding="utf-8"))
                run_text, adjustments = preserve_workflow_execution_signals(
                    run_text=run_text,
                    reproduce_text=reproduce_text,
                    context=context,
                    grounding=grounding,
                    setting=setting,
                )
                if adjustments:
                    (generated_dir / "run.sh").write_text(run_text, encoding="utf-8")
                    (generated_dir / "run.sh").chmod(0o755)
                    grounding = dict(grounding)
                    grounding["execution_signal_adjustments"] = adjustments
                validation = validate_harness(
                    reproduce_text=reproduce_text,
                    run_text=run_text,
                    oracle=oracle,
                    context=context,
                    setting=setting,
                    grounding=grounding,
                )
                generation_payload = {
                    "status": "generated" if validation["valid"] else "validation_error",
                    "setting": setting,
                    "output_dir": str(generated_dir),
                    "prompt_path": str(source_generated / "prompt.txt"),
                    "reproduce_path": str(generated_dir / "reproduce.py"),
                    "run_path": str(generated_dir / "run.sh"),
                    "oracle_path": str(generated_dir / "oracle.json"),
                    "metadata_path": str(generated_dir / "generation_metadata.json"),
                    "error": "",
                    "llm_metadata": {},
                    "validation": {
                        **validation,
                        "warnings": [*validation.get("warnings", []), "reused_generated_artifacts"],
                    },
                    "validation_attempts": [],
                    "grounding": grounding,
                    "reused_generated_from": str(source_generated),
                }
                if routing_decision_payload:
                    generation_payload["routing_decision"] = routing_decision_payload
                generation_status = generation_payload["status"]
                if generation_status == "validation_error":
                    if os.environ.get("OPENAI_API_KEY"):
                        generation = generate_harness(
                            setting=setting,
                            context=context,
                            output_dir=generated_dir,
                            metadata=prompt_metadata,
                            refinement_feedback={
                                "validation_failed": True,
                                "issues": validation["issues"],
                                "warnings": validation["warnings"],
                                "previous_grounding": grounding,
                            },
                            extra_generation_metadata=extra_generation_metadata,
                        )
                        generation_payload = generation.to_dict()
                        generation_payload["reused_generated_from"] = str(source_generated)
                        generation_status = generation.status
                    else:
                        generation_payload["error"] = (
                            "Reused harness failed validation and OPENAI_API_KEY is unset; "
                            "regeneration was not attempted."
                        )
                        generation_payload["validation"]["warnings"].append(
                            "regeneration_unavailable_no_llm"
                        )
        else:
            generation = generate_harness(
                setting=setting,
                context=context,
                output_dir=generated_dir,
                metadata=prompt_metadata,
                refinement_feedback=feedback,
                extra_generation_metadata=extra_generation_metadata,
            )
            generation_payload = generation.to_dict()
            generation_status = generation.status
        if routing_decision_payload:
            generation_payload["routing_decision"] = routing_decision_payload
        if generation_status != "generated" and generation_status != "skipped_no_llm":
            execution_payload = {
                "status": generation_status,
                "classification": "harness_error",
                "env_policy": env_policy,
                "error": generation_payload.get("error", ""),
            }
        else:
            execution_instance = dict(instance)
            # The raw benchmark row does not expose these normalized fields.
            # Carry the extracted failure evidence into execution so workflow
            # signal diagnostics rank the actual CI command ahead of a later
            # generated approximation.
            execution_instance["failed_command"] = failure_spec.failed_command
            execution_instance["failed_step"] = failure_spec.failed_step
            execution_payload = execute_generated_harness(
                instance=execution_instance,
                generated_dir=generated_dir,
                output_dir=(output_dir / "attempts" / f"attempt_{attempt_index}" if setting in REFINE_SETTINGS else output_dir),
                work_dir=Path(work_dir) / "execution" / setting,
                timeout_seconds=timeout_seconds,
                generation_status=generation_status,
                env_policy=env_policy,
                cleanup_worktrees=cleanup_worktrees,
            )
        attempts.append(
            {
                "attempt": attempt_index,
                "generation": generation_payload,
                "execution": execution_payload,
            }
        )
        final_generation = generation_payload
        final_execution = execution_payload
        copy_generated_to_root(generated_dir, root_generated)

        if execution_payload.get("classification") == "reproduced":
            break
        if generation_status == "skipped_no_llm":
            break
        if attempt_index < loop_attempts:
            if setting == "S8_router_typed_refine":
                feedback = build_typed_feedback(execution_payload)
            else:
                feedback = {
                    "classification": execution_payload.get("classification"),
                    "failing": execution_payload.get("failing"),
                    "fixed": execution_payload.get("fixed"),
                    "fake_reproduction_reason": execution_payload.get("fake_reproduction_reason", ""),
                }
            attempts[-1]["feedback_for_next_attempt"] = feedback

    write_final_result(
        output_dir=output_dir,
        instance=instance,
        setting=setting,
        failure_spec=failure_spec.to_dict(),
        generation=final_generation,
        execution=final_execution,
        attempts=attempts,
        context_error=context_error,
        routing_decision=routing_decision_payload,
    )
    return final_execution


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CI-Repair-Bench MVP reproduction experiment.")
    parser.add_argument("--dataset", "--dataset_path", dest="dataset_path", type=Path, default=None)
    parser.add_argument("--hf_name", default=DEFAULT_HF_NAME)
    parser.add_argument("--split", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--instance_ids", default="", help="Comma-separated instance ids for targeted replay.")
    parser.add_argument("--setting", default=None, help="Single setting alias.")
    parser.add_argument("--settings", default="S5_full_context")
    parser.add_argument("--output_dir", type=Path, default=Path("outputs/ci_repair_mvp"))
    parser.add_argument("--work_dir", type=Path, default=Path("work/ci_repair"))
    parser.add_argument("--timeout_seconds", type=int, default=120)
    parser.add_argument("--max_log_chars", type=int, default=20_000)
    parser.add_argument("--max_snippet_chars", type=int, default=1_500)
    parser.add_argument("--max_attempts", type=int, default=4)
    parser.add_argument("--clone_context", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--reuse_generated_from", type=Path, default=None)
    parser.add_argument("--env_policy", choices=["E0_no_setup", "E1_pythonpath_only", "E2_tool_bootstrap"], default="E0_no_setup")
    parser.add_argument(
        "--cleanup_worktrees",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Remove failing/fixed execution checkouts after persisting diagnostics.",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Skip instance/setting pairs whose top-level result.json already exists.",
    )
    parser.add_argument("--summarize", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    settings = parse_settings(args.setting or args.settings)
    instances = load_instances(
        dataset_path=args.dataset_path,
        hf_name=args.hf_name,
        split=args.split,
        limit=args.limit,
    )
    if args.instance_ids:
        requested_ids = {value.strip() for value in args.instance_ids.split(",") if value.strip()}
        instances = [instance for instance in instances if safe_instance_id(instance) in requested_ids]
        missing_ids = requested_ids - {safe_instance_id(instance) for instance in instances}
        if missing_ids:
            raise ValueError(f"Unknown instance id(s): {', '.join(sorted(missing_ids))}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for index, instance in enumerate(instances, start=1):
        instance_id = safe_instance_id(instance, index)
        for setting in settings:
            print(f"[{index}/{len(instances)}] {instance_id} {setting}")
            existing_result = args.output_dir / instance_id / setting / "result.json"
            if args.resume and existing_result.is_file():
                print("  skipped=existing_result")
                continue
            try:
                result = run_instance_setting(
                    instance=instance,
                    setting=setting,
                    output_root=args.output_dir,
                    work_dir=args.work_dir,
                    timeout_seconds=args.timeout_seconds,
                    max_log_chars=args.max_log_chars,
                    max_snippet_chars=args.max_snippet_chars,
                    max_attempts=args.max_attempts,
                    clone_context=args.clone_context,
                    reuse_generated_from=args.reuse_generated_from,
                    env_policy=args.env_policy,
                    cleanup_worktrees=args.cleanup_worktrees,
                )
                print(f"  classification={result.get('classification')}")
            except Exception as exc:
                error_dir = args.output_dir / instance_id / setting
                error_dir.mkdir(parents=True, exist_ok=True)
                payload = {
                    "instance_id": instance_id,
                    "setting": setting,
                    "classification": "unknown",
                    "error": str(exc),
                }
                (error_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  error={exc}")

    if args.summarize:
        summary = summarize_result_dir(args.output_dir)
        print(json.dumps(summary["overall"], indent=2))


if __name__ == "__main__":
    main()
