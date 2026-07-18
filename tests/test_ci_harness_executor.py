import json
import subprocess
from pathlib import Path
from typing import Any

import echo_ci.harness_executor as harness_executor
from echo_ci.env_policy import EnvSetupResult
from echo_ci.harness_executor import install_harness, run_harness
from echo_ci.harness_generator import ensure_run_sh_header, normalize_run_sh, preserve_workflow_execution_signals
from echo_ci.ci_context_retriever import ContextBundle


def _write_generated(path: Path, run_text: str = "python reproduce.py\n") -> None:
    path.mkdir(parents=True)
    (path / "reproduce.py").write_text("print('ok')\n", encoding="utf-8")
    (path / "run.sh").write_text(run_text, encoding="utf-8")
    (path / "oracle.json").write_text(json.dumps({"must_contain_on_failing": ["ok"]}), encoding="utf-8")
    (path / "generation_metadata.json").write_text(json.dumps({"status": "generated"}), encoding="utf-8")


def test_install_harness_copies_artifacts_to_repo_dirs(tmp_path: Path):
    generated = tmp_path / "generated"
    failing_repo = tmp_path / "failing_repo"
    fixed_repo = tmp_path / "fixed_repo"
    failing_repo.mkdir()
    fixed_repo.mkdir()
    _write_generated(generated)

    failing_ok, failing_error, failing_preflight = install_harness(failing_repo, generated)
    fixed_ok, fixed_error, fixed_preflight = install_harness(fixed_repo, generated)

    assert failing_ok is True
    assert fixed_ok is True
    assert failing_error == ""
    assert fixed_error == ""
    for repo in (failing_repo, fixed_repo):
        assert (repo / ".echo_repro" / "reproduce.py").exists()
        assert (repo / ".echo_repro" / "run.sh").exists()
        assert (repo / ".echo_repro" / "oracle.json").exists()
        assert (repo / ".echo_repro" / "generation_metadata.json").exists()
    assert failing_preflight["reproduce_py_exists"] is True
    assert fixed_preflight["run_sh_exists"] is True


def test_run_sh_normalization_uses_repo_root_reproduce():
    run = ensure_run_sh_header(normalize_run_sh('cd "$(dirname "$0")"\npython reproduce.py\n'))

    assert 'REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"' in run
    assert '"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"' in run
    assert 'cd "$(dirname "$0")"' not in run


def test_s5_postprocessor_injects_workflow_working_directory():
    context = ContextBundle(
        workflow="""
jobs:
  lint:
    defaults:
      run:
        working-directory: libs/agno
    steps:
      - run: ruff check .
""",
        failure_spec={"failed_command": "ruff check .", "error_signature": "F401"},
    )
    run, adjustments = preserve_workflow_execution_signals(
        run_text="ruff check .\n",
        reproduce_text="",
        context=context,
        grounding={"selected_command": "ruff check ."},
        setting="S5_full_context",
    )

    assert 'cd "$REPO_ROOT/libs/agno"' in run
    assert adjustments == [{"kind": "working_directory", "value": "libs/agno", "source": "workflow"}]


def test_issue_only_postprocessor_does_not_leak_workflow_context():
    context = ContextBundle(
        workflow="jobs:\n  lint:\n    defaults:\n      run:\n        working-directory: libs/agno\n    steps:\n      - run: ruff check .\n",
        failure_spec={"failed_command": "ruff check ."},
    )
    run, adjustments = preserve_workflow_execution_signals(
        run_text="ruff check .\n",
        reproduce_text="",
        context=context,
        grounding={"selected_command": "ruff check ."},
        setting="S1_issue_only",
    )

    assert "libs/agno" not in run
    assert adjustments == []


def test_run_header_normalization_is_idempotent_and_keeps_scoped_cd():
    source = ensure_run_sh_header(
        'cd "$REPO_ROOT/framework"\n'
        '"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"\n'
    )

    assert ensure_run_sh_header(source) == source
    assert source.index('cd "$REPO_ROOT/framework"') > source.index('cd "$REPO_ROOT"')


def test_dynamic_retry_installs_missing_allowlisted_tool_once(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    echo = repo / ".echo_repro"
    echo.mkdir(parents=True)
    (echo / "run.sh").write_text("flake8 src\n", encoding="utf-8")
    (echo / "reproduce.py").write_text("print('x')\n", encoding="utf-8")
    (echo / "oracle.json").write_text("{}", encoding="utf-8")
    env_setup = EnvSetupResult(policy="E2_tool_bootstrap", venv_path=str(repo / ".echo_venv"))
    calls: list[list[str]] = []
    installs: list[str] = []

    def fake_run(command: list[str], **kwargs: Any):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 127, "", "flake8: command not found\n")
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    def fake_setup(**kwargs: Any):
        return env_setup, {}

    def fake_install(**kwargs: Any):
        tool = kwargs["tool"]
        installs.append(tool)
        kwargs["diagnostics"].dynamic_missing_tools.append(tool)
        kwargs["diagnostics"].dynamic_installed_tools.append(tool)
        return True

    monkeypatch.setattr(harness_executor, "_run_command", fake_run)
    monkeypatch.setattr(harness_executor, "setup_environment", fake_setup)
    monkeypatch.setattr(harness_executor, "install_ci_tool", fake_install)
    monkeypatch.setattr(harness_executor, "build_execution_env", lambda *args, **kwargs: {})

    result = run_harness(
        repo,
        phase="fixed",
        timeout_seconds=120,
        env_policy="E2_tool_bootstrap",
        preflight={
            "echo_repro_dir_exists": True,
            "run_sh_exists": True,
            "reproduce_py_exists": True,
            "oracle_json_exists": True,
            "run_sh_content": "flake8 src\n",
            "reproduce_py_content": "",
            "oracle_json_content": "{}",
            "generation_metadata_content": "",
        },
    )

    assert result.exit_code == 0
    assert installs == ["flake8"]
    assert result.env_setup["dynamic_retry"] is True
    assert result.env_setup["dynamic_retry_used"] is True
    assert result.env_setup["dynamic_retry_exit_code"] == 0
    assert result.env_setup["dynamic_installed_tools"] == ["flake8"]


def test_dynamic_retry_does_not_install_non_allowlisted_package(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    echo = repo / ".echo_repro"
    echo.mkdir(parents=True)
    (echo / "run.sh").write_text("python script.py\n", encoding="utf-8")
    (echo / "reproduce.py").write_text("print('x')\n", encoding="utf-8")
    (echo / "oracle.json").write_text("{}", encoding="utf-8")
    env_setup = EnvSetupResult(policy="E2_tool_bootstrap", venv_path=str(repo / ".echo_venv"))
    installs: list[str] = []

    monkeypatch.setattr(
        harness_executor,
        "_run_command",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 127, "", "requests: command not found\n"),
    )
    monkeypatch.setattr(harness_executor, "setup_environment", lambda **kwargs: (env_setup, {}))
    monkeypatch.setattr(harness_executor, "install_ci_tool", lambda **kwargs: installs.append(kwargs["tool"]) or True)

    result = run_harness(
        repo,
        phase="fixed",
        timeout_seconds=120,
        env_policy="E2_tool_bootstrap",
        preflight={
            "echo_repro_dir_exists": True,
            "run_sh_exists": True,
            "reproduce_py_exists": True,
            "oracle_json_exists": True,
            "run_sh_content": "python script.py\n",
            "reproduce_py_content": "",
            "oracle_json_content": "{}",
            "generation_metadata_content": "",
        },
    )

    assert result.exit_code == 127
    assert installs == []
    assert result.env_setup["dynamic_retry_used"] is False
