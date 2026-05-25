from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from echo_repro.models import PreparedRepos

logger = logging.getLogger(__name__)


class RepoPreparationError(RuntimeError):
    """Raised when a prepared benchmark repository cannot be trusted."""


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    timeout = int(os.getenv("ECHO_REPRO_CMD_TIMEOUT", timeout))
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout=_coerce_output(exc.stdout),
            stderr=_coerce_output(exc.stderr) + f"\nCommand timed out after {timeout} seconds.",
        )


def _require_success(result: subprocess.CompletedProcess, action: str) -> None:
    if result.returncode != 0:
        raise RepoPreparationError(
            f"{action} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def _require_git_repo(repo_dir: Path) -> None:
    repo_dir = Path(repo_dir)
    if not (repo_dir / ".git").exists():
        raise RepoPreparationError(f"Prepared repository is missing .git metadata: {repo_dir}")

    result = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_dir)
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise RepoPreparationError(
            f"Prepared path is not a valid git work tree: {repo_dir}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    top_level = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=repo_dir)
    if top_level.returncode != 0 or Path(top_level.stdout.strip()).resolve() != repo_dir.resolve():
        raise RepoPreparationError(
            f"Prepared path is not the git work tree root: {repo_dir}\n"
            f"stdout:\n{top_level.stdout}\n"
            f"stderr:\n{top_level.stderr}"
        )


def get_head_commit(repo_dir: Path) -> str:
    _require_git_repo(repo_dir)
    result = run_cmd(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    _require_success(result, f"git rev-parse HEAD for {repo_dir}")
    return result.stdout.strip()


def require_head_commit(repo_dir: Path, expected_commit: str) -> str:
    actual_commit = get_head_commit(repo_dir)
    if actual_commit != expected_commit:
        raise RepoPreparationError(
            f"Prepared repository HEAD mismatch for {repo_dir}: "
            f"expected {expected_commit}, got {actual_commit}"
        )
    return actual_commit


def get_worktree_diff_stat(repo_dir: Path) -> str:
    _require_git_repo(repo_dir)
    result = run_cmd(["git", "diff", "--stat"], cwd=repo_dir)
    _require_success(result, f"git diff --stat for {repo_dir}")
    return result.stdout.strip()


def repo_slug(repo: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "__", repo.strip())
    return slug.strip("_") or "unknown_repo"


def repo_remote_url(repo: str) -> str:
    if repo.startswith(("https://", "http://", "file://", "ssh://", "git@")):
        return repo
    repo_path = Path(repo).expanduser()
    if repo_path.exists() or repo_path.is_absolute() or repo.startswith((".", "~")):
        return str(repo_path.resolve())
    return f"https://github.com/{repo}.git"


def _ensure_origin(repo_dir: Path, remote_url: str) -> None:
    result = run_cmd(["git", "remote", "get-url", "origin"], cwd=repo_dir)
    if result.returncode == 0:
        if result.stdout.strip() != remote_url:
            set_result = run_cmd(["git", "remote", "set-url", "origin", remote_url], cwd=repo_dir)
            _require_success(set_result, f"git remote set-url origin for {repo_dir}")
        return

    add_result = run_cmd(["git", "remote", "add", "origin", remote_url], cwd=repo_dir)
    _require_success(add_result, f"git remote add origin for {repo_dir}")


def has_commit(repo_dir: Path, commit: str) -> bool:
    _require_git_repo(repo_dir)
    result = run_cmd(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_dir)
    return result.returncode == 0


def is_retryable_fetch_error(stderr: str) -> bool:
    lowered = stderr.lower()
    retryable_fragments = [
        "http2",
        "curl 92",
        "early eof",
        "connection reset",
        "operation timed out",
        "the remote end hung up unexpectedly",
    ]
    return any(fragment in lowered for fragment in retryable_fragments)


def fetch_commit(repo_dir: Path, commit: str) -> None:
    _require_git_repo(repo_dir)
    if has_commit(repo_dir, commit):
        return

    result = run_cmd(["git", "fetch", "--depth=1", "origin", commit], cwd=repo_dir)
    if result.returncode != 0 and is_retryable_fetch_error(result.stderr):
        retry = run_cmd(
            ["git", "-c", "http.version=HTTP/1.1", "fetch", "--depth=1", "origin", commit],
            cwd=repo_dir,
        )
        if retry.returncode == 0:
            result = retry
        else:
            result = subprocess.CompletedProcess(
                args=retry.args,
                returncode=retry.returncode,
                stdout=(
                    f"{result.stdout}\n"
                    "--- retry with http.version=HTTP/1.1 stdout ---\n"
                    f"{retry.stdout}"
                ),
                stderr=(
                    f"{result.stderr}\n"
                    "--- retry with http.version=HTTP/1.1 stderr ---\n"
                    f"{retry.stderr}"
                ),
            )
    _require_success(result, f"git fetch --depth=1 origin {commit}")
    if not has_commit(repo_dir, commit):
        raise RepoPreparationError(f"Fetched commit is still unavailable in cache: {commit}")


def ensure_repo_cache(repo: str, cache_dir: Path, commit: str) -> Path:
    cache_repo = Path(cache_dir) / repo_slug(repo)
    remote_url = repo_remote_url(repo)

    if cache_repo.exists() and not (cache_repo / ".git").exists():
        shutil.rmtree(cache_repo)

    if not cache_repo.exists():
        cache_repo.mkdir(parents=True)
        init_result = run_cmd(["git", "init"], cwd=cache_repo)
        try:
            _require_success(init_result, f"git init cache for {repo}")
        except RepoPreparationError:
            shutil.rmtree(cache_repo, ignore_errors=True)
            raise

    _require_git_repo(cache_repo)
    _ensure_origin(cache_repo, remote_url)
    fetch_commit(cache_repo, commit)
    return cache_repo


def materialize_repo_from_cache(repo: str, cache_dir: Path, target_dir: Path, commit: str) -> Path:
    cache_repo = ensure_repo_cache(repo, cache_dir, commit)
    copy_repo(cache_repo, target_dir)
    checkout_commit(target_dir, commit)
    return cache_repo


def clone_repo(repo: str, target_dir: Path) -> Path:
    target_dir = Path(target_dir)
    if (target_dir / ".git").exists():
        _require_git_repo(target_dir)
        return target_dir

    existed_before = target_dir.exists()
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    result = run_cmd(
        ["git", "clone", repo_remote_url(repo), str(target_dir)],
    )
    try:
        _require_success(result, f"git clone for {repo}")
    except RepoPreparationError:
        if not existed_before and target_dir.exists():
            shutil.rmtree(target_dir)
        raise
    return target_dir


def checkout_commit(repo_dir: Path, commit: str) -> None:
    repo_dir = Path(repo_dir)
    _require_git_repo(repo_dir)
    checkout_result = run_cmd(["git", "checkout", "--force", commit], cwd=repo_dir)
    _require_success(checkout_result, f"git checkout {commit}")
    clean_result = run_cmd(["git", "clean", "-fdx"], cwd=repo_dir)
    _require_success(clean_result, "git clean -fdx")
    require_head_commit(repo_dir, commit)


def copy_repo(src: Path, dst: Path) -> Path:
    src = Path(src)
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def apply_patch(repo_dir: Path, patch_text: str) -> bool:
    repo_dir = Path(repo_dir)
    _require_git_repo(repo_dir)
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


def prepare_swebench_repos(instance: dict, workdir: Path, cache_dir: Path | None = None) -> PreparedRepos:
    instance_id = str(instance["instance_id"])
    repo = str(instance["repo"])
    base_commit = str(instance["base_commit"])
    patch_text = str(instance.get("patch", ""))

    instance_dir = Path(workdir) / instance_id
    buggy_repo = instance_dir / "buggy"
    fixed_repo = instance_dir / "fixed"

    repo_cache_path = None
    if cache_dir is None:
        clone_repo(repo, buggy_repo)
        checkout_commit(buggy_repo, base_commit)
    else:
        repo_cache_path = materialize_repo_from_cache(repo, cache_dir, buggy_repo, base_commit)

    buggy_commit = require_head_commit(buggy_repo, base_commit)
    copy_repo(buggy_repo, fixed_repo)
    fixed_commit = require_head_commit(fixed_repo, base_commit)

    patch_applied = apply_patch(fixed_repo, patch_text) if patch_text.strip() else True
    if patch_text.strip() and not patch_applied:
        raise RepoPreparationError(f"Patch did not apply cleanly for {instance_id}: {fixed_repo}")

    fixed_diff_stat = get_worktree_diff_stat(fixed_repo)
    if patch_text.strip() and not fixed_diff_stat:
        raise RepoPreparationError(
            f"Patch application produced no tracked diff for {instance_id}: {fixed_repo}"
        )

    return PreparedRepos(
        instance_id=instance_id,
        repo=repo,
        base_commit=base_commit,
        buggy_repo=buggy_repo,
        fixed_repo=fixed_repo,
        patch_applied=patch_applied,
        repo_validated=True,
        buggy_commit=buggy_commit,
        fixed_commit=fixed_commit,
        fixed_diff_stat=fixed_diff_stat,
        repo_cache_path=repo_cache_path,
    )
