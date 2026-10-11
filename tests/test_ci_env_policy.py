from pathlib import Path
from types import SimpleNamespace
import os

import echo_ci.env_policy as env_policy_module
from echo_ci.env_policy import (
    build_execution_env,
    detect_ci_tool_matches,
    detect_ci_tools,
    run_sh_uses_disallowed_install,
    setup_environment,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_detect_ci_tools_from_run_sh():
    run_text = """
pytest tests/test_api.py
python -m ruff check .
flake8 src
"""

    assert detect_ci_tools(run_text) == ["flake8", "pytest", "ruff"]


def test_e1_adds_pythonpath_without_installing(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    commands = []
    monkeypatch.setattr(env_policy_module, "_run", lambda command, **kwargs: commands.append(command) or _completed())
    env_setup, env = setup_environment(
        repo_dir=repo,
        policy="E1_pythonpath_only",
        run_text="python -c 'print(1)'",
        timeout=60,
    )

    assert env_setup.policy == "E1_pythonpath_only"
    assert env_setup.install_commands == []
    assert not any(command[-1] in {"pytest", "ruff", "flake8"} for command in commands)
    assert str(repo) in env["PYTHONPATH"].split(":")


def test_e2_builds_install_command_only_for_missing_ci_tools(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(env_policy_module, "_run", lambda command, **kwargs: _completed())
    monkeypatch.setattr(env_policy_module.shutil, "which", lambda tool, path=None: None)
    env_setup, _ = setup_environment(
        repo_dir=repo,
        policy="E2_tool_bootstrap",
        run_text="definitely-not-a-ci-tool --version\npytest --version\n",
        timeout=120,
    )

    assert "pytest" in [item["tool"] for item in env_setup.tools_detected]
    assert "definitely-not-a-ci-tool" not in env_setup.tools_detected
    assert all(command[-1] == "pytest" for command in env_setup.install_commands)


def test_e2_does_not_install_requirements_or_project_package():
    run_text = """
python -m pip install -r requirements.txt
python -m pip install .
poetry install
apt-get update
docker build .
    """

    assert run_sh_uses_disallowed_install(run_text) is True
    assert "pytest" not in detect_ci_tools(run_text)


def test_build_execution_env_adds_venv_path(tmp_path: Path):
    repo = tmp_path / "repo"
    venv = repo / ".echo_venv"
    repo.mkdir()

    env = build_execution_env(repo, venv, include_pythonpath=True)

    assert str(venv / "bin") in env["PATH"]
    assert env["REPO_ROOT"] == str(repo.resolve())


def test_detect_pytest_from_subprocess_list():
    matches = detect_ci_tool_matches({"reproduce.py": 'subprocess.run(["pytest", "tests"])'})

    assert {"tool": "pytest", "source": "reproduce.py", "pattern": "subprocess.run"} in matches


def test_detect_ruff_from_subprocess_shell_string():
    matches = detect_ci_tool_matches({"reproduce.py": 'subprocess.run("ruff check .", shell=True)'})

    assert {"tool": "ruff", "source": "reproduce.py", "pattern": "subprocess.run"} in matches


def test_detect_pre_commit_from_os_system():
    matches = detect_ci_tool_matches({"reproduce.py": 'os.system("pre-commit run")'})

    assert {"tool": "pre-commit", "source": "reproduce.py", "pattern": "os.system"} in matches


def test_detect_poetry_from_reproduce_py():
    matches = detect_ci_tool_matches({"reproduce.py": 'subprocess.run(["poetry", "run", "pytest"])'})
    tools = {item["tool"] for item in matches}

    assert "poetry" in tools
    assert "pytest" in tools


def test_env_setup_diagnostics_include_source_and_pattern(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(env_policy_module, "_run", lambda command, **kwargs: _completed())
    monkeypatch.setattr(env_policy_module.shutil, "which", lambda tool, path=None: str(repo / ".echo_venv" / "bin" / tool))

    env_setup, _ = setup_environment(
        repo_dir=repo,
        policy="E2_tool_bootstrap",
        run_text="",
        reproduce_text='subprocess.run(["pytest"])',
        timeout=60,
    )

    assert {"tool": "pytest", "source": "reproduce.py", "pattern": "subprocess.run"} in env_setup.tools_detected


def test_e2_ignores_global_tool_and_installs_into_experiment_venv(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(env_policy_module, "_run", lambda command, **kwargs: _completed())

    def fake_which(tool: str, path: str | None = None):
        if path == os.environ.get("PATH", ""):
            return f"/usr/local/bin/{tool}"
        return None

    monkeypatch.setattr(env_policy_module.shutil, "which", fake_which)
    env_setup, _ = setup_environment(
        repo_dir=repo,
        policy="E2_tool_bootstrap",
        run_text="mypy --version",
        timeout=60,
    )

    assert env_setup.install_commands[-1][-1] == "mypy"
    assert env_setup.external_tools_ignored == {"mypy": "/usr/local/bin/mypy"}
    assert env_setup.unresolved_tools == ["mypy"]


def test_env_setup_records_isolated_tool_resolution(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    tool = repo / ".echo_venv" / "bin" / "mypy"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setattr(env_policy_module, "_run", lambda command, **kwargs: _completed())

    env_setup, env = setup_environment(
        repo_dir=repo,
        policy="E2_tool_bootstrap",
        run_text="mypy --version",
        timeout=60,
    )

    assert env_setup.resolved_tools["mypy"] == str(tool.resolve())
    assert env_setup.python_executable.endswith(".echo_venv/bin/python")
    assert env_setup.execution_path == env["PATH"]
