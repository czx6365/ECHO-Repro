from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from echo_ci.ci_repair_loader import load_instances
from echo_ci.metrics import flatten_result, load_results
from echo_ci.repo_manager import RobustRepoManager
from echo_ci.schema_utils import field, safe_instance_id


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _pr_refs_containing(repo_cache: Path, commit: str) -> str:
    result = _run(["git", "for-each-ref", "--contains", commit, "--format=%(refname:short)", "refs/remotes/origin/pr"], repo_cache)
    if result.returncode != 0:
        return ""
    refs = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return ", ".join(refs[:5])


def _likely_cause(check: dict[str, Any], checkout_error: str, pr_refs: str) -> str:
    lowered = checkout_error.lower()
    if check.get("cat_file_type") == "commit" and check.get("tree_available"):
        if pr_refs:
            return "commit_available_in_pr_refs"
        return "available_after_aggressive_fetch"
    if check.get("missing_commit_object"):
        return "missing_commit_object"
    if check.get("missing_tree_object"):
        return "missing_tree_object"
    if pr_refs:
        return "commit_available_in_pr_refs"
    if "repository not found" in lowered or "not found" in lowered:
        return "repo_unavailable_or_private"
    if "unable to read tree" in lowered:
        return "missing_tree_object"
    if "reference is not a tree" in lowered or "bad object" in lowered:
        return "missing_commit_object"
    return "checkout_failed"


def markdown_table(rows: list[dict[str, str]]) -> str:
    columns = [
        "instance",
        "repo",
        "setting",
        "sha_fail",
        "commit_object",
        "tree_object",
        "checkout_error",
        "likely_cause",
    ]
    lines = [
        "# Checkout Diagnosis",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        values = [row.get(column, "").replace("\n", " ")[:180] for column in columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose CI-Repair checkout failures.")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--result_dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache_dir", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    instances = load_instances(dataset_path=args.dataset)
    by_id = {safe_instance_id(instance): instance for instance in instances}
    payloads = load_results(args.result_dir)
    manager = RobustRepoManager(
        cache_dir=args.cache_dir or (args.result_dir / "checkout_diagnosis_cache"),
        work_dir=args.result_dir / "checkout_diagnosis_work",
        timeout=args.timeout,
    )
    rows: list[dict[str, str]] = []
    repo_cache_by_repo: dict[str, Path] = {}

    for payload in payloads:
        flat = flatten_result(payload)
        if flat["classification"] != "failing_checkout_error":
            continue
        instance_id = str(flat["instance_id"])
        instance = by_id.get(instance_id, {})
        repo = str(field(instance, "repo", default=flat.get("repo", "")))
        sha_fail = str(field(instance, "failing_commit", default=""))
        if repo not in repo_cache_by_repo:
            repo_cache_by_repo[repo] = manager.prepare_repo(repo)
        repo_cache = repo_cache_by_repo[repo]
        check = manager.ensure_commit_available(repo_cache, sha_fail).to_dict()
        pr_refs = _pr_refs_containing(repo_cache, sha_fail) if check.get("cat_file_type") == "commit" else ""
        execution = payload.get("execution") or {}
        failing = execution.get("failing") or {}
        checkout_error = (
            failing.get("checkout_error")
            or failing.get("error")
            or execution.get("failing_error_reason")
            or flat.get("failing_error_reason")
            or ""
        )
        rows.append(
            {
                "instance": instance_id,
                "repo": repo,
                "setting": str(flat["setting"]),
                "sha_fail": sha_fail,
                "commit_object": "yes" if check.get("cat_file_type") == "commit" else "no",
                "tree_object": "yes" if check.get("tree_available") else "no",
                "checkout_error": checkout_error,
                "likely_cause": _likely_cause(check, checkout_error, pr_refs),
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown_table(rows), encoding="utf-8")
    print(json.dumps({"diagnosed": len(rows), "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
