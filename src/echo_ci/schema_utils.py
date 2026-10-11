from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

FIELD_CANDIDATES: dict[str, list[str]] = {
    "repo": ["repo", "repository", "full_name", "github_repo", "project", "repo_name"],
    "failing_commit": [
        "failing_commit",
        "failed_commit",
        "buggy_commit",
        "base_commit",
        "fail_sha",
        "sha_fail",
        "before_sha",
    ],
    "fixed_commit": [
        "success_commit",
        "passing_commit",
        "fixed_commit",
        "target_commit",
        "pass_sha",
        "sha_success",
        "after_sha",
    ],
    "ci_log": ["ci_log", "log", "logs", "failure_log", "build_log", "raw_log"],
    "workflow_path": [
        "workflow_path",
        "workflow_file",
        "workflow_filename",
        "ci_workflow_path",
        "github_workflow_path",
    ],
    "workflow_content": ["workflow_content", "workflow_yaml", "workflow_yml", "workflow", "ci_config"],
    "patch": ["patch", "ground_truth_patch", "fix_patch", "developer_patch", "diff"],
    "failure_type": ["failure_type", "error_type", "ci_error_type", "labels", "categories"],
    "instance_id": ["instance_id", "id", "sample_id", "task_id"],
    "issue": ["issue", "problem_statement", "description", "body", "title", "commit_message"],
}


def _lookup_path(instance: Mapping[str, Any], path: str, missing: object) -> Any:
    value: Any = instance
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return missing
        value = value[part]
    return value


def get_field(instance: Mapping[str, Any], candidates: Iterable[str] | str, default: Any = None) -> Any:
    """Return the first matching field, accepting schema aliases and case drift."""

    if isinstance(candidates, str):
        candidates = [candidates]

    missing = object()
    for candidate in candidates:
        if "." in candidate:
            value = _lookup_path(instance, candidate, missing)
            if value is not missing and value is not None:
                return value
        elif candidate in instance and instance[candidate] is not None:
            return instance[candidate]

    lowered = {str(key).lower(): key for key in instance}
    for candidate in candidates:
        key = lowered.get(candidate.lower())
        if key is not None and instance[key] is not None:
            return instance[key]

    return default


def field(instance: Mapping[str, Any], canonical_name: str, default: Any = None) -> Any:
    if canonical_name == "repo":
        value = get_field(instance, FIELD_CANDIDATES[canonical_name], default=None)
        if isinstance(value, str) and ("/" in value or value.startswith("http")):
            return value
        owner = get_field(instance, ["repo_owner", "owner", "org", "organization"], default=None)
        name = get_field(instance, ["repo_name", "name", "project"], default=None)
        if owner and name:
            return f"{owner}/{name}"
        if value is not None:
            return value
        return default
    return get_field(instance, FIELD_CANDIDATES[canonical_name], default=default)


def coerce_text(value: Any, *, max_chars: int | None = None) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    elif isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars] + "\n...[truncated]"
    return text


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, Mapping):
        return [coerce_text(value)]
    if isinstance(value, Iterable):
        return [coerce_text(item).strip() for item in value if coerce_text(item).strip()]
    return [str(value)]


def normalize_failure_type(value: Any) -> str:
    items = [item.lower() for item in as_list(value)]
    joined = " ".join(items)
    for failure_type in ("test", "lint", "dependency", "build", "format", "workflow"):
        if failure_type in joined:
            return failure_type
    if "type" in joined or "mypy" in joined:
        return "lint"
    if "black" in joined or "isort" in joined or "ruff format" in joined:
        return "format"
    return items[0] if len(items) == 1 else "unknown"


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        if not value:
            return "list[empty]"
        inner = sorted({type_name(item) for item in value[:5]})
        return f"list[{', '.join(inner)}]"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def preview_value(value: Any, max_chars: int = 180) -> str:
    text = coerce_text(value)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def canonical_preview(instance: Mapping[str, Any]) -> dict[str, str]:
    preview = {}
    for name in FIELD_CANDIDATES:
        value = field(instance, name, default=None)
        if value is not None:
            preview[name] = preview_value(value)
    return preview


def safe_instance_id(instance: Mapping[str, Any], fallback_index: int | None = None) -> str:
    raw = field(instance, "instance_id", default=None)
    if raw is None:
        repo = str(field(instance, "repo", default="instance"))
        raw = f"{repo}-{fallback_index}" if fallback_index is not None else repo
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(raw))
    return safe.strip("._") or f"instance_{fallback_index or 0}"
