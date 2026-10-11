import subprocess
from pathlib import Path

import echo_repro.environment as environment_module
from echo_repro.dependency_repair import install_with_llm, validate_repair_spec
from echo_repro.environment import EnvironmentRepairManager
from echo_repro.llm.base import BaseLLMClient, _lenient_json_loads
from echo_repro.models import ExecutionResult

PYTHON = "venv/bin/python"


def test_validate_normalizes_pip_install_forms():
    spec = {"install_commands": ["pip install numpy==1.21.6 --no-cache-dir"]}
    assert validate_repair_spec(spec, PYTHON, None) == [
        ([PYTHON, "-m", "pip", "install", "numpy==1.21.6", "--no-cache-dir"], None)
    ]

    spec = {"install_commands": ["python -m pip install six"]}
    assert validate_repair_spec(spec, PYTHON, None) == [
        ([PYTHON, "-m", "pip", "install", "six"], None)
    ]


def test_validate_rejects_escaped_flag_set():
    for flag in ("--user", "--target", "--root", "--prefix", "--system", "--break-system-packages"):
        spec = {"install_commands": [f"pip install numpy {flag} /tmp/x"]}
        assert validate_repair_spec(spec, PYTHON, None) is None, flag


def test_validate_rejects_unknown_flags_and_non_install_commands():
    assert validate_repair_spec({"install_commands": ["pip install -r requirements.txt"]}, PYTHON, None) is None
    assert validate_repair_spec({"install_commands": ["pip install -e git+https://github.com/x/y"]}, PYTHON, None) is None
    assert validate_repair_spec({"install_commands": ["pip install --bogus-flag numpy"]}, PYTHON, None) is None
    assert validate_repair_spec({"install_commands": ["pip uninstall numpy"]}, PYTHON, None) is None
    assert validate_repair_spec({"install_commands": ["sudo rm -rf /"]}, PYTHON, None) is None
    assert validate_repair_spec({"install_commands": ["conda install numpy"]}, PYTHON, None) is None
    assert validate_repair_spec({"install_commands": ["python -c 'import os'"]}, PYTHON, None) is None


def test_validate_rejects_non_https_index_urls():
    spec = {"install_commands": ["pip install numpy --index-url http://evil.example.com/simple"]}
    assert validate_repair_spec(spec, PYTHON, None) is None
    spec = {"install_commands": ["pip install numpy --index-url https://pypi.org/simple"]}
    assert validate_repair_spec(spec, PYTHON, None) is not None


def test_validate_rejects_paths_urls_and_git_refs_as_args():
    for arg in ("/tmp/pkg.whl", "https://example.com/x.whl", "git+https://github.com/a/b.git", "../numpy"):
        spec = {"install_commands": [f"pip install {arg}"]}
        assert validate_repair_spec(spec, PYTHON, None) is None, arg


def test_validate_dot_install_requires_repo_path():
    spec = {"install_commands": ["pip install ."]}
    assert validate_repair_spec(spec, PYTHON, None) is None
    assert validate_repair_spec(spec, PYTHON, "/tmp/repo") == [
        ([PYTHON, "-m", "pip", "install", "."], "/tmp/repo")
    ]
    spec = {"install_commands": ["pip install -e ."]}
    assert validate_repair_spec(spec, PYTHON, "/tmp/repo") == [
        ([PYTHON, "-m", "pip", "install", "-e", "."], "/tmp/repo")
    ]


def test_validate_drops_invalid_commands_and_truncates():
    spec = {
        "install_commands": [
            "sudo rm -rf /",
            "pip install numpy",
            "pip install scipy",
            "pip install pandas",
            "pip install matplotlib",
        ]
    }
    commands = validate_repair_spec(spec, PYTHON, None)
    assert commands is not None
    assert [cmd[-1] for cmd, _ in commands] == ["numpy", "scipy", "pandas"]


def test_validate_returns_none_for_garbage():
    assert validate_repair_spec({"install_commands": []}, PYTHON, None) is None
    assert validate_repair_spec({"install_commands": "pip install numpy"}, PYTHON, None) is None
    assert validate_repair_spec({"install_commands": [123]}, PYTHON, None) is None
    assert validate_repair_spec("not a dict", PYTHON, None) is None
    assert validate_repair_spec(None, PYTHON, None) is None


def test_lenient_json_loads_handles_fences_and_prose():
    assert _lenient_json_loads('{"a": 1}') == {"a": 1}
    assert _lenient_json_loads('```json\n{"a": 1}\n```') == {"a": 1}
    assert _lenient_json_loads('Sure! Here you go:\n{"a": {"b": [1, 2]}}\nHope that helps.') == {
        "a": {"b": [1, 2]}
    }


def test_lenient_json_loads_raises_for_garbage():
    import pytest

    with pytest.raises(ValueError):
        _lenient_json_loads("")
    with pytest.raises(ValueError):
        _lenient_json_loads("no json here at all")


class FencedJsonClient(BaseLLMClient):
    """generate_json raises ValueError, generate_text returns fenced JSON."""

    def __init__(self, spec: dict):
        self.spec = spec

    def generate_text(self, prompt: str) -> str:
        return f"```json\n{__import__('json').dumps(self.spec)}\n```"

    def generate_json(self, prompt: str) -> dict:
        raise ValueError("malformed json")

    def repair_dependency(self, **kwargs):
        return super().repair_dependency(**kwargs)


def test_repair_dependency_falls_back_to_lenient_text_parsing():
    client = FencedJsonClient(
        {"packages": [{"name": "numpy", "version_spec": "==1.21.6"}], "install_commands": [], "reason": "x"}
    )
    spec = client.repair_dependency(
        repo_slug="demo/demo",
        missing_module="numpy",
        stderr_tail="boom",
        python_version="Python 3.11",
        declaration_summary="(none)",
    )
    assert spec["packages"][0]["version_spec"] == "==1.21.6"
    assert "demo/demo" in client.last_prompt


class ScriptedRepairClient(BaseLLMClient):
    def __init__(self, specs: list[dict]):
        self.specs = list(specs)
        self.calls: list[dict] = []

    def generate_text(self, prompt: str) -> str:
        raise NotImplementedError

    def generate_json(self, prompt: str) -> dict:
        raise NotImplementedError

    def repair_dependency(self, **kwargs):
        self.calls.append(kwargs)
        spec = self.specs.pop(0)
        if isinstance(spec, Exception):
            raise spec
        return spec


class ExplodingRepairClient(BaseLLMClient):
    def generate_text(self, prompt: str) -> str:
        raise NotImplementedError

    def generate_json(self, prompt: str) -> dict:
        raise NotImplementedError

    def repair_dependency(self, **kwargs):
        raise RuntimeError("backend down")


def _manager(tmp_path: Path) -> EnvironmentRepairManager:
    manager = EnvironmentRepairManager(repo_slug="demo/repo", env_root=tmp_path / "envs")
    manager.env_path.mkdir(parents=True)
    (manager.env_path / "bin").mkdir()
    (manager.env_path / "bin" / "python").touch()
    return manager


def _result(stderr: str) -> ExecutionResult:
    return ExecutionResult(
        repo_path=Path("/tmp/demo_repo"),
        command="python reproduce.py",
        returncode=1,
        stdout="",
        stderr=stderr,
    )


def _ok_spec(package: str) -> dict:
    return {
        "packages": [{"name": package, "version_spec": "==1.0"}],
        "install_commands": [f"pip install {package}==1.0"],
        "reason": "the repo declares it",
    }


def _wrong_version_spec(package: str) -> dict:
    return {
        "packages": [{"name": package, "version_spec": "==99.0.0"}],
        "install_commands": [f"pip install {package}==99.0.0"],
        "reason": "a guess",
    }


def test_install_with_llm_success_path(tmp_path: Path, monkeypatch):
    manager = _manager(tmp_path)
    client = ScriptedRepairClient([_ok_spec("numpy")])
    calls = []

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        calls.append((cmd, cwd, timeout))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)

    repair = install_with_llm(
        manager, _result("ModuleNotFoundError: No module named 'numpy'"), tmp_path / "repo", client
    )

    assert repair is not None
    assert repair.success is True
    assert repair.strategy == "llm"
    assert repair.missing_module == "numpy"
    assert repair.install_command[-1] == "numpy==1.0"
    # calls[0] is the python --version probe; the install follows
    install_calls = [call for call in calls if "-m" in call[0] and "install" in call[0]]
    assert len(install_calls) == 1
    assert install_calls[0][0][0] == str(manager.python_path)
    assert install_calls[0][2] == 900


def test_install_with_llm_retries_with_feedback_then_succeeds(tmp_path: Path, monkeypatch):
    manager = _manager(tmp_path)
    client = ScriptedRepairClient([_wrong_version_spec("numpy"), _ok_spec("numpy")])
    attempts = []

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        attempts.append(cmd)
        if "==99.0.0" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no matching version for numpy==99.0.0")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)

    repair = install_with_llm(
        manager, _result("ModuleNotFoundError: No module named 'numpy'"), tmp_path / "repo", client
    )

    assert repair is not None
    assert repair.success is True
    install_calls = [cmd for cmd in attempts if "-m" in cmd and "install" in cmd]
    assert len(install_calls) == 2
    assert len(client.calls) == 2
    assert "numpy==99.0.0" in client.calls[1]["feedback"]


def test_install_with_llm_second_round_failure_is_llm_strategy_failure(tmp_path: Path, monkeypatch):
    manager = _manager(tmp_path)
    client = ScriptedRepairClient([_wrong_version_spec("numpy"), _ok_spec("numpy")])
    monkeypatch.setattr(
        environment_module,
        "run_cmd",
        lambda cmd, cwd=None, timeout=300, env=None: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="version not found"
        ),
    )

    repair = install_with_llm(
        manager, _result("ModuleNotFoundError: No module named 'numpy'"), tmp_path / "repo", client
    )

    assert repair is not None
    assert repair.success is False
    assert repair.strategy == "llm"


def test_install_with_llm_llm_exception_signals_mechanical_fallback(tmp_path: Path):
    manager = _manager(tmp_path)
    repair = install_with_llm(
        manager, _result("ModuleNotFoundError: No module named 'numpy'"), tmp_path / "repo", ExplodingRepairClient()
    )
    assert repair is None


def test_install_with_llm_invalid_spec_signals_mechanical_fallback(tmp_path: Path, monkeypatch):
    manager = _manager(tmp_path)
    client = ScriptedRepairClient([{"install_commands": ["sudo rm -rf /"]}])
    monkeypatch.setattr(
        environment_module,
        "run_cmd",
        lambda cmd, cwd=None, timeout=300, env=None: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    repair = install_with_llm(
        manager, _result("ModuleNotFoundError: No module named 'numpy'"), tmp_path / "repo", client
    )
    assert repair is None


def test_install_with_llm_forwards_manager_install_env(tmp_path: Path, monkeypatch):
    manager = _manager(tmp_path)
    manager._install_env = {"CFLAGS": "-Wno-error=incompatible-function-pointer-types"}
    manager._detected_python = "3.9"
    client = ScriptedRepairClient([_ok_spec("numpy")])
    calls = []

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        calls.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)

    repair = install_with_llm(
        manager, _result("ModuleNotFoundError: No module named 'numpy'"), tmp_path / "repo", client
    )

    assert repair is not None
    install_calls = [call for call in calls if "-m" in call[0] and "install" in call[0]]
    assert install_calls[0][1] == {"CFLAGS": "-Wno-error=incompatible-function-pointer-types"}
    assert repair.detected_python == "3.9"


def test_install_with_llm_timeout_is_treated_as_failure(tmp_path: Path, monkeypatch):
    manager = _manager(tmp_path)
    client = ScriptedRepairClient([_wrong_version_spec("numpy"), _ok_spec("numpy")])

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)

    repair = install_with_llm(
        manager, _result("ModuleNotFoundError: No module named 'numpy'"), tmp_path / "repo", client
    )

    assert repair is not None
    assert repair.success is False
    assert repair.strategy == "llm"
    assert "timed out" in repair.stderr.lower()
