from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from echo_ci.ci_context_retriever import ContextBundle
from echo_ci.schema_utils import normalize_failure_type

CONTEXT_SOURCES = ("ci_log", "workflow", "source", "tests", "env")


@dataclass
class RoutingDecision:
    route_type: str
    failure_type: str
    oracle_type: str
    selected_context_sources: list[str]
    requested_context_sources: list[str]
    dropped_context_sources: list[str]
    missing_selected_context_sources: list[str]
    reason: str
    original_context_size_chars: int
    routed_context_size_chars: int
    reduction_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _context_size(context: ContextBundle) -> int:
    return sum(len(str(getattr(context, source) or "")) for source in CONTEXT_SOURCES)


def _has_content(context: ContextBundle, source: str) -> bool:
    return bool(str(getattr(context, source) or "").strip())


def _derived_failure_type(failure_spec: dict[str, Any], override: str | None = None) -> str:
    raw_value = str(override or failure_spec.get("failure_type") or "").lower()
    if raw_value in {"environment", "env", "path", "target", "config", "configuration"}:
        raw_type = "environment" if raw_value == "env" else raw_value
    else:
        raw_type = normalize_failure_type(override or failure_spec.get("failure_type"))
    oracle_type = normalize_failure_type(failure_spec.get("oracle_type"))
    if raw_type == "unknown" and oracle_type != "unknown":
        raw_type = oracle_type

    text = "\n".join(
        str(failure_spec.get(key) or "")
        for key in (
            "failure_type",
            "oracle_type",
            "failed_command",
            "error_signature",
            "failed_step",
        )
    ).lower()
    if raw_type != "unknown":
        return raw_type
    if re.search(r"working[-_ ]directory|wrong cwd|\bcd:|no such file or directory|pathspec", text):
        return "path"
    if re.search(r"command not found|permission denied|python-version|setup-python|unsupported python", text):
        return "environment"
    if ".github/workflows" in text or "workflow" in text:
        return "workflow"
    return "unknown"


def _route_sources(failure_type: str, failure_spec: dict[str, Any]) -> tuple[tuple[str, ...], str]:
    command = str(failure_spec.get("failed_command") or "").lower()
    dependency_markers = "\n".join(
        str(item)
        for item in failure_spec.get("dependency_context", [])
        if str(item).strip()
    ).lower()

    if failure_type in {"lint", "format"}:
        return (
            ("ci_log", "workflow", "source", "env"),
            "lint/format failures need the failed tool command, workflow scope, target files, and config/dependency files; tests are usually noise.",
        )
    if failure_type == "test":
        sources = ["ci_log", "workflow", "source", "tests"]
        if dependency_markers or any(token in command for token in ("tox", "nox", "pip", "poetry")):
            sources.append("env")
        return (
            tuple(sources),
            "test failures need traceback/log evidence, the CI command, existing tests, and related source; env files are added only when setup is implicated.",
        )
    if failure_type == "dependency":
        return (
            ("ci_log", "workflow", "env"),
            "dependency failures are primarily explained by install logs, workflow setup, and dependency manifests; source/test snippets are deferred.",
        )
    if failure_type == "build":
        return (
            ("ci_log", "workflow", "source", "env"),
            "build failures need the failed build command, workflow setup, source/build targets, and packaging configuration.",
        )
    if failure_type in {"workflow", "config", "configuration"}:
        return (
            ("ci_log", "workflow", "env"),
            "workflow/config failures need the workflow step, command output, and environment/dependency configuration.",
        )
    if failure_type in {"environment", "path"}:
        return (
            ("ci_log", "workflow", "source", "env"),
            "environment/path failures need command output, workflow cwd/runtime signals, target files, and environment files.",
        )
    return (
        ("ci_log", "workflow", "source", "tests", "env"),
        "unknown failures use a conservative compact full context because routing confidence is low.",
    )


def route_context(
    context: ContextBundle,
    *,
    route_type: str = "failure_type",
    failure_type_override: str | None = None,
) -> tuple[ContextBundle, RoutingDecision]:
    """Return a context bundle whose sources are selected by failure type."""

    failure_spec = dict(context.failure_spec or {})
    failure_type = _derived_failure_type(failure_spec, override=failure_type_override)
    requested_sources, reason = _route_sources(failure_type, failure_spec)
    requested = list(requested_sources)
    available = [source for source in CONTEXT_SOURCES if _has_content(context, source)]
    selected = [source for source in requested if _has_content(context, source)]
    dropped = [source for source in available if source not in requested]
    missing = [source for source in requested if not _has_content(context, source)]

    routed = ContextBundle(
        ci_log=context.ci_log if "ci_log" in requested_sources else "",
        workflow=context.workflow if "workflow" in requested_sources else "",
        source=context.source if "source" in requested_sources else "",
        tests=context.tests if "tests" in requested_sources else "",
        env=context.env if "env" in requested_sources else "",
        failure_spec=failure_spec,
    )
    original_size = _context_size(context)
    routed_size = _context_size(routed)
    reduction = 0.0 if original_size == 0 else round(1 - (routed_size / original_size), 4)
    decision = RoutingDecision(
        route_type=route_type,
        failure_type=failure_type,
        oracle_type=str(failure_spec.get("oracle_type") or "unknown"),
        selected_context_sources=selected,
        requested_context_sources=requested,
        dropped_context_sources=dropped,
        missing_selected_context_sources=missing,
        reason=reason,
        original_context_size_chars=original_size,
        routed_context_size_chars=routed_size,
        reduction_ratio=reduction,
    )
    return routed, decision


def write_routing_decision(decision: RoutingDecision, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "routing_decision.json"
    path.write_text(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
