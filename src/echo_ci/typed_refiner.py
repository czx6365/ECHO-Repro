from __future__ import annotations

from typing import Any


def _last_text(value: str, limit: int = 2400) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[-limit:]


def _phase_evidence(execution: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(execution, dict):
        return {}
    return {
        "exit_code": execution.get("exit_code"),
        "error_kind": execution.get("error_kind", ""),
        "error": _last_text(str(execution.get("error") or ""), 1200),
        "stdout_tail": _last_text(str(execution.get("stdout") or "")),
        "stderr_tail": _last_text(str(execution.get("stderr") or "")),
        "target_preflight": execution.get("target_preflight") or {},
        "dependency_diagnosis": execution.get("dependency_diagnosis") or {},
        "workflow_signal": execution.get("workflow_signal") or {},
    }


def _action_for_classification(classification: str) -> tuple[str, list[str]]:
    if classification == "fake_reproduction":
        return (
            "replace_fake_reproduction",
            [
                "Remove unconditional failure, hard-coded target error output, and synthetic AssertionError/sys.exit paths.",
                "The next harness must execute a real project command, project import path, existing test, or CI tool.",
                "Keep oracle signals tied to observed command output rather than strings printed by the harness itself.",
            ],
        )
    if classification == "oracle_error":
        return (
            "repair_oracle",
            [
                "The failing commit produced a non-zero result and the fixed commit passed, but the oracle did not match the target signal.",
                "Keep the command stable and revise oracle.json to use specific semantic failing signals from failing stdout/stderr.",
                "Avoid generic signals such as error, failed, exception, or traceback unless paired with a specific file, rule, or assertion.",
            ],
        )
    if classification == "buggy_pass":
        return (
            "strengthen_trigger",
            [
                "The harness did not trigger the failure on the failing commit.",
                "Prefer the extracted failed command, workflow target, pytest node id, linter target, or traceback path.",
                "Do not broaden into a full workflow run; choose the smallest command that should fail on the buggy commit.",
            ],
        )
    if classification == "buggy_fail_fixed_fail":
        return (
            "separate_bug_from_environment",
            [
                "Both commits failed, so the command may not correspond to the fixed behavior or the environment may be incomplete.",
                "Check whether fixed-side output is dependency/setup noise, wrong working directory, deleted/renamed target, or an over-broad oracle.",
                "Prefer a narrower target and avoid adding package installation unless the original CI failure is install/build related.",
            ],
        )
    if classification.startswith(("fixed_missing_third_party_package", "fixed_missing_project_package", "fixed_dependency", "fixed_package_version")):
        return (
            "repair_dependency_context",
            [
                "The fixed commit failed because the harness or command depends on packages that are not available in the controlled environment.",
                "Avoid blind pip install in run.sh. Prefer the original CI tool command, a lighter import/version check, or dependency evidence from manifests/workflow.",
                "If the missing module is the project package, use the repository-local source path or existing test command instead of importing an uninstalled distribution.",
            ],
        )
    if classification.startswith("fixed_missing_ci_tool") or "tool_resolution" in classification:
        return (
            "repair_ci_tool_command",
            [
                "A CI tool was unresolved or invoked incorrectly.",
                "Use a supported CI tool command only when the log/workflow shows that tool, and keep the target/config path explicit.",
                "Do not add host-global installs; rely on the experiment environment policy to bootstrap allowed CI tools.",
            ],
        )
    if classification.startswith(("fixed_wrong_workdir", "fixed_path_unknown", "fixed_wrong_run_sh_path")):
        return (
            "repair_workdir_or_harness_path",
            [
                "The fixed-side failure points to an invalid working directory or harness path.",
                "Preserve workflow working-directory when available and call $REPO_ROOT/.echo_repro/reproduce.py from run.sh.",
                "Avoid absolute work/output paths that only exist in one checkout.",
            ],
        )
    if classification.startswith(("fixed_missing_project_target_file", "fixed_target_hallucinated", "fixed_target_deleted", "fixed_target_renamed")):
        return (
            "repair_target_selection",
            [
                "The harness references a target file that is missing, renamed, deleted, or hallucinated on the fixed commit.",
                "Choose a target grounded in the CI log, workflow command, traceback, or cross-commit target preflight.",
                "If the fix deletes/renames the target with evidence, make the fixed-side oracle accept that transition rather than failing on FileNotFoundError.",
            ],
        )
    if classification.startswith("failing_checkout") or classification.startswith("fixed_checkout"):
        return (
            "record_benchmark_preparation_issue",
            [
                "Checkout failed before harness behavior could be evaluated.",
                "Do not compensate by changing the harness; keep the failure recorded as benchmark/preparation noise.",
            ],
        )
    if classification in {"harness_error", "fixed_command_fail", "unknown"} or classification.startswith("failing_"):
        return (
            "repair_harness_execution",
            [
                "Use the concrete failing/fixed stderr tails to repair syntax, path, command, or target selection errors.",
                "Keep the harness command-level and grounded in the extracted CI failure.",
                "Preserve the fail-on-buggy/pass-on-fixed contract.",
            ],
        )
    return (
        "generic_typed_repair",
        [
            "Repair the previous attempt using the classified execution outcome.",
            "Keep the harness minimal, grounded in CI evidence, and free of fake reproduction.",
        ],
    )


def build_typed_feedback(execution_payload: dict[str, Any]) -> dict[str, Any]:
    classification = str(execution_payload.get("classification") or "unknown")
    action, instructions = _action_for_classification(classification)
    return {
        "feedback_style": "failure_type_aware",
        "classification": classification,
        "typed_feedback_action": action,
        "repair_instructions": instructions,
        "failing": _phase_evidence(execution_payload.get("failing")),
        "fixed": _phase_evidence(execution_payload.get("fixed")),
        "fake_reproduction_reason": execution_payload.get("fake_reproduction_reason", ""),
        "target_preflight": execution_payload.get("target_preflight") or {},
        "workflow_signal": execution_payload.get("workflow_signal") or {},
    }
