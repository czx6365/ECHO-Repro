from __future__ import annotations

import json
from typing import Any

from echo_ci.ci_context_retriever import ContextBundle
from echo_ci.workflow_parser import extract_workflow_execution_signal

INPUT_SETTINGS = (
    "S1_issue_only",
    "S2_code_only",
    "S3_log_only",
    "S4_log_workflow",
    "S5_full_context",
    "S6_full_refine",
    "S7_router",
    "S8_router_typed_refine",
    "S9_oracle_router",
)

ROUTER_SETTINGS = {"S7_router", "S8_router_typed_refine", "S9_oracle_router"}
REFINE_SETTINGS = {"S6_full_refine", "S8_router_typed_refine"}
WORKFLOW_AWARE_SETTINGS = {
    "S4_log_workflow",
    "S5_full_context",
    "S6_full_refine",
    "S7_router",
    "S8_router_typed_refine",
    "S9_oracle_router",
}


def _section(title: str, content: str) -> str:
    content = content.strip()
    if not content:
        content = "(not available)"
    return f"## {title}\n{content}"


def _json_section(title: str, payload: dict[str, Any]) -> str:
    return _section(title, json.dumps(payload, ensure_ascii=False, indent=2))


def _tail_lines(text: str, limit: int = 80) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-limit:])


def _workflow_step_for_command(workflow: str, failed_command: str, *, radius: int = 18) -> str:
    if not workflow.strip() or not failed_command.strip():
        return ""
    lines = workflow.splitlines()
    needle = failed_command.strip().lower()
    for index, line in enumerate(lines):
        if needle in line.lower():
            start = max(0, index - radius)
            end = min(len(lines), index + radius + 1)
            return "\n".join(lines[start:end])
    command_head = needle.split()[0] if needle.split() else ""
    if command_head:
        for index, line in enumerate(lines):
            if command_head in line.lower() and ("run:" in line.lower() or command_head in line.lower()):
                start = max(0, index - radius)
                end = min(len(lines), index + radius + 1)
                return "\n".join(lines[start:end])
    return ""


def _strategy_for_failure(failure_spec: dict[str, Any]) -> str:
    failure_type = str(failure_spec.get("failure_type") or "unknown").lower()
    oracle_type = str(failure_spec.get("oracle_type") or "unknown").lower()
    kind = failure_type if failure_type != "unknown" else oracle_type
    failed_command = str(failure_spec.get("failed_command") or "").strip()
    command_note = (
        f"The extracted failed_command is `{failed_command}`. Prefer deriving run.sh from it."
        if failed_command
        else "No reliable failed_command was extracted. Infer the smallest command from the CI log."
    )
    if kind in {"format", "lint"}:
        return "\n".join(
            [
                command_note,
                "For format/lint failures, prefer the exact formatter/linter command from CI logs.",
                "If the command is missing, infer a minimal ruff/flake8/black/isort/mypy/pre-commit command from log keywords.",
                "Avoid installing dependencies. Do not rewrite files. Run in check/diff mode when possible.",
            ]
        )
    if kind == "test" or oracle_type in {"assertion", "test"}:
        return "\n".join(
            [
                command_note,
                "For test failures, prefer the pytest command or minimal pytest target from the log.",
                "If a traceback points to a test file/function, run pytest on that file or node id.",
                "Do not install the whole project unless the failed command itself is an install command.",
            ]
        )
    if kind in {"dependency", "import"} or oracle_type == "dependency":
        return "\n".join(
            [
                command_note,
                "For dependency/import failures, generate a minimal import/version check only when the log shows ImportError, ModuleNotFoundError, or version conflict.",
                "Use python3 -m pip only for lightweight checks if necessary.",
                "Avoid full installation unless the failed command is an install command.",
            ]
        )
    if kind == "build" or oracle_type == "build":
        return "\n".join(
            [
                command_note,
                "For build failures, prefer the failed build command from workflow/log.",
                "Avoid full package installation unless it is exactly the failed command.",
            ]
        )
    if kind in {"workflow", "config"}:
        return "\n".join(
            [
                command_note,
                "For workflow/config failures, generate a minimal command that validates the config or reproduces the command failure.",
                "Do not re-execute the whole GitHub Actions workflow.",
            ]
        )
    return "\n".join(
        [
            command_note,
            "For unknown failures, use failed_command if available.",
            "Otherwise generate the smallest reproduce.py based on error_signature and project interaction.",
        ]
    )


def build_harness_prompt(
    *,
    setting: str,
    context: ContextBundle,
    metadata: dict[str, Any] | None = None,
    refinement_feedback: dict[str, Any] | None = None,
) -> str:
    metadata = metadata or {}
    failure_spec = context.failure_spec
    failed_command = str(failure_spec.get("failed_command") or "").strip()
    workflow_signal = extract_workflow_execution_signal(
        context.workflow,
        command=f"{failed_command}\n{failure_spec.get('error_signature', '')}".strip(),
    )
    grounding_payload = {
        "failed_command": failed_command,
        "failed_step": failure_spec.get("failed_step", ""),
        "error_signature": failure_spec.get("error_signature", ""),
        "failure_type": failure_spec.get("failure_type", "unknown"),
        "oracle_type": failure_spec.get("oracle_type", "unknown"),
        "last_80_ci_log_lines": _tail_lines(context.ci_log, 80),
        "workflow_step_containing_failed_command": _workflow_step_for_command(context.workflow, failed_command),
        "workflow_execution_signal": workflow_signal,
    }
    parts = [
        "You are generating a command-level minimal CI failure reproduction harness.",
        "The goal is not to fix the bug. Generate only files that can show Fail-to-Pass behavior.",
        "Return exactly one JSON object with keys: reproduce.py, run.sh, oracle.json, grounding.",
        "",
        "Hard constraints:",
        "- The harness must be a minimal command-level reproduction, not a full GitHub Actions re-execution.",
        "- reproduce.py must be self-contained and minimal.",
        "- Prefer reusing the failed command extracted from CI logs.",
        "- If failed_command exists, run.sh should be derived from that command unless there is a clear reason not to.",
        "- Do not run pip install, poetry install, apt-get, or docker build unless the CI failure itself is dependency/install/build related.",
        "- Do not modify repository files.",
        "- Do not download external resources.",
        "- Do not fake reproduction with unconditional raise AssertionError, unconditional sys.exit(1), or printing the target error and failing.",
        "- reproduce.py must call project code, a project CLI, existing test logic, or a CI tool command.",
        "- run.sh must execute the real reproduction path.",
        "- The harness must fail on the failing commit and pass on the fixed commit.",
        "- oracle.json must include exit transition, semantic failing signals, fixed-side forbidden signals, oracle_type, and repo-relative path matching.",
        "- oracle must be strict enough to match the target CI failure but not so broad that it fails on the fixed commit.",
        "- grounding must include selected_command, why_this_command, expected_failing_signal, why_fixed_should_pass.",
        "- When workflow_execution_signal is available, preserve its working_directory, target_scope, config_files, and relevant environment in run.sh/reproduce.py.",
        "- JSON-only response. No markdown fences.",
    ]

    parts.extend(
        [
            _json_section("Grounding Signals You Must Use", grounding_payload),
            _section("Failure-Type-Specific Strategy", _strategy_for_failure(failure_spec)),
        ]
    )

    if setting == "S1_issue_only":
        minimal = {
            "metadata": metadata,
            "failure_type": failure_spec.get("failure_type", "unknown"),
            "repo": failure_spec.get("repo", ""),
            "instance_id": failure_spec.get("instance_id", ""),
        }
        parts.append(_json_section("Available Context: Issue/Metadata Only", minimal))
    elif setting == "S2_code_only":
        parts.extend(
            [
                _json_section("FailureSpec", failure_spec),
                _section("Source Snippets", context.source),
                _section("Test Snippets", context.tests),
            ]
        )
    elif setting == "S3_log_only":
        parts.extend(
            [
                _json_section("FailureSpec", failure_spec),
                _section("CI Log Snippet", context.ci_log),
            ]
        )
    elif setting == "S4_log_workflow":
        parts.extend(
            [
                _json_section("FailureSpec", failure_spec),
                _section("CI Log Snippet", context.ci_log),
                _section("Workflow Snippet", context.workflow),
            ]
        )
    elif setting in {"S5_full_context", "S6_full_refine"}:
        parts.extend(
            [
                _json_section("FailureSpec", failure_spec),
                _section("CI Log Snippet", context.ci_log),
                _section("Workflow Snippet", context.workflow),
                _section("Source Snippets", context.source),
                _section("Test Snippets", context.tests),
                _section("Environment/Dependency Snippets", context.env),
            ]
        )
    elif setting in ROUTER_SETTINGS:
        routing_decision = metadata.get("routing_decision") if isinstance(metadata, dict) else None
        parts.append(_json_section("FailureSpec", failure_spec))
        if isinstance(routing_decision, dict):
            parts.append(_json_section("Context Routing Decision", routing_decision))
        for title, content in (
            ("Routed CI Log Snippet", context.ci_log),
            ("Routed Workflow Snippet", context.workflow),
            ("Routed Source Snippets", context.source),
            ("Routed Test Snippets", context.tests),
            ("Routed Environment/Dependency Snippets", context.env),
        ):
            if content.strip():
                parts.append(_section(title, content))
    else:
        raise ValueError(f"Unknown CI-Repair input setting: {setting}")

    if refinement_feedback:
        parts.append(_json_section("Previous Attempt Feedback", refinement_feedback))
        if refinement_feedback.get("feedback_style") == "failure_type_aware":
            parts.append(
                _section(
                    "Typed Feedback Policy",
                    "Follow typed_feedback_action and repair_instructions first. Do not treat dependency, environment, target, oracle, and fake-reproduction failures as interchangeable reflection failures.",
                )
            )
        parts.append(
            _section(
                "Regeneration Instruction",
                "Fix every listed issue. Keep the harness minimal and command-level. Do not add installation or setup unless the failed command itself is an install/build command. Reuse failed_command when available.",
            )
        )

    parts.append(
        _section(
            "Required oracle.json shape",
            json.dumps(
                {
                    "failing_exit_nonzero": True,
                    "fixed_exit_zero": True,
                    "expected_failing_exit_code": "nonzero",
                    "expected_passing_exit_code": 0,
                    "any_failing_signal": ["one of several stable semantic signals"],
                    "failing_signal_match": "any",
                    "must_contain_on_failing": ["short signature if available"],
                    "must_not_contain_on_fixed": ["same signature"],
                    "path_match_mode": "repo_relative",
                    "oracle_type": "exception|assertion|lint|format|dependency|build|test|unknown",
                    "grounding": {
                        "selected_command": "the exact command or minimal command chosen",
                        "why_this_command": "why it matches the CI failure",
                        "expected_failing_signal": "specific output/exit behavior on failing commit",
                        "why_fixed_should_pass": "why the fixed commit should not show the signal",
                    },
                },
                indent=2,
            ),
        )
    )
    return "\n\n".join(parts).strip() + "\n"
