from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_instances(jsonl_path: Path) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    for line in Path(jsonl_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        instances.append(json.loads(line))
    return instances


def select_instance(instances: list[dict[str, Any]], instance_id: str) -> dict[str, Any]:
    for instance in instances:
        if instance.get("instance_id") == instance_id:
            return instance
    raise ValueError(f"Instance not found: {instance_id}")


def extract_issue_text(instance: dict[str, Any]) -> str:
    problem_statement = str(instance.get("problem_statement", "")).strip()
    hints_text = str(instance.get("hints_text", "")).strip()
    parts = [part for part in [problem_statement, hints_text] if part]
    return "\n\nAdditional hints:\n".join(parts) if len(parts) == 2 else (parts[0] if parts else "")


def get_repo_metadata(instance: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": instance.get("instance_id", ""),
        "repo": instance.get("repo", ""),
        "version": instance.get("version", ""),
        "base_commit": instance.get("base_commit", ""),
        "patch": instance.get("patch", ""),
        "test_patch": instance.get("test_patch", ""),
    }


def prepare_instance_stub(instance: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": instance.get("instance_id", ""),
        "issue_text": extract_issue_text(instance),
        "repo_metadata": get_repo_metadata(instance),
    }
