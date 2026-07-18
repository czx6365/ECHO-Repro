from pathlib import Path

from echo_ci.repo_manager import CheckoutResult, GitCommandResult, RobustRepoManager


class FakeRepoManager(RobustRepoManager):
    def __init__(self, tmp_path: Path, responses: dict[tuple[str, ...], GitCommandResult] | None = None):
        super().__init__(cache_dir=tmp_path / "cache", work_dir=tmp_path / "work", timeout=30)
        self.commands: list[list[str]] = []
        self.responses = responses or {}

    def _run(self, command: list[str], *, cwd: Path) -> GitCommandResult:
        self.commands.append(command)
        key = tuple(command)
        if key in self.responses:
            return self.responses[key]
        return GitCommandResult(command=command, cwd=str(cwd), returncode=0, stdout="")


def _result(command: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> GitCommandResult:
    return GitCommandResult(command=command, cwd="", returncode=returncode, stdout=stdout, stderr=stderr)


def test_prepare_repo_clone_command_uses_partial_no_checkout_cache(tmp_path: Path):
    manager = FakeRepoManager(tmp_path)

    manager.prepare_repo("owner/repo")

    clone_commands = [command for command in manager.commands if command[:2] == ["git", "clone"]]
    assert clone_commands
    clone = clone_commands[0]
    assert "--depth" not in clone
    assert "--filter=blob:none" in clone
    assert "--no-checkout" in clone


def test_missing_commit_object_classification(tmp_path: Path):
    cat = ["env", "GIT_NO_LAZY_FETCH=1", "git", "cat-file", "-t", "abc"]
    fetch_depth = ["git", "fetch", "origin", "abc", "--depth=1"]
    fetch_full = ["git", "fetch", "origin", "abc"]
    manager = FakeRepoManager(
        tmp_path,
        responses={
            tuple(cat): _result(cat, returncode=128, stderr="fatal: Not a valid object name abc"),
            tuple(fetch_depth): _result(fetch_depth, returncode=128, stderr="fatal: couldn't find remote ref abc"),
            tuple(fetch_full): _result(fetch_full, returncode=128, stderr="fatal: couldn't find remote ref abc"),
        },
    )

    result = manager.ensure_commit_available(tmp_path, "abc")

    assert result.missing_commit_object is True
    assert result.missing_tree_object is False
    assert result.stage == "cat_file"
    assert len(result.fetch_attempts) == 2


def test_missing_tree_object_classification(tmp_path: Path):
    cat = ["env", "GIT_NO_LAZY_FETCH=1", "git", "cat-file", "-t", "abc"]
    tree = ["env", "GIT_NO_LAZY_FETCH=1", "git", "rev-parse", "abc^{tree}"]
    fetch_depth = ["git", "fetch", "origin", "abc", "--depth=1"]
    fetch_full = ["git", "fetch", "origin", "abc"]
    manager = FakeRepoManager(
        tmp_path,
        responses={
            tuple(cat): _result(cat, stdout="commit\n"),
            tuple(tree): _result(tree, returncode=128, stderr="fatal: unable to read tree abc"),
            tuple(fetch_depth): _result(fetch_depth),
            tuple(fetch_full): _result(fetch_full),
        },
    )

    result = manager.ensure_commit_available(tmp_path, "abc")

    assert result.missing_commit_object is False
    assert result.missing_tree_object is True
    assert result.stage == "tree_check"


def test_checkout_diagnostics_serialization():
    checkout = CheckoutResult(
        repo="owner/repo",
        commit="abc",
        dest_dir="/tmp/repo",
        repo_prepare_strategy="aggressive_fetch",
        commit_check={"commit": "abc", "missing_tree_object": True},
        checkout_success=False,
        checkout_error="fatal: unable to read tree",
        checkout_stage="tree_check",
    )

    data = checkout.to_dict()

    assert data["commit_check"]["missing_tree_object"] is True
    assert data["checkout_stage"] == "tree_check"


def test_cleanup_checkout_removes_cloned_execution_tree(tmp_path: Path):
    manager = RobustRepoManager(cache_dir=tmp_path / "cache", work_dir=tmp_path / "work")
    checkout = tmp_path / "work" / "case" / "failing_repo"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "module.py").write_text("pass\n", encoding="utf-8")

    assert manager.cleanup_checkout(checkout) is True
    assert not checkout.exists()
