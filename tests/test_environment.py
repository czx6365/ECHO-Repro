import subprocess
from pathlib import Path

import echo_repro.environment as environment_module
from echo_repro.environment import (
    EnvironmentProfileManager,
    EnvironmentRepairManager,
    dependency_files_for_repo,
    detect_python_version,
    find_python_executable,
    hash_dependency_files,
    package_for_module,
    parse_missing_module,
    slugify_repo,
)
from echo_repro.models import ExecutionResult


def test_parse_missing_module_from_modulenotfounderror():
    stderr = "ModuleNotFoundError: No module named 'erfa'"
    assert parse_missing_module(stderr) == "erfa"


def test_package_for_module_uses_known_overrides():
    assert package_for_module("erfa") == "pyerfa"
    assert package_for_module("yaml") == "PyYAML"
    assert package_for_module("missing_module") == "missing-module"


def test_slugify_repo_for_cache_path():
    assert slugify_repo("astropy/astropy") == "astropy__astropy"


def test_environment_repair_manager_records_failed_parse(tmp_path: Path):
    manager = EnvironmentRepairManager(repo_slug="demo/repo", env_root=tmp_path / "envs")
    result = ExecutionResult(
        repo_path=tmp_path,
        command="python reproduce.py",
        returncode=1,
        stdout="",
        stderr="ImportError: something else",
    )

    repair = manager.repair_dependency(result)

    assert repair.attempted is False
    assert repair.success is False
    assert "parse" in repair.reason


def test_environment_repair_manager_can_target_profile_env(tmp_path: Path):
    profile_env = tmp_path / "envs" / "profile"
    manager = EnvironmentRepairManager(
        repo_slug="demo/repo",
        env_root=tmp_path / "envs",
        env_path_override=profile_env,
    )

    assert manager.env_path == profile_env
    assert manager.python_path == profile_env / "bin" / "python"


def test_dependency_hash_changes_with_environment_files(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = repo / "requirements.txt"
    requirements.write_text("numpy>=1.20\n", encoding="utf-8")

    first_hash = hash_dependency_files(dependency_files_for_repo(repo), repo_path=repo)
    requirements.write_text("numpy>=1.21\n", encoding="utf-8")
    second_hash = hash_dependency_files(dependency_files_for_repo(repo), repo_path=repo)

    assert first_hash != second_hash


def test_detect_python_version_prefers_tox_supported_version(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tox.ini").write_text(
        "[tox]\nenvlist = py{38,39,310}-test\n",
        encoding="utf-8",
    )

    assert detect_python_version(repo, current_version="3.14") == "3.10"


def test_environment_profile_no_install_does_not_create_env(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tox.ini").write_text(
        "[tox]\nenvlist = py{38,39,310}-test\n",
        encoding="utf-8",
    )
    env_root = tmp_path / "envs"

    profile = EnvironmentProfileManager(
        repo="astropy/astropy",
        repo_path=repo,
        env_root=env_root,
        allow_install=False,
    ).prepare_profile()

    assert profile.ready is False
    assert profile.attempted is False
    assert profile.detected_python == "3.10"
    assert profile.profile_key.startswith("astropy__astropy-py310-")
    assert profile.env_path is not None
    assert not profile.env_path.exists()
    assert not env_root.exists()


def test_find_python_executable_accepts_matching_base_python(tmp_path: Path, monkeypatch):
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")

    def fake_run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int = 300):
        return subprocess.CompletedProcess(cmd, 0, stdout="Python 3.10.13\n", stderr="")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)

    assert find_python_executable("3.10", fake_python) == fake_python
