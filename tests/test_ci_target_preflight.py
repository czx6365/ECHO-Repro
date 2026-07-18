import subprocess
from pathlib import Path

from echo_ci.target_preflight import cross_commit_target_preflight


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def test_preflight_proves_target_deleted_by_fix(tmp_path: Path):
    failing_repo = tmp_path / "failing"
    failing_repo.mkdir()
    _git(failing_repo, "init")
    _git(failing_repo, "config", "user.email", "test@example.com")
    _git(failing_repo, "config", "user.name", "Test")
    target = failing_repo / "src" / "pkg" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_text("bad = True\n", encoding="utf-8")
    _git(failing_repo, "add", ".")
    _git(failing_repo, "commit", "-m", "failing")
    failing_commit = _git(failing_repo, "rev-parse", "HEAD")
    target.unlink()
    _git(failing_repo, "add", "-A")
    _git(failing_repo, "commit", "-m", "fixed")
    fixed_commit = _git(failing_repo, "rev-parse", "HEAD")
    fixed_repo = tmp_path / "fixed"
    subprocess.run(["git", "clone", str(failing_repo), str(fixed_repo)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _git(failing_repo, "checkout", "--detach", failing_commit)

    result = cross_commit_target_preflight(
        failing_repo=failing_repo,
        fixed_repo=fixed_repo,
        failing_commit=failing_commit,
        fixed_commit=fixed_commit,
        run_text="flake8 src/pkg/module.py",
        reproduce_text="",
    )

    record = result["targets"][0]
    assert record["exists_on_failing"] is True
    assert record["exists_on_fixed"] is False
    assert record["changed_by_patch"] is True
    assert record["classification"] == "target_deleted_by_fix"
    assert result["all_targets_deleted_by_fix"] is True
