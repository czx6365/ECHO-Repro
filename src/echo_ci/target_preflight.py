from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


TARGET_SUFFIXES = {
    ".py",
    ".pyi",
    ".toml",
    ".ini",
    ".cfg",
    ".yaml",
    ".yml",
    ".json",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
}
IGNORED_TARGET_NAMES = {"reproduce.py", "run.sh", "oracle.json", "generation_metadata.json"}


def _normalize_target(value: str) -> str:
    target = value.replace("\\", "/").strip("'\".,:;()[]{}")
    target = re.sub(r"^\$\{?REPO_ROOT\}?/", "", target)
    target = re.sub(r"^\./", "", target)
    target = re.sub(r"^.*?/(?:failing_repo|fixed_repo)/", "", target)
    project_root = re.search(r"/(src|test|tests|lib|libs|examples|scripts|tools|py)/", target, re.IGNORECASE)
    if project_root and target.startswith("/"):
        target = target[project_root.start() + 1 :]
    if "/.echo_repro/" in target or "/.echo_venv/" in target:
        return ""
    if target.startswith((".echo_repro/", ".echo_venv/")):
        return ""
    if Path(target).name in IGNORED_TARGET_NAMES:
        return ""
    if target.lower().startswith(("secrets.", "env.", "github.")):
        return ""
    if Path(target).suffix.lower() not in TARGET_SUFFIXES:
        return ""
    return target


def extract_harness_targets(*texts: str) -> list[str]:
    targets: list[str] = []
    path_pattern = re.compile(
        r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.$/{}/\\-]+\.(?:pyi?|toml|ini|cfg|ya?ml|json|jsx?|tsx?))",
        re.IGNORECASE,
    )
    for text in texts:
        for match in path_pattern.finditer(str(text or "")):
            target = _normalize_target(match.group(1))
            if target and target not in targets:
                targets.append(target)
    return targets


def _git_changes(repo_path: Path, failing_commit: str, fixed_commit: str) -> dict[str, Any]:
    deleted: set[str] = set()
    changed: set[str] = set()
    renamed: dict[str, str] = {}
    if not failing_commit or not fixed_commit:
        return {"changed": changed, "deleted": deleted, "renamed": renamed, "error": "missing commit"}
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", "-M", failing_commit, fixed_commit, "--"],
            cwd=str(repo_path),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"changed": changed, "deleted": deleted, "renamed": renamed, "error": str(exc)}
    if result.returncode != 0:
        return {"changed": changed, "deleted": deleted, "renamed": renamed, "error": result.stderr}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            old, new = parts[1], parts[2]
            changed.update((old, new))
            renamed[old] = new
        else:
            path = parts[1]
            changed.add(path)
            if status == "D":
                deleted.add(path)
    return {"changed": changed, "deleted": deleted, "renamed": renamed, "error": ""}


def _suffix_matches(repo_path: Path, target: str) -> list[str]:
    name = Path(target).name
    if not name:
        return []
    matches: list[str] = []
    try:
        candidates = repo_path.rglob(name)
    except OSError:
        return []
    for candidate in candidates:
        if not candidate.is_file() or any(part in {".git", ".echo_venv", ".echo_repro"} for part in candidate.parts):
            continue
        relative = candidate.relative_to(repo_path).as_posix()
        if relative.endswith(target) and relative not in matches:
            matches.append(relative)
    return sorted(matches)


def _required_working_directory(resolved_path: str, target: str) -> str:
    if resolved_path == target or not resolved_path.endswith(target):
        return ""
    prefix = resolved_path[: -len(target)].rstrip("/")
    return prefix


def cross_commit_target_preflight(
    *,
    failing_repo: Path,
    fixed_repo: Path,
    failing_commit: str,
    fixed_commit: str,
    run_text: str,
    reproduce_text: str,
    oracle_text: str = "",
) -> dict[str, Any]:
    failing_repo = Path(failing_repo)
    fixed_repo = Path(fixed_repo)
    targets = extract_harness_targets(run_text, reproduce_text, oracle_text)
    changes = _git_changes(failing_repo, failing_commit, fixed_commit)
    records: list[dict[str, Any]] = []
    for target in targets:
        failing_exact = (failing_repo / target).is_file()
        fixed_exact = (fixed_repo / target).is_file()
        failing_matches = [] if failing_exact else _suffix_matches(failing_repo, target)
        fixed_matches = [] if fixed_exact else _suffix_matches(fixed_repo, target)
        failing_resolved = target if failing_exact else (failing_matches[0] if len(failing_matches) == 1 else "")
        fixed_resolved = target if fixed_exact else (fixed_matches[0] if len(fixed_matches) == 1 else "")
        renamed_to = str(changes["renamed"].get(target) or "")
        if failing_resolved and fixed_resolved:
            classification = "target_stable"
        elif failing_resolved and (target in changes["deleted"] or failing_resolved in changes["deleted"]):
            classification = "target_deleted_by_fix"
        elif failing_resolved and (renamed_to or failing_resolved in changes["renamed"]):
            classification = "target_renamed_by_fix"
            renamed_to = renamed_to or str(changes["renamed"].get(failing_resolved) or "")
        elif not failing_resolved and not fixed_resolved:
            classification = "target_hallucinated"
        elif not failing_resolved:
            classification = "target_missing_on_failing"
        else:
            classification = "target_missing_on_fixed"
        required_workdir = _required_working_directory(failing_resolved, target)
        records.append(
            {
                "target": target,
                "exists_on_failing": bool(failing_resolved),
                "exists_on_fixed": bool(fixed_resolved),
                "failing_resolved_path": failing_resolved,
                "fixed_resolved_path": fixed_resolved,
                "changed_by_patch": target in changes["changed"] or failing_resolved in changes["changed"],
                "renamed_or_deleted": classification in {"target_deleted_by_fix", "target_renamed_by_fix"},
                "renamed_to": renamed_to,
                "required_working_directory": required_workdir,
                "classification": classification,
            }
        )
    classifications = sorted({record["classification"] for record in records})
    return {
        "targets": records,
        "classifications": classifications,
        "all_targets_stable": bool(records) and all(
            record["classification"] == "target_stable" for record in records
        ),
        "has_deleted_by_fix": any(
            record["classification"] == "target_deleted_by_fix" for record in records
        ),
        "all_targets_deleted_by_fix": bool(records) and all(
            record["classification"] == "target_deleted_by_fix" for record in records
        ),
        "has_renamed_by_fix": any(
            record["classification"] == "target_renamed_by_fix" for record in records
        ),
        "has_invalid_target": any(
            record["classification"] in {"target_hallucinated", "target_missing_on_failing"}
            for record in records
        ),
        "git_diff_error": changes["error"],
    }
