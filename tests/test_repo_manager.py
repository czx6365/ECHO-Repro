from pathlib import Path
import shutil
import sys

import pytest

import echo_repro.repo_manager as repo_manager


def _git(cmd: list[str], cwd: Path) -> str:
    result = repo_manager.run_cmd(cmd, cwd=cwd)
    if result.returncode != 0:
        raise AssertionError(f"git command failed: {cmd}\nstdout={result.stdout}\nstderr={result.stderr}")
    return result.stdout.strip()


def _make_local_repo(repo_dir: Path) -> tuple[str, str]:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(["git", "init"], cwd=repo_dir)
    _git(["git", "config", "user.name", "Test User"], cwd=repo_dir)
    _git(["git", "config", "user.email", "test@example.com"], cwd=repo_dir)

    tracked = repo_dir / "sample.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(["git", "add", "sample.txt"], cwd=repo_dir)
    _git(["git", "commit", "-m", "base"], cwd=repo_dir)
    base_commit = _git(["git", "rev-parse", "HEAD"], cwd=repo_dir)

    tracked.write_text("fixed\n", encoding="utf-8")
    patch_text = _git(["git", "diff"], cwd=repo_dir)
    _git(["git", "checkout", "--", "sample.txt"], cwd=repo_dir)
    return base_commit, patch_text


def test_checkout_commit_restores_commit_and_cleans_untracked(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    base_commit, _ = _make_local_repo(repo_dir)

    tracked = repo_dir / "sample.txt"
    tracked.write_text("changed\n", encoding="utf-8")
    _git(["git", "add", "sample.txt"], cwd=repo_dir)
    _git(["git", "commit", "-m", "change"], cwd=repo_dir)

    untracked = repo_dir / "temp.txt"
    untracked.write_text("temp\n", encoding="utf-8")

    repo_manager.checkout_commit(repo_dir, base_commit)

    assert tracked.read_text(encoding="utf-8") == "base\n"
    assert not untracked.exists()


def test_copy_repo_replaces_existing_destination(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "a.txt").write_text("hello\n", encoding="utf-8")
    dst.mkdir()
    (dst / "stale.txt").write_text("stale\n", encoding="utf-8")

    copied = repo_manager.copy_repo(src, dst)

    assert copied == dst
    assert (dst / "a.txt").read_text(encoding="utf-8") == "hello\n"
    assert not (dst / "stale.txt").exists()


def test_apply_patch_applies_small_patch(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    _, patch_text = _make_local_repo(repo_dir)

    applied = repo_manager.apply_patch(repo_dir, patch_text)

    assert applied is True
    assert (repo_dir / "sample.txt").read_text(encoding="utf-8") == "fixed\n"


def test_repo_slug_sanitizes_repo_names():
    assert repo_manager.repo_slug("astropy/astropy") == "astropy__astropy"
    assert repo_manager.repo_slug("  ") == "unknown_repo"


def test_fetch_error_retry_classifier_handles_http2_noise():
    assert repo_manager.is_retryable_fetch_error("Error in the HTTP2 framing layer") is True
    assert repo_manager.is_retryable_fetch_error("fatal: repository not found") is False


def test_prepare_swebench_repos_uses_cache_and_reuses_cached_commit(tmp_path: Path):
    source_repo = tmp_path / "source_repo"
    base_commit, patch_text = _make_local_repo(source_repo)
    cache_dir = tmp_path / "cache"

    instance = {
        "instance_id": "demo__repo-cache",
        "repo": str(source_repo),
        "base_commit": base_commit,
        "patch": patch_text,
    }

    prepared = repo_manager.prepare_swebench_repos(
        instance,
        workdir=tmp_path / "prepared",
        cache_dir=cache_dir,
    )

    expected_cache_path = cache_dir / repo_manager.repo_slug(str(source_repo))
    assert prepared.repo_cache_path == expected_cache_path
    assert repo_manager.has_commit(expected_cache_path, base_commit) is True
    assert (prepared.buggy_repo / "sample.txt").read_text(encoding="utf-8") == "base\n"
    assert (prepared.fixed_repo / "sample.txt").read_text(encoding="utf-8") == "fixed\n"

    shutil.rmtree(source_repo)
    prepared_again = repo_manager.prepare_swebench_repos(
        instance,
        workdir=tmp_path / "prepared_again",
        cache_dir=cache_dir,
    )

    assert prepared_again.repo_cache_path == expected_cache_path
    assert (prepared_again.buggy_repo / "sample.txt").read_text(encoding="utf-8") == "base\n"


def test_prepare_swebench_repos_with_mocked_clone(tmp_path: Path, monkeypatch):
    source_repo = tmp_path / "source_repo"
    base_commit, patch_text = _make_local_repo(source_repo)

    def fake_clone(repo: str, target_dir: Path) -> Path:
        return repo_manager.copy_repo(source_repo, target_dir)

    monkeypatch.setattr(repo_manager, "clone_repo", fake_clone)

    instance = {
        "instance_id": "demo__repo-1",
        "repo": "demo/repo",
        "base_commit": base_commit,
        "patch": patch_text,
    }
    prepared = repo_manager.prepare_swebench_repos(instance, workdir=tmp_path / "prepared")

    assert prepared.instance_id == "demo__repo-1"
    assert prepared.repo == "demo/repo"
    assert prepared.base_commit == base_commit
    assert prepared.patch_applied is True
    assert prepared.repo_validated is True
    assert prepared.buggy_commit == base_commit
    assert prepared.fixed_commit == base_commit
    assert "sample.txt" in prepared.fixed_diff_stat
    assert prepared.buggy_repo.exists()
    assert prepared.fixed_repo.exists()
    assert (prepared.buggy_repo / "sample.txt").read_text(encoding="utf-8") == "base\n"
    assert (prepared.fixed_repo / "sample.txt").read_text(encoding="utf-8") == "fixed\n"


def test_prepare_swebench_repos_rejects_non_git_source(tmp_path: Path, monkeypatch):
    source_repo = tmp_path / "source_repo"
    source_repo.mkdir()
    (source_repo / "sample.txt").write_text("base\n", encoding="utf-8")

    def fake_clone(repo: str, target_dir: Path) -> Path:
        return repo_manager.copy_repo(source_repo, target_dir)

    monkeypatch.setattr(repo_manager, "clone_repo", fake_clone)

    instance = {
        "instance_id": "demo__repo-no-git",
        "repo": "demo/repo",
        "base_commit": "abc123",
        "patch": "non-empty patch",
    }

    with pytest.raises(repo_manager.RepoPreparationError, match="missing .git"):
        repo_manager.prepare_swebench_repos(instance, workdir=tmp_path / "prepared")


def test_require_git_repo_rejects_parent_worktree_leakage(tmp_path: Path):
    parent_repo = tmp_path / "parent"
    _make_local_repo(parent_repo)
    nested = parent_repo / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    (nested / ".git" / "HEAD").write_text("invalid\n", encoding="utf-8")

    with pytest.raises(repo_manager.RepoPreparationError, match="work tree root"):
        repo_manager.get_head_commit(nested)


def test_prepare_swebench_repos_rejects_patch_with_no_diff(tmp_path: Path, monkeypatch):
    source_repo = tmp_path / "source_repo"
    base_commit, _ = _make_local_repo(source_repo)

    def fake_clone(repo: str, target_dir: Path) -> Path:
        return repo_manager.copy_repo(source_repo, target_dir)

    def fake_apply_patch(repo_dir: Path, patch_text: str) -> bool:
        return True

    monkeypatch.setattr(repo_manager, "clone_repo", fake_clone)
    monkeypatch.setattr(repo_manager, "apply_patch", fake_apply_patch)

    instance = {
        "instance_id": "demo__repo-no-diff",
        "repo": "demo/repo",
        "base_commit": base_commit,
        "patch": "non-empty patch",
    }

    with pytest.raises(repo_manager.RepoPreparationError, match="produced no tracked diff"):
        repo_manager.prepare_swebench_repos(instance, workdir=tmp_path / "prepared")


def test_run_cmd_converts_timeout_to_failed_result():
    result = repo_manager.run_cmd(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        timeout=1,
    )

    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_run_cmd_timeout_can_be_configured_with_env(monkeypatch):
    monkeypatch.setenv("ECHO_REPRO_CMD_TIMEOUT", "1")
    result = repo_manager.run_cmd(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        timeout=120,
    )

    assert result.returncode == 124
