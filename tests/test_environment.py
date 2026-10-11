import subprocess
from pathlib import Path

import echo_repro.environment as environment_module
from echo_repro.environment import (
    EnvironmentProfileManager,
    EnvironmentRepairManager,
    _astropy_build_ext_env,
    _astropy_import_needs_build_ext,
    _legacy_runtime_constraints,
    _needs_astropy_build_ext_fallback,
    _needs_non_editable_fallback,
    _needs_source_checkout_build_ext,
    dependency_files_for_repo,
    detect_python_version,
    find_python_executable,
    hash_dependency_files,
    package_for_module,
    parse_missing_module,
    slugify_repo,
)
from echo_repro.models import EnvironmentRepairResult, ExecutionResult


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

    assert manager.env_path == profile_env.resolve()
    assert manager.python_path == profile_env.resolve() / "bin" / "python"


def test_environment_repair_manager_uses_absolute_default_env_path(tmp_path: Path):
    manager = EnvironmentRepairManager(repo_slug="demo/repo", env_root=tmp_path / "envs")

    assert manager.env_path == (tmp_path / "envs" / "demo__repo").resolve()
    assert manager.python_path == (tmp_path / "envs" / "demo__repo" / "bin" / "python").resolve()


def test_repair_dependency_uses_era_suffixed_path_and_interpreter(tmp_path: Path, monkeypatch):
    manager = EnvironmentRepairManager(repo_slug="demo/repo", env_root=tmp_path / "envs")
    era_python = tmp_path / "python3.9"
    era_python.touch()
    monkeypatch.setattr(
        environment_module, "detect_python_version", lambda repo_path, current_version=None: "3.9"
    )
    monkeypatch.setattr(
        environment_module, "find_python_executable", lambda version, base_python: era_python
    )
    calls = []

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        calls.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)
    repo = tmp_path / "repo"
    repo.mkdir()

    repair = manager.repair_dependency(
        _repair_result("ModuleNotFoundError: No module named 'numpy'"), repo_path=repo
    )

    assert repair.env_path == (tmp_path / "envs" / "demo__repo-py39").resolve()
    assert repair.detected_python == "3.9"
    create_cmd = calls[0][0]
    assert create_cmd[0] == str(era_python)
    assert create_cmd[1:] == ["-m", "venv", str(manager.env_path)]
    install_calls = [call for call in calls if "-m" in call[0] and "install" in call[0]]
    assert install_calls and install_calls[0][1] is not None
    assert "incompatible-function-pointer-types" in install_calls[0][1]["CFLAGS"]
    assert repair.install_command[0] == str(manager.python_path)


def test_repair_dependency_era_missing_interpreter_falls_back(tmp_path: Path, monkeypatch):
    manager = EnvironmentRepairManager(repo_slug="demo/repo", env_root=tmp_path / "envs")
    monkeypatch.setattr(
        environment_module, "detect_python_version", lambda repo_path, current_version=None: "3.9"
    )
    monkeypatch.setattr(
        environment_module, "find_python_executable", lambda version, base_python: None
    )
    calls = []

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        calls.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)

    repair = manager.repair_dependency(
        _repair_result("ModuleNotFoundError: No module named 'numpy'"), repo_path=tmp_path
    )

    assert repair.env_path == (tmp_path / "envs" / "demo__repo").resolve()
    assert calls[0][0][0] == str(manager.base_python)
    assert calls[1][1] is None  # no CFLAGS env on fallback


def test_repair_dependency_host_version_keeps_bare_path(tmp_path: Path, monkeypatch):
    manager = EnvironmentRepairManager(repo_slug="demo/repo", env_root=tmp_path / "envs")
    import sys as _sys

    host = f"{_sys.version_info.major}.{_sys.version_info.minor}"
    monkeypatch.setattr(
        environment_module, "detect_python_version", lambda repo_path, current_version=None: host
    )

    def boom(version, base_python):
        raise AssertionError("find_python_executable must not run for the host version")

    monkeypatch.setattr(environment_module, "find_python_executable", boom)
    calls = []

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        calls.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)

    repair = manager.repair_dependency(
        _repair_result("ModuleNotFoundError: No module named 'numpy'"), repo_path=tmp_path
    )

    assert repair.env_path == (tmp_path / "envs" / "demo__repo").resolve()
    assert calls[0][0][0] == str(manager.base_python)
    assert calls[1][1] is None


def _repair_result(stderr: str) -> ExecutionResult:
    return ExecutionResult(
        repo_path=Path("/tmp/demo_repo"),
        command="python reproduce.py",
        returncode=1,
        stdout="",
        stderr=stderr,
    )


def _manager_with_existing_venv(tmp_path: Path) -> EnvironmentRepairManager:
    manager = EnvironmentRepairManager(repo_slug="demo/repo", env_root=tmp_path / "envs")
    manager.env_path.mkdir(parents=True)
    (manager.env_path / "bin").mkdir()
    (manager.env_path / "bin" / "python").touch()
    return manager


def test_mechanical_repair_marks_strategy(tmp_path: Path, monkeypatch):
    manager = _manager_with_existing_venv(tmp_path)
    monkeypatch.setattr(
        environment_module,
        "run_cmd",
        lambda cmd, cwd=None, timeout=300, env=None: subprocess.CompletedProcess(
            cmd, 0, stdout="installed", stderr=""
        ),
    )

    repair = manager.repair_dependency(_repair_result("ModuleNotFoundError: No module named 'numpy'"))

    assert repair.success is True
    assert repair.strategy == "mechanical"
    assert repair.package == "numpy"


def test_mechanical_repair_survives_timeout_expired(tmp_path: Path, monkeypatch):
    manager = _manager_with_existing_venv(tmp_path)

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)

    repair = manager.repair_dependency(_repair_result("ModuleNotFoundError: No module named 'numpy'"))

    assert repair.success is False
    assert repair.strategy == "mechanical"
    assert repair.returncode == -1


def test_repair_dependency_dispatches_to_llm_when_available(tmp_path: Path, monkeypatch):
    import echo_repro.dependency_repair as dependency_repair_module

    manager = _manager_with_existing_venv(tmp_path)
    canned = EnvironmentRepairResult(
        attempted=True,
        success=True,
        missing_module="numpy",
        package="numpy",
        strategy="llm",
        reason="LLM-driven dependency installation succeeded.",
    )
    captured = {}

    def fake_install_with_llm(mgr, result, repo_path, llm_client):
        captured["manager"] = mgr
        captured["repo_path"] = repo_path
        captured["llm_client"] = llm_client
        return canned

    monkeypatch.setattr(dependency_repair_module, "install_with_llm", fake_install_with_llm)

    client = object()
    repair = manager.repair_dependency(
        _repair_result("ModuleNotFoundError: No module named 'numpy'"),
        llm_client=client,
        repo_path=tmp_path / "repo",
    )

    assert repair is canned
    assert captured["manager"] is manager
    assert captured["repo_path"] == tmp_path / "repo"
    assert captured["llm_client"] is client


def test_repair_dependency_falls_back_to_mechanical_without_client(tmp_path: Path, monkeypatch):
    import echo_repro.dependency_repair as dependency_repair_module

    manager = _manager_with_existing_venv(tmp_path)
    called = []

    def fake_install_with_llm(mgr, result, repo_path, llm_client):
        called.append(True)
        return EnvironmentRepairResult(success=True)

    monkeypatch.setattr(dependency_repair_module, "install_with_llm", fake_install_with_llm)
    monkeypatch.setattr(
        environment_module,
        "run_cmd",
        lambda cmd, cwd=None, timeout=300, env=None: subprocess.CompletedProcess(
            cmd, 0, stdout="installed", stderr=""
        ),
    )

    repair = manager.repair_dependency(_repair_result("ModuleNotFoundError: No module named 'numpy'"))

    assert called == []
    assert repair.strategy == "mechanical"


def test_venv_creation_failure_does_not_call_llm(tmp_path: Path, monkeypatch):
    import echo_repro.dependency_repair as dependency_repair_module

    manager = EnvironmentRepairManager(repo_slug="demo/repo", env_root=tmp_path / "envs")
    called = []
    monkeypatch.setattr(dependency_repair_module, "install_with_llm", lambda *a, **k: called.append(True))
    monkeypatch.setattr(
        environment_module,
        "run_cmd",
        lambda cmd, cwd=None, timeout=300, env=None: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="venv creation failed"
        ),
    )

    repair = manager.repair_dependency(
        _repair_result("ModuleNotFoundError: No module named 'numpy'"),
        llm_client=object(),
        repo_path=tmp_path / "repo",
    )

    assert called == []
    assert repair.success is False
    assert "venv" in repair.reason.lower() or "virtual environment" in repair.reason


def test_needs_source_checkout_build_ext_detects_astropy_message():
    stderr = (
        "ImportError: You appear to be trying to import astropy from within a "
        "source checkout or from an editable installation without building the "
        "extension modules first. Either run:\n\n"
        "  pip install -e .\n\nor\n\n  python setup.py build_ext --inplace\n"
    )
    assert _needs_source_checkout_build_ext(stderr) is True
    assert _needs_source_checkout_build_ext("ModuleNotFoundError: No module named 'numpy'") is False


ASTROPY_CHECKOUT_STDERR = (
    "Traceback (most recent call last):\n"
    "  File \"/repo/reproduce.py\", line 9, in main\n"
    "    from astropy.table import Table\n"
    "ImportError: You appear to be trying to import astropy from within a source checkout "
    "or from an editable installation without building the extension modules first. Either run:\n\n"
    "  pip install -e .\n\nor\n\n  python setup.py build_ext --inplace\n"
)


def test_repair_dependency_builds_source_checkout_extensions(tmp_path: Path, monkeypatch):
    manager = _manager_with_existing_venv(tmp_path)
    calls = []

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        calls.append((cmd, cwd, timeout, env))
        return subprocess.CompletedProcess(cmd, 0, stdout="installed", stderr="")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)
    repo = tmp_path / "repo"
    repo.mkdir()

    repair = manager.repair_dependency(
        _repair_result(ASTROPY_CHECKOUT_STDERR), repo_path=repo
    )

    assert repair.success is True
    assert repair.strategy == "mechanical"
    assert repair.install_command[1:] == ["-m", "pip", "install", "-e", "."]
    assert calls and calls[0][1] == repo
    assert calls[0][2] == 900


def test_repair_dependency_retries_source_checkout_build_on_failure(tmp_path: Path, monkeypatch):
    manager = _manager_with_existing_venv(tmp_path)
    attempts = []

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        attempts.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="build failed")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)
    repo = tmp_path / "repo"
    repo.mkdir()

    first = manager.repair_dependency(_repair_result(ASTROPY_CHECKOUT_STDERR), repo_path=repo)
    second = manager.repair_dependency(_repair_result(ASTROPY_CHECKOUT_STDERR), repo_path=repo)

    assert first.success is False
    assert second.success is False
    assert len(attempts) == 2  # failure resets _source_build_done so a later round retries


def test_repair_dependency_does_not_rebuild_after_success(tmp_path: Path, monkeypatch):
    manager = _manager_with_existing_venv(tmp_path)
    attempts = []

    def fake_run_cmd(cmd, cwd=None, timeout=300, env=None):
        attempts.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="installed", stderr="")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)
    repo = tmp_path / "repo"
    repo.mkdir()

    manager.repair_dependency(_repair_result(ASTROPY_CHECKOUT_STDERR), repo_path=repo)
    manager.repair_dependency(_repair_result(ASTROPY_CHECKOUT_STDERR), repo_path=repo)

    assert len(attempts) == 1  # success marks _source_build_done; no second build


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


def test_detect_python_version_caps_distutils_projects_at_python_311(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "setup.py").write_text(
        "from setuptools import setup\nsetup(python_requires='>=3.6')\n",
        encoding="utf-8",
    )
    package = repo / "package"
    package.mkdir()
    (package / "compat.py").write_text(
        "from distutils.version import LooseVersion\n",
        encoding="utf-8",
    )

    assert detect_python_version(repo, current_version="3.14") == "3.11"


def test_detect_python_version_caps_imp_projects_at_python_311(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "setup.py").write_text(
        "from setuptools import setup\nsetup(python_requires='>=3.6')\n",
        encoding="utf-8",
    )
    package = repo / "package"
    package.mkdir()
    (package / "compat.py").write_text(
        "import imp\nimp.reload(sys)\n",
        encoding="utf-8",
    )

    assert detect_python_version(repo, current_version="3.14") == "3.11"


def test_detect_python_version_does_not_flag_importlib_as_imp(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    package = repo / "package"
    package.mkdir()
    (package / "mod.py").write_text("import importlib\n", encoding="utf-8")

    assert detect_python_version(repo, current_version="3.14") == "3.14"


def test_detect_python_version_caps_removed_collections_abc_imports_at_python_39(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
    package = repo / "package"
    package.mkdir()
    (package / "compat.py").write_text(
        "from collections import Mapping, defaultdict\n",
        encoding="utf-8",
    )

    assert detect_python_version(repo, current_version="3.14") == "3.9"


def test_xarray_legacy_runtime_constraints_pin_numpy_and_pandas(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    package = repo / "xarray"
    package.mkdir()
    (package / "dtypes.py").write_text("value = np.unicode_\n", encoding="utf-8")

    assert _legacy_runtime_constraints("pydata__xarray", repo) == ["numpy<2", "pandas<2"]


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


def test_find_python_executable_does_not_use_mismatched_base_for_current_version(tmp_path: Path, monkeypatch):
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    current = f"{environment_module.sys.version_info.major}.{environment_module.sys.version_info.minor}"

    def fake_run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int = 300):
        return subprocess.CompletedProcess(cmd, 0, stdout="Python 3.7.17\n", stderr="")

    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)

    assert find_python_executable(current, fake_python) == Path(environment_module.sys.executable)


def test_non_editable_fallback_detects_missing_build_editable_hook():
    result = subprocess.CompletedProcess(
        ["python", "-m", "pip", "install", "-e", "."],
        1,
        stdout="",
        stderr=(
            "ERROR: Project file:///repo uses a build backend that is missing "
            "the 'build_editable' hook, so it cannot be installed in editable mode."
        ),
    )

    assert _needs_non_editable_fallback(result) is True


def test_astropy_build_ext_fallback_detects_failed_wheel_build():
    result = subprocess.CompletedProcess(
        ["python", "-m", "pip", "install", "."],
        1,
        stdout="ERROR: Failed building wheel for astropy",
        stderr="",
    )

    assert _needs_astropy_build_ext_fallback(result) is True


def test_astropy_build_ext_env_prefers_system_cfitsio():
    env = _astropy_build_ext_env()

    assert env["ASTROPY_USE_SYSTEM_CFITSIO"] == "1"
    assert "/opt/homebrew/lib/pkgconfig" in env["PKG_CONFIG_PATH"]


def test_astropy_import_check_detects_missing_build_ext_message():
    result = subprocess.CompletedProcess(
        ["python", "-c", "import astropy"],
        1,
        stdout="",
        stderr="ImportError: run python setup.py build_ext --inplace",
    )

    assert _astropy_import_needs_build_ext(result) is True


def test_environment_profile_does_not_reuse_stale_astropy_marker(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
    env_root = tmp_path / "envs"
    # prepare_profile derives the profile key from detect_python_version(repo)
    # on the current interpreter, so the env dir must follow the same
    # computation instead of hard-coding a Python version.
    detected = detect_python_version(repo)
    env_path = env_root / f"astropy__astropy-py{detected.replace('.', '')}-468fc3f6f8be"
    python_path = env_path / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    marker = env_path / ".echo-repro-profile-ready.json"
    marker.write_text("{}", encoding="utf-8")

    def fake_hash(files: list[Path], repo_path: Path | None = None) -> str:
        return "468fc3f6f8be0000"

    def fake_run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int = 300, env: dict[str, str] | None = None):
        if cmd == [str(python_path), "-c", "import astropy"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="build_ext --inplace")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(environment_module, "hash_dependency_files", fake_hash)
    monkeypatch.setattr(environment_module, "run_cmd", fake_run_cmd)

    profile = EnvironmentProfileManager(
        repo="astropy/astropy",
        repo_path=repo,
        env_root=env_root,
        allow_install=False,
    ).prepare_profile()

    assert profile.ready is False
    assert profile.reused_existing is False
    assert "extension modules" in profile.reason
