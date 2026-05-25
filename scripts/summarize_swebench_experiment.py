from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

DEFAULT_INSTANCES_PATH = Path("data/swebench_lite_small.jsonl")
DEFAULT_OUTPUTS_DIR = Path("outputs")
DEFAULT_MARKDOWN_PATH = Path("outputs/swebench_lite_small_summary.md")
DEFAULT_CSV_PATH = Path("outputs/swebench_lite_small_summary.csv")

TABLE_COLUMNS = [
    "instance_id",
    "repo",
    "prepared?",
    "env ready?",
    "reproduced?",
    "fixed passed?",
    "failure category",
    "cost",
    "attempts",
]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_result(outputs_dir: Path, instance_id: str) -> dict[str, Any] | None:
    path = Path(outputs_dir) / instance_id / "result.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def bool_mark(value: bool | None) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "pending"


def prepared_status(result: dict[str, Any]) -> str:
    metadata = result.get("instance_metadata", {})
    repo_validated = metadata.get("repo_validated")
    if repo_validated is None:
        return "unknown"
    return bool_mark(bool(repo_validated))


def total_tokens(result: dict[str, Any]) -> int:
    total = 0
    for attempt in result.get("attempts_summary", []):
        metadata = attempt.get("llm_metadata") or {}
        total += int(metadata.get("total_tokens") or 0)
    return total


def env_ready(result: dict[str, Any] | None) -> bool | None:
    if result is None:
        return None
    final = result.get("final_result", {})
    if final.get("failure_category") in {"repo_error", "patch_error"}:
        return None
    if final.get("failure_category") in {"dependency_error", "environment_error", "import_error"}:
        return False

    repairs = result.get("environment_repairs", [])
    if any(not repair.get("success", False) for repair in repairs):
        return False
    return True


def summarize_instance(instance: dict[str, Any], outputs_dir: Path) -> dict[str, str]:
    instance_id = str(instance.get("instance_id", ""))
    result = load_result(outputs_dir, instance_id)
    if result is None:
        return {
            "instance_id": instance_id,
            "repo": str(instance.get("repo", "")),
            "prepared?": "pending",
            "env ready?": "pending",
            "reproduced?": "pending",
            "fixed passed?": "pending",
            "failure category": "not_run",
            "cost": "0",
            "attempts": "0",
        }

    final = result.get("final_result", {})
    buggy_status = final.get("buggy_status")
    fixed_status = final.get("fixed_status")

    return {
        "instance_id": instance_id,
        "repo": str(instance.get("repo", result.get("instance_metadata", {}).get("repo", ""))),
        "prepared?": prepared_status(result),
        "env ready?": bool_mark(env_ready(result)),
        "reproduced?": bool_mark(buggy_status == "reproduced"),
        "fixed passed?": bool_mark(fixed_status == "resolved"),
        "failure category": str(final.get("failure_category") or ""),
        "cost": str(total_tokens(result)),
        "attempts": str(len(result.get("attempts_summary", []))),
    }


def markdown_table(rows: list[dict[str, str]]) -> str:
    lines = [
        "| " + " | ".join(TABLE_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in TABLE_COLUMNS) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row[column] for column in TABLE_COLUMNS) + " |")
    return "\n".join(lines) + "\n"


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TABLE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize small SWE-bench Lite experiment outputs.")
    parser.add_argument("--instances-file", type=Path, default=DEFAULT_INSTANCES_PATH)
    parser.add_argument("--outputs-dir", type=Path, default=DEFAULT_OUTPUTS_DIR)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_MARKDOWN_PATH)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_CSV_PATH)
    args = parser.parse_args()

    instances = load_jsonl(args.instances_file)
    rows = [summarize_instance(instance, args.outputs_dir) for instance in instances]

    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(markdown_table(rows), encoding="utf-8")
    write_csv(rows, args.output_csv)

    print(f"Wrote Markdown summary to {args.output_md}")
    print(f"Wrote CSV summary to {args.output_csv}")
    print(markdown_table(rows))


if __name__ == "__main__":
    main()
