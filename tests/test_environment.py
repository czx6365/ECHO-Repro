from pathlib import Path

from echo_repro.environment import (
    EnvironmentRepairManager,
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
