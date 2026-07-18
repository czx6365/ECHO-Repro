from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from echo_ci.result_classifier import CommandExecution, classify_failing_error, classify_fixed_error


def find_result_files(result_dir: Path) -> list[Path]:
    paths = []
    for path in sorted(Path(result_dir).rglob("result.json")):
        if path.parent.name == "execution":
            continue
        if path.parent.parent.name.startswith("attempt_"):
            continue
        paths.append(path)
    return paths


def load_results(result_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in find_result_files(result_dir):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "classification" not in payload and "execution" not in payload:
            continue
        payload["_path"] = str(path)
        rows.append(payload)
    return rows


def flatten_result(payload: dict[str, Any]) -> dict[str, Any]:
    execution = payload.get("execution") or payload
    generation = payload.get("generation") or {}
    failure_spec = payload.get("failure_spec") or {}
    failing = execution.get("failing") or {}
    fixed = execution.get("fixed") or {}
    validation = generation.get("validation") or {}
    preservation = validation.get("workflow_signal_preservation") or {}
    routing_decision = payload.get("routing_decision") or generation.get("routing_decision") or {}
    target_preflight = execution.get("target_preflight") or payload.get("target_preflight") or {}
    grounding = generation.get("grounding") or {}
    selected_command = str(grounding.get("selected_command") or "")
    failed_command = str(failure_spec.get("failed_command") or "")
    duration = (failing.get("duration") or 0) + (fixed.get("duration") or 0)
    raw_classification = execution.get("classification") or payload.get("classification") or "unknown"
    fixed_error_subtype = execution.get("fixed_error_subtype") or payload.get("fixed_error_subtype") or ""
    fixed_error_reason = execution.get("fixed_error_reason") or payload.get("fixed_error_reason") or ""
    failing_error_subtype = execution.get("failing_error_subtype") or payload.get("failing_error_subtype") or ""
    failing_error_reason = execution.get("failing_error_reason") or payload.get("failing_error_reason") or ""
    failing_execution = CommandExecution(
        phase="failing",
        exit_code=failing.get("exit_code"),
        stdout=failing.get("stdout") or "",
        stderr=failing.get("stderr") or "",
        duration=float(failing.get("duration") or 0),
        timeout=bool(failing.get("timeout")),
        error=failing.get("error") or "",
        matched_oracle=bool(failing.get("matched_oracle")),
        error_kind=failing.get("error_kind") or "",
        preflight=failing.get("preflight"),
        env_setup=failing.get("env_setup"),
        target_preflight=failing.get("target_preflight"),
        dependency_diagnosis=failing.get("dependency_diagnosis"),
        workflow_signal=failing.get("workflow_signal"),
        project_name=execution.get("repo") or payload.get("repo") or failure_spec.get("repo") or "",
    ) if failing else None
    reclassifiable_fixed = {
        "fixed_error",
        "fixed_harness_path_error",
        "fixed_path_unknown",
        "fixed_wrong_run_sh_path",
        "fixed_wrong_workdir",
        "fixed_missing_project_target_file",
        "fixed_dependency_error",
        "dependency_error",
    }
    if raw_classification in reclassifiable_fixed:
        if failing_execution and failing_execution.error and not fixed:
            failing_error_subtype, failing_error_reason = classify_failing_error(failing_execution)
        else:
            fixed_execution = CommandExecution(
                phase="fixed",
                exit_code=fixed.get("exit_code"),
                stdout=fixed.get("stdout") or "",
                stderr=fixed.get("stderr") or "",
                duration=float(fixed.get("duration") or 0),
                timeout=bool(fixed.get("timeout")),
                error=fixed.get("error") or "",
                matched_oracle=bool(fixed.get("matched_oracle")),
                error_kind=fixed.get("error_kind") or "",
                preflight=fixed.get("preflight"),
                env_setup=fixed.get("env_setup"),
                target_preflight=fixed.get("target_preflight"),
                dependency_diagnosis=fixed.get("dependency_diagnosis"),
                workflow_signal=fixed.get("workflow_signal"),
                project_name=execution.get("repo") or payload.get("repo") or failure_spec.get("repo") or "",
            ) if fixed else None
            fixed_error_subtype, fixed_error_reason = classify_fixed_error(fixed_execution)
    if raw_classification in reclassifiable_fixed and failing_error_subtype:
        classification = failing_error_subtype
    elif raw_classification in reclassifiable_fixed and fixed_error_subtype:
        classification = fixed_error_subtype
    else:
        classification = raw_classification
    workflow_signal_expected = bool(preservation.get("command_from_workflow"))
    workflow_signal_preserved = (
        bool(preservation.get("preserved")) if workflow_signal_expected else None
    )
    workdir_expected = preservation.get("working_directory_preserved") is not None
    workdir_recovered = (
        bool(preservation.get("working_directory_preserved")) if workdir_expected else None
    )
    normalized_selected = " ".join(selected_command.lower().split())
    normalized_failed = " ".join(failed_command.lower().split())
    failed_command_grounded = bool(
        normalized_selected
        and normalized_failed
        and (
            normalized_selected in normalized_failed
            or normalized_failed in normalized_selected
            or normalized_selected.split()[0] == normalized_failed.split()[0]
        )
    )
    command_traceable = failed_command_grounded or workflow_signal_expected
    oracle_payload = execution.get("oracle") or payload.get("oracle") or {}
    oracle_signals = oracle_payload.get("any_failing_signal") or oracle_payload.get("must_contain_on_failing") or []
    oracle_valid = bool(oracle_signals) and raw_classification != "oracle_error"
    grounded = bool(
        selected_command
        and command_traceable
        and oracle_signals
        and (workflow_signal_preserved is not False)
    )
    environment_failure = any(
        marker in str(classification)
        for marker in ("missing_ci_tool", "tool_resolution", "environment_error", "python_version_mismatch")
    )
    selected_context_sources = routing_decision.get("selected_context_sources") or []
    if isinstance(selected_context_sources, str):
        selected_context_sources = [selected_context_sources]
    typed_feedback_actions: list[str] = []
    for attempt in payload.get("attempts_detail") or []:
        feedback = attempt.get("feedback_for_next_attempt") if isinstance(attempt, dict) else None
        if isinstance(feedback, dict) and feedback.get("typed_feedback_action"):
            typed_feedback_actions.append(str(feedback["typed_feedback_action"]))
    return {
        "instance_id": payload.get("instance_id") or failure_spec.get("instance_id") or "",
        "repo": payload.get("repo") or failure_spec.get("repo") or execution.get("repo") or "",
        "setting": payload.get("setting") or generation.get("setting") or "unknown",
        "env_policy": execution.get("env_policy") or payload.get("env_policy") or "E0_no_setup",
        "failure_type": payload.get("failure_type") or failure_spec.get("failure_type") or "unknown",
        "generation_status": generation.get("status") or payload.get("generation_status") or "",
        "classification": classification,
        "raw_classification": raw_classification,
        "fixed_error_subtype": fixed_error_subtype,
        "fixed_error_reason": fixed_error_reason,
        "failing_error_subtype": failing_error_subtype,
        "failing_error_reason": failing_error_reason,
        "f_to_p": classification == "reproduced",
        "fake": classification == "fake_reproduction" or execution.get("fake_reproduction_detected") is True,
        "timeout": classification == "timeout",
        "buggy_failed": bool(failing) and failing.get("exit_code") not in (0, None),
        "fixed_passed": bool(fixed) and fixed.get("exit_code") == 0,
        "executable": bool(failing) and not failing.get("timeout") and not failing.get("error"),
        "attempts": payload.get("attempts", 1),
        "duration": duration,
        "token_cost": (generation.get("llm_metadata") or {}).get("total_tokens") or 0,
        "path": payload.get("_path", ""),
        "env_setups": [item for item in (failing.get("env_setup"), fixed.get("env_setup")) if item],
        "workflow_signal_expected": workflow_signal_expected,
        "workflow_signal_preserved": workflow_signal_preserved,
        "working_directory_expected": workdir_expected,
        "working_directory_recovered": workdir_recovered,
        "failed_command_grounded": failed_command_grounded,
        "oracle_valid": oracle_valid,
        "grounded": grounded,
        "environment_failure": environment_failure,
        "target_preflight_valid": not bool(target_preflight.get("has_invalid_target")) if target_preflight else None,
        "routing_failure_type": routing_decision.get("failure_type", ""),
        "selected_context_sources": ",".join(str(source) for source in selected_context_sources),
        "context_size_chars": routing_decision.get("routed_context_size_chars") or payload.get("context_size_chars") or 0,
        "original_context_size_chars": routing_decision.get("original_context_size_chars") or 0,
        "context_reduction_ratio": routing_decision.get("reduction_ratio") or 0.0,
        "typed_feedback_actions": ",".join(typed_feedback_actions),
        "dependency_diagnoses": [
            diagnosis
            for diagnosis in (failing.get("dependency_diagnosis"), fixed.get("dependency_diagnosis"))
            if diagnosis
        ],
    }


def _rate(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return round(sum(1 for row in rows if row.get(key)) / len(rows), 4)


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "total_instances": 0,
            "attempted_instances": 0,
            "executable_rate": 0.0,
            "buggy_fail_rate": 0.0,
            "fixed_pass_rate": 0.0,
            "f_to_p_rate": 0.0,
            "fake_reproduction_rate": 0.0,
            "timeout_rate": 0.0,
            "avg_attempts": 0.0,
            "avg_duration": 0.0,
            "avg_token_cost": 0.0,
        }
    attempted = [row for row in rows if row.get("generation_status") != "skipped_no_llm"]
    return {
        "total_instances": len({row.get("instance_id") for row in rows}),
        "attempted_instances": len(attempted),
        "executable_rate": _rate(rows, "executable"),
        "buggy_fail_rate": _rate(rows, "buggy_failed"),
        "fixed_pass_rate": _rate(rows, "fixed_passed"),
        "f_to_p_rate": _rate(rows, "f_to_p"),
        "fake_reproduction_rate": _rate(rows, "fake"),
        "timeout_rate": _rate(rows, "timeout"),
        "avg_attempts": round(sum(float(row.get("attempts") or 0) for row in rows) / len(rows), 3),
        "avg_duration": round(sum(float(row.get("duration") or 0) for row in rows) / len(rows), 3),
        "avg_token_cost": round(sum(float(row.get("token_cost") or 0) for row in rows) / len(rows), 3),
    }


def grouped_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "unknown")].append(row)
    return {name: summarize_rows(group) for name, group in sorted(grouped.items())}


def grouped_by_setting_env_policy(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("setting") or "unknown"), str(row.get("env_policy") or "unknown"))].append(row)
    return grouped


def count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts = Counter(str(row.get(key) or "unknown") for row in rows)
    return dict(sorted(counts.items()))


def is_dependency_classification(classification: str) -> bool:
    return classification in {"dependency_error", "fixed_dependency_error"} or classification.startswith(
        (
            "fixed_missing_ci_tool",
            "fixed_missing_project_package",
            "fixed_missing_third_party_package",
            "fixed_python_version_",
            "fixed_package_version_",
            "fixed_dependency_",
            "failing_missing_ci_tool",
            "failing_missing_project_package",
            "failing_missing_third_party_package",
            "failing_python_version_",
            "failing_package_version_",
            "failing_dependency_",
        )
    )


def dependency_breakdown(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(row.get("classification") or "unknown") for row in rows if is_dependency_classification(str(row.get("classification") or "")))
    return dict(sorted(counts.items()))


def fixed_dependency_count(rows: list[dict[str, Any]]) -> int:
    return sum(
        1
        for row in rows
        if is_dependency_classification(str(row.get("classification") or ""))
        and str(row.get("classification") or "").startswith("fixed_")
    )


def tool_bootstrap_stats(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    stats: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        for env_setup in row.get("env_setups") or []:
            detected_names = set()
            for detected in env_setup.get("tools_detected") or []:
                tool = str(detected.get("tool") if isinstance(detected, dict) else detected)
                if tool:
                    detected_names.add(tool)
            for tool in detected_names:
                stats[tool]["detected"] += 1
            for tool in env_setup.get("tools_missing") or []:
                stats[str(tool)]["missing"] += 1
            for tool in env_setup.get("tools_installed") or []:
                stats[str(tool)]["installed"] += 1
            for tool in env_setup.get("dynamic_missing_tools") or []:
                stats[str(tool)]["dynamic_missing"] += 1
            for tool in env_setup.get("dynamic_installed_tools") or []:
                stats[str(tool)]["dynamic_installed"] += 1
            for tool in env_setup.get("install_failed") or []:
                stats[str(tool)]["install_failed"] += 1
    return {tool: dict(counter) for tool, counter in sorted(stats.items())}


def mechanism_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def measured_rate(value_key: str, expected_key: str | None = None) -> dict[str, Any]:
        if expected_key:
            measured = [row for row in rows if row.get(expected_key) is True]
        else:
            measured = [row for row in rows if row.get(value_key) is not None]
        passed = sum(1 for row in measured if row.get(value_key) is True)
        return {
            "passed": passed,
            "measured": len(measured),
            "rate": round(passed / len(measured), 4) if measured else 0.0,
        }

    return {
        "workflow_execution_signal_preservation": measured_rate(
            "workflow_signal_preserved", "workflow_signal_expected"
        ),
        "working_directory_recovery": measured_rate(
            "working_directory_recovered", "working_directory_expected"
        ),
        "failed_command_grounding": measured_rate("failed_command_grounded"),
        "grounded_reproduction": measured_rate("grounded"),
        "oracle_validity": measured_rate("oracle_valid"),
        "environment_failure": {
            "count": sum(1 for row in rows if row.get("environment_failure")),
            "total": len(rows),
            "rate": round(sum(1 for row in rows if row.get("environment_failure")) / len(rows), 4)
            if rows
            else 0.0,
        },
        "target_preflight_validity": measured_rate("target_preflight_valid"),
    }


def write_results_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "instance_id",
        "repo",
        "setting",
        "failure_type",
        "generation_status",
        "classification",
        "raw_classification",
        "fixed_error_subtype",
        "fixed_error_reason",
        "failing_error_subtype",
        "failing_error_reason",
        "f_to_p",
        "fake",
        "timeout",
        "buggy_failed",
        "fixed_passed",
        "executable",
        "attempts",
        "duration",
        "token_cost",
        "env_policy",
        "workflow_signal_preserved",
        "working_directory_recovered",
        "failed_command_grounded",
        "oracle_valid",
        "grounded",
        "environment_failure",
        "target_preflight_valid",
        "path",
        "routing_failure_type",
        "selected_context_sources",
        "context_size_chars",
        "original_context_size_chars",
        "context_reduction_ratio",
        "typed_feedback_actions",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def summary_markdown(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    by_setting = grouped_summary(rows, "setting")
    lines = ["# CI-Repair MVP Summary", ""]
    lines.append("## By Input Setting")
    lines.append("| setting | executable | buggy_fail | fixed_pass | F->P | fake | timeout |")
    lines.append("| ------- | ---------: | ---------: | ---------: | --: | ---: | ------: |")
    for setting, stats in by_setting.items():
        lines.append(
            "| "
            + " | ".join(
                [
                    setting,
                    _percent(stats["executable_rate"]),
                    _percent(stats["buggy_fail_rate"]),
                    _percent(stats["fixed_pass_rate"]),
                    _percent(stats["f_to_p_rate"]),
                    _percent(stats["fake_reproduction_rate"]),
                    _percent(stats["timeout_rate"]),
                ]
            )
            + " |"
        )

    grouped_env = grouped_by_setting_env_policy(rows)
    lines.extend(["", "## By Setting And Env Policy"])
    lines.append("| setting | env_policy | executable | fixed_dependency_error | F->P | reproduced |")
    lines.append("| ------- | ---------- | ---------: | ---------------------: | --: | ---------: |")
    for (setting, env_policy), group in sorted(grouped_env.items()):
        stats = summarize_rows(group)
        reproduced = sum(1 for row in group if row.get("classification") == "reproduced")
        lines.append(
            f"| {setting} | {env_policy} | {_percent(stats['executable_rate'])} | "
            f"{fixed_dependency_count(group)} | {_percent(stats['f_to_p_rate'])} | {reproduced} |"
        )

    dep_counts = dependency_breakdown(rows)
    if dep_counts:
        lines.extend(["", "## Dependency Breakdown"])
        lines.append("| dependency_category | count |")
        lines.append("| ------------------- | ----: |")
        for category, count in sorted(dep_counts.items()):
            lines.append(f"| {category} | {count} |")

    tool_stats = tool_bootstrap_stats(rows)
    if tool_stats:
        lines.extend(["", "## Tool Bootstrap Stats"])
        lines.append("| tool | detected | missing | installed | dynamic_missing | dynamic_installed | install_failed |")
        lines.append("| ---- | -------: | ------: | --------: | --------------: | ----------------: | -------------: |")
        for tool, stats in tool_stats.items():
            lines.append(
                f"| {tool} | {stats.get('detected', 0)} | {stats.get('missing', 0)} | "
                f"{stats.get('installed', 0)} | {stats.get('dynamic_missing', 0)} | "
                f"{stats.get('dynamic_installed', 0)} | {stats.get('install_failed', 0)} |"
            )

    routed_rows = [row for row in rows if row.get("selected_context_sources")]
    if routed_rows:
        lines.extend(["", "## Routing Context Stats"])
        lines.append("| setting | n | avg_context_chars | avg_reduction | F->P |")
        lines.append("| ------- | -: | ----------------: | ------------: | --: |")
        grouped_routed: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in routed_rows:
            grouped_routed[str(row.get("setting") or "unknown")].append(row)
        for setting, group in sorted(grouped_routed.items()):
            avg_context = sum(float(row.get("context_size_chars") or 0) for row in group) / len(group)
            avg_reduction = sum(float(row.get("context_reduction_ratio") or 0) for row in group) / len(group)
            lines.append(
                f"| {setting} | {len(group)} | {avg_context:.1f} | {_percent(avg_reduction)} | {_percent(_rate(group, 'f_to_p'))} |"
            )

    mechanisms = mechanism_metrics(rows)
    lines.extend(["", "## Mechanism Validity Metrics"])
    lines.append("| metric | passed/count | rate |")
    lines.append("| ------ | -----------: | ---: |")
    for key in (
        "workflow_execution_signal_preservation",
        "working_directory_recovery",
        "failed_command_grounding",
        "grounded_reproduction",
        "oracle_validity",
        "target_preflight_validity",
    ):
        stats = mechanisms[key]
        lines.append(f"| {key} | {stats['passed']}/{stats['measured']} | {_percent(stats['rate'])} |")
    environment = mechanisms["environment_failure"]
    lines.append(
        f"| environment_failure | {environment['count']}/{environment['total']} | {_percent(environment['rate'])} |"
    )

    fixed_rows = [row for row in rows if str(row.get("classification", "")).startswith("fixed_")]
    if fixed_rows:
        lines.extend(["", "## Fixed Error Breakdown"])
        lines.append("| subtype | n | share |")
        lines.append("| ------- | -: | ----: |")
        counts = Counter(str(row.get("classification") or "fixed_error") for row in fixed_rows)
        for subtype, count in counts.most_common():
            lines.append(f"| {subtype} | {count} | {_percent(count / len(fixed_rows))} |")

    failing_rows = [row for row in rows if str(row.get("classification", "")).startswith("failing_")]
    if failing_rows:
        lines.extend(["", "## Failing Error Breakdown"])
        lines.append("| subtype | n | share |")
        lines.append("| ------- | -: | ----: |")
        counts = Counter(str(row.get("classification") or "failing_error") for row in failing_rows)
        for subtype, count in counts.most_common():
            lines.append(f"| {subtype} | {count} | {_percent(count / len(failing_rows))} |")

    lines.extend(["", "## By Failure Type"])
    lines.append("| failure_type | n | F->P | most_common_failure |")
    lines.append("| ------------ | -: | --: | ------------------- |")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("failure_type") or "unknown")].append(row)
    for failure_type, group in sorted(grouped.items()):
        counts = Counter(row["classification"] for row in group if row["classification"] != "reproduced")
        common = counts.most_common(1)[0][0] if counts else ""
        n_instances = len({row.get("instance_id") for row in group})
        lines.append(
            f"| {failure_type} | {n_instances} | {_percent(_rate(group, 'f_to_p'))} | {common} |"
        )

    lines.extend(["", "## Overall", ""])
    lines.append("```json")
    lines.append(json.dumps(summary["overall"], indent=2))
    lines.append("```")
    return "\n".join(lines) + "\n"


def summarize_result_dir(result_dir: Path, *, output_dir: Path | None = None) -> dict[str, Any]:
    output_dir = Path(output_dir or result_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = load_results(result_dir)
    rows = [flatten_result(payload) for payload in payloads]
    grouped_env = grouped_by_setting_env_policy(rows)
    summary = {
        "overall": summarize_rows(rows),
        "by_setting": grouped_summary(rows, "setting"),
        "by_failure_type": grouped_summary(rows, "failure_type"),
        "by_classification": count_by(rows, "classification"),
        "by_setting_env_policy": {
            f"{setting}|{env_policy}": summarize_rows(group)
            for (setting, env_policy), group in sorted(grouped_env.items())
        },
        "fixed_error_breakdown": count_by(
            [row for row in rows if str(row.get("classification", "")).startswith("fixed_")],
            "classification",
        ),
        "failing_error_breakdown": count_by(
            [row for row in rows if str(row.get("classification", "")).startswith("failing_")],
            "classification",
        ),
        "dependency_breakdown": dependency_breakdown(rows),
        "tool_bootstrap_stats": tool_bootstrap_stats(rows),
        "mechanism_metrics": mechanism_metrics(rows),
    }
    write_results_csv(rows, output_dir / "results.csv")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "summary.md").write_text(summary_markdown(rows, summary), encoding="utf-8")
    return summary
