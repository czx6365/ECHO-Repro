from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from echo_repro.models import PreparedRepos

logger = logging.getLogger(__name__)


def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess, action: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(
            f"{action} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def clone_repo(repo: str, target_dir: Path) -> Path:
    target_dir = Path(target_dir)
    if (target_dir / ".git").exists():
        return target_dir

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    result = run_cmd(
        ["git", "clone", f"https://github.com/{repo}.git", str(target_dir)],
    )
    _require_success(result, f"git clone for {repo}")
    return target_dir


def checkout_commit(repo_dir: Path, commit: str) -> None:
    repo_dir = Path(repo_dir)
    checkout_result = run_cmd(["git", "checkout", "--force", commit], cwd=repo_dir)
    _require_success(checkout_result, f"git checkout {commit}")
    clean_result = run_cmd(["git", "clean", "-fdx"], cwd=repo_dir)
    _require_success(clean_result, "git clean -fdx")


def copy_repo(src: Path, dst: Path) -> Path:
    src = Path(src)
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def apply_patch(repo_dir: Path, patch_text: str) -> bool:
    repo_dir = Path(repo_dir)
    normalized_patch_text = patch_text if patch_text.endswith("\n") else f"{patch_text}\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as handle:
        handle.write(normalized_patch_text)
        patch_path = Path(handle.name)

    try:
        result = run_cmd(["git", "apply", str(patch_path)], cwd=repo_dir)
        if result.returncode != 0:
            logger.warning(
                "git apply failed for %s\nstdout:\n%s\nstderr:\n%s",
                repo_dir,
                result.stdout,
                result.stderr,
            )
            return False
        return True
    finally:
        patch_path.unlink(missing_ok=True)


def prepare_swebench_repos(instance: dict, workdir: Path) -> PreparedRepos:
    instance_id = str(instance["instance_id"])
    repo = str(instance["repo"])
    base_commit = str(instance["base_commit"])
    patch_text = str(instance.get("patch", ""))

    instance_dir = Path(workdir) / instance_id
    buggy_repo = instance_dir / "buggy"
    fixed_repo = instance_dir / "fixed"

    clone_repo(repo, buggy_repo)
    checkout_commit(buggy_repo, base_commit)
    copy_repo(buggy_repo, fixed_repo)

    patch_applied = apply_patch(fixed_repo, patch_text) if patch_text.strip() else True
    return PreparedRepos(
        instance_id=instance_id,
        repo=repo,
        base_commit=base_commit,
        buggy_repo=buggy_repo,
        fixed_repo=fixed_repo,
        patch_applied=patch_applied,
    )
