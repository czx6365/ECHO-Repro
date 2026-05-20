import json
from pathlib import Path

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
    assert prepared.buggy_repo.exists()
    assert prepared.fixed_repo.exists()
    assert (prepared.buggy_repo / "sample.txt").read_text(encoding="utf-8") == "base\n"
    assert (prepared.fixed_repo / "sample.txt").read_text(encoding="utf-8") == "fixed\n"
