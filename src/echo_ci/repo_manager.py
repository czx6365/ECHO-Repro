from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


def safe_repo_name(repo: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "__" for ch in repo).strip("_") or "repo"


def repo_clone_url(repo: str) -> str:
    if repo.startswith("http://") or repo.startswith("https://") or repo.endswith(".git"):
        return repo
    return f"https://github.com/{repo}.git"


@dataclass
class GitCommandResult:
    command: list[str]
    cwd: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timeout: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timeout

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CommitCheckResult:
    commit: str
    cat_file_type: str = ""
    tree_available: bool = False
    missing_commit_object: bool = False
    missing_tree_object: bool = False
    fetch_attempts: list[dict] = field(default_factory=list)
    error: str = ""
    stage: str = "cat_file"

    @property
    def ok(self) -> bool:
        return self.cat_file_type == "commit" and self.tree_available

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckoutResult:
    repo: str
    commit: str
    dest_dir: str
    repo_cache: str = ""
    repo_prepare_strategy: str = ""
    commit_check: dict = field(default_factory=dict)
    checkout_success: bool = False
    checkout_error: str = ""
    checkout_stage: str = "clone"
    checkout_strategy: str = ""
    commands: list[dict] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.checkout_success

    def to_dict(self) -> dict:
        return asdict(self)


class RobustRepoManager:
    def __init__(self, cache_dir: Path, work_dir: Path, timeout: int = 300) -> None:
        self.cache_dir = Path(cache_dir).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.timeout = timeout
        self.last_prepare_strategy = ""
        self.last_prepare_commands: list[dict] = []

    def _run(self, command: list[str], *, cwd: Path) -> GitCommandResult:
        try:
            result = subprocess.run(
                command,
                cwd=str(cwd),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
            )
            return GitCommandResult(
                command=command,
                cwd=str(cwd),
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            return GitCommandResult(
                command=command,
                cwd=str(cwd),
                returncode=124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or f"Timed out after {self.timeout}s",
                timeout=True,
            )
        except OSError as exc:
            return GitCommandResult(
                command=command,
                cwd=str(cwd),
                returncode=127,
                stdout="",
                stderr=str(exc),
            )

    def _record(self, result: GitCommandResult, bucket: list[dict] | None = None) -> GitCommandResult:
        if bucket is not None:
            bucket.append(result.to_dict())
        return result

    def prepare_repo(self, repo: str) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        repo_cache = self.cache_dir / safe_repo_name(repo)
        url = repo_clone_url(repo)
        commands: list[dict] = []
        strategy: list[str] = []

        if not (repo_cache / ".git").exists():
            if repo_cache.exists():
                shutil.rmtree(repo_cache)
            result = self._record(
                self._run(["git", "clone", "--no-single-branch", url, str(repo_cache)], cwd=self.cache_dir),
                commands,
            )
            strategy.append("clone_no_single_branch")
            if not result.ok:
                self.last_prepare_strategy = ",".join(strategy)
                self.last_prepare_commands = commands
                return repo_cache
        else:
            self._record(self._run(["git", "remote", "set-url", "origin", url], cwd=repo_cache), commands)
            self._record(self._run(["git", "fetch", "origin", "--tags", "--prune", "--force"], cwd=repo_cache), commands)
            strategy.append("update_cache")

        marker = repo_cache / ".echo_ci_aggressive_fetch_done"
        if marker.exists():
            strategy.append("aggressive_fetch_cached")
            self.last_prepare_strategy = ",".join(strategy)
            self.last_prepare_commands = commands
            return repo_cache

        aggressive_fetches = [
            ["git", "fetch", "origin", "+refs/heads/*:refs/remotes/origin/*", "--prune", "--force"],
            ["git", "fetch", "origin", "+refs/tags/*:refs/tags/*", "--prune", "--force"],
            ["git", "fetch", "origin", "+refs/pull/*/head:refs/remotes/origin/pr/*/head", "--prune", "--force"],
            ["git", "fetch", "origin", "+refs/pull/*/merge:refs/remotes/origin/pr/*/merge", "--prune", "--force"],
        ]
        for command in aggressive_fetches:
            self._record(self._run(command, cwd=repo_cache), commands)
        strategy.append("aggressive_fetch")
        if all(command.get("returncode") == 0 for command in commands[-len(aggressive_fetches) :]):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok\n", encoding="utf-8")
        self.last_prepare_strategy = ",".join(strategy)
        self.last_prepare_commands = commands
        return repo_cache

    def _cat_file_type(self, repo_cache: Path, commit: str) -> GitCommandResult:
        return self._run(["git", "cat-file", "-t", commit], cwd=repo_cache)

    def _tree_check(self, repo_cache: Path, commit: str) -> GitCommandResult:
        return self._run(["git", "rev-parse", f"{commit}^{{tree}}"], cwd=repo_cache)

    def ensure_commit_available(self, repo_cache: Path, commit: str) -> CommitCheckResult:
        result = CommitCheckResult(commit=commit)
        if not commit:
            result.missing_commit_object = True
            result.error = "Missing commit SHA."
            return result

        cat = self._cat_file_type(repo_cache, commit)
        result.cat_file_type = cat.stdout.strip() if cat.ok else ""
        if result.cat_file_type != "commit":
            result.missing_commit_object = True
            result.error = cat.stderr or cat.stdout
            for command in (
                ["git", "fetch", "origin", commit, "--depth=1"],
                ["git", "fetch", "origin", commit],
            ):
                attempt = self._run(command, cwd=repo_cache)
                result.fetch_attempts.append(attempt.to_dict())
                cat = self._cat_file_type(repo_cache, commit)
                result.cat_file_type = cat.stdout.strip() if cat.ok else ""
                if result.cat_file_type == "commit":
                    result.missing_commit_object = False
                    result.error = ""
                    break

        if result.cat_file_type != "commit":
            result.stage = "cat_file"
            result.missing_commit_object = True
            if not result.error:
                result.error = cat.stderr or cat.stdout
            return result

        tree = self._tree_check(repo_cache, commit)
        result.tree_available = tree.ok
        if not tree.ok:
            for command in (
                ["git", "fetch", "origin", commit, "--depth=1"],
                ["git", "fetch", "origin", commit],
            ):
                attempt = self._run(command, cwd=repo_cache)
                result.fetch_attempts.append(attempt.to_dict())
                tree = self._tree_check(repo_cache, commit)
                result.tree_available = tree.ok
                if tree.ok:
                    break
        if not result.tree_available:
            result.stage = "tree_check"
            result.missing_tree_object = True
            result.error = tree.stderr or tree.stdout
        return result

    def _remove_dest(self, dest_dir: Path, repo_cache: Path | None = None) -> None:
        if not dest_dir.exists():
            return
        git_file = dest_dir / ".git"
        if repo_cache and git_file.is_file():
            self._run(["git", "worktree", "remove", "--force", str(dest_dir)], cwd=repo_cache)
        if dest_dir.exists():
            for _ in range(3):
                try:
                    shutil.rmtree(dest_dir)
                    break
                except OSError:
                    time.sleep(0.2)
            if dest_dir.exists():
                subprocess.run(["rm", "-rf", str(dest_dir)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def cleanup_checkout(self, dest_dir: Path, *, repo_cache: Path | None = None) -> bool:
        """Remove an execution checkout after its diagnostics are persisted."""

        self._remove_dest(Path(dest_dir), repo_cache=repo_cache)
        return not Path(dest_dir).exists()

    def checkout_commit(self, repo: str, commit: str, dest_dir: Path) -> CheckoutResult:
        dest_dir = Path(dest_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        commands: list[dict] = []
        repo_cache = self.prepare_repo(repo)
        commands.extend(self.last_prepare_commands)
        checkout = CheckoutResult(
            repo=repo,
            commit=commit,
            dest_dir=str(dest_dir),
            repo_cache=str(repo_cache),
            repo_prepare_strategy=self.last_prepare_strategy,
            commands=commands,
        )
        if not (repo_cache / ".git").exists():
            checkout.checkout_stage = "clone"
            checkout.checkout_error = "Repository cache is unavailable after clone/fetch."
            return checkout

        check = self.ensure_commit_available(repo_cache, commit)
        checkout.commit_check = check.to_dict()
        if not check.ok:
            checkout.checkout_stage = check.stage
            checkout.checkout_error = check.error
            return checkout

        self._remove_dest(dest_dir, repo_cache=repo_cache)
        dest_dir.parent.mkdir(parents=True, exist_ok=True)

        clone_result = self._record(
            self._run(["git", "clone", str(repo_cache), str(dest_dir)], cwd=dest_dir.parent),
            commands,
        )
        checkout.checkout_strategy = "clone_from_cache"
        if clone_result.ok:
            checkout_result = self._record(
                self._run(["git", "checkout", "--detach", commit], cwd=dest_dir),
                commands,
            )
            if checkout_result.ok:
                checkout.checkout_success = True
                checkout.checkout_stage = "checkout"
                return checkout
            checkout.checkout_error = checkout_result.stderr or checkout_result.stdout

        self._remove_dest(dest_dir, repo_cache=repo_cache)
        worktree_result = self._record(
            self._run(["git", "worktree", "add", "--detach", str(dest_dir), commit], cwd=repo_cache),
            commands,
        )
        checkout.checkout_strategy = "worktree_add"
        if worktree_result.ok:
            checkout.checkout_success = True
            checkout.checkout_stage = "checkout"
            return checkout

        checkout.checkout_stage = "checkout"
        checkout.checkout_error = worktree_result.stderr or worktree_result.stdout or checkout.checkout_error
        return checkout


def checkout_result_from_json(path: Path) -> CheckoutResult:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return CheckoutResult(**data)
