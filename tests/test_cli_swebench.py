import json
from pathlib import Path

from typer.testing import CliRunner

import echo_repro.cli as cli_module
from echo_repro.cli import app
from echo_repro.models import PreparedRepos

runner = CliRunner()


def _make_mock_repo(repo_dir: Path, fixed: bool) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    if fixed:
        body = (
            'def divide(a, b):\n'
            '    if b == 0:\n'
            '        raise ZeroDivisionError("division by zero")\n'
            "    return a / b\n"
        )
    else:
        body = (
            "def divide(a, b):\n"
            "    if b == 0:\n"
            "        return 0\n"
            "    return a / b\n"
        )
    (repo_dir / "buggy_module.py").write_text(body, encoding="utf-8")
    (repo_dir / "test_buggy_module.py").write_text(
        "from buggy_module import divide\n",
        encoding="utf-8",
    )
    (repo_dir / "requirements.txt").write_text("pytest>=8\n", encoding="utf-8")


def test_run_swebench_one_creates_result_json(tmp_path: Path, monkeypatch):
    instances_file = tmp_path / "instances.jsonl"
    instance = {
        "instance_id": "demo__repo-1",
        "repo": "demo/repo",
        "version": "lite",
        "base_commit": "abc123",
        "problem_statement": "divide(a, b) should raise ZeroDivisionError when b is zero",
        "patch": "unused in mocked prepare",
    }
    instances_file.write_text(json.dumps(instance) + "\n", encoding="utf-8")

    buggy_repo = tmp_path / "prepared" / "demo__repo-1" / "buggy"
    fixed_repo = tmp_path / "prepared" / "demo__repo-1" / "fixed"
    _make_mock_repo(buggy_repo, fixed=False)
    _make_mock_repo(fixed_repo, fixed=True)

    def fake_prepare(instance: dict, workdir: Path) -> PreparedRepos:
        return PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            patch_applied=True,
        )

    monkeypatch.setattr(cli_module, "prepare_swebench_repos", fake_prepare)

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-1",
                "--workdir",
                "repos",
                "--mock",
                "--max-attempts",
                "3",
            ],
        )
        assert result.exit_code == 0, result.stdout
        output_path = Path("outputs/demo__repo-1/result.json")
        assert output_path.exists()
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert payload["instance_metadata"]["instance_id"] == "demo__repo-1"
        assert payload["prepared_repos"]["patch_applied"] is True
        assert payload["execution"]["buggy"]["status"] == "reproduced"
        assert payload["execution"]["fixed"]["status"] == "resolved"
        assert payload["validation"]["success"] is True
        assert isinstance(payload["attempts"], list)


def test_existing_version_command_still_works():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_run_swebench_one_accepts_llm_mock(tmp_path: Path, monkeypatch):
    instances_file = tmp_path / "instances.jsonl"
    instance = {
        "instance_id": "demo__repo-2",
        "repo": "demo/repo",
        "version": "lite",
        "base_commit": "abc123",
        "problem_statement": "divide(a, b) should raise ZeroDivisionError when b is zero",
        "patch": "unused in mocked prepare",
    }
    instances_file.write_text(json.dumps(instance) + "\n", encoding="utf-8")

    buggy_repo = tmp_path / "prepared" / "demo__repo-2" / "buggy"
    fixed_repo = tmp_path / "prepared" / "demo__repo-2" / "fixed"
    _make_mock_repo(buggy_repo, fixed=False)
    _make_mock_repo(fixed_repo, fixed=True)

    def fake_prepare(instance: dict, workdir: Path) -> PreparedRepos:
        return PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            patch_applied=True,
        )

    monkeypatch.setattr(cli_module, "prepare_swebench_repos", fake_prepare)

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-2",
                "--workdir",
                "repos",
                "--llm",
                "mock",
            ],
        )
        assert result.exit_code == 0, result.stdout


def test_run_swebench_one_openai_without_api_key_gives_clear_error(tmp_path: Path, monkeypatch):
    instances_file = tmp_path / "instances.jsonl"
    instance = {
        "instance_id": "demo__repo-3",
        "repo": "demo/repo",
        "version": "lite",
        "base_commit": "abc123",
        "problem_statement": "divide(a, b) should raise ZeroDivisionError when b is zero",
        "patch": "unused in mocked prepare",
    }
    instances_file.write_text(json.dumps(instance) + "\n", encoding="utf-8")

    buggy_repo = tmp_path / "prepared" / "demo__repo-3" / "buggy"
    fixed_repo = tmp_path / "prepared" / "demo__repo-3" / "fixed"
    _make_mock_repo(buggy_repo, fixed=False)
    _make_mock_repo(fixed_repo, fixed=True)

    def fake_prepare(instance: dict, workdir: Path) -> PreparedRepos:
        return PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            patch_applied=True,
        )

    monkeypatch.setattr(cli_module, "prepare_swebench_repos", fake_prepare)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-3",
                "--workdir",
                "repos",
                "--llm",
                "openai",
            ],
        )
        assert result.exit_code != 0
        assert "OPENAI_API_KEY" in result.stdout
