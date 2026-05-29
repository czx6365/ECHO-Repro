from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from echo_repro.models import EnvironmentProfileResult, EnvironmentRepairResult, ExecutionResult

MODULE_PACKAGE_OVERRIDES = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "erfa": "pyerfa",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
}

DEPENDENCY_FILE_NAMES = {
    "environment.yaml",
    "environment.yml",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements-tests.txt",
    "requirements.txt",
    "requirements_dev.txt",
    "setup.cfg",
    "setup.py",
    "tox.ini",
}


def parse_missing_module(stderr: str) -> str:
    patterns = [
        r"No module named ['\"](?P<module>[^'\"]+)['\"]",
        r"ModuleNotFoundError:\s+No module named ['\"](?P<module>[^'\"]+)['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, stderr)
        if match:
            return match.group("module").split(".")[0]
    return ""


def package_for_module(module: str) -> str:
    return MODULE_PACKAGE_OVERRIDES.get(module, module.replace("_", "-"))


def slugify_repo(repo: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "__", repo.strip())
    return slug.strip("_") or "unknown_repo"


def venv_python(env_path: Path) -> Path:
    if sys.platform == "win32":
        return env_path / "Scripts" / "python.exe"
    return env_path / "bin" / "python"


def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


def dependency_files_for_repo(repo_path: Path) -> list[Path]:
    repo_path = Path(repo_path)
    files = [repo_path / name for name in DEPENDENCY_FILE_NAMES if (repo_path / name).is_file()]
    requirements_dir = repo_path / "requirements"
    if requirements_dir.is_dir():
        files.extend(path for path in requirements_dir.glob("*.txt") if path.is_file())
    return sorted(set(files), key=lambda path: path.relative_to(repo_path).as_posix())


def hash_dependency_files(files: list[Path], repo_path: Path | None = None) -> str:
    digest = hashlib.sha256()
    if not files:
        digest.update(b"no-dependency-files")
        return digest.hexdigest()

    base = Path(repo_path) if repo_path else None
    for path in sorted(files, key=lambda item: str(item)):
        label = path.relative_to(base).as_posix() if base and path.is_relative_to(base) else path.name
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _python_version_from_token(token: str) -> str:
    token = token.strip()
    if not token or token == "dev":
        return ""
    if "." in token and re.fullmatch(r"3\.\d+", token):
        return token
    if re.fullmatch(r"3\d", token):
        return f"3.{token[1]}"
    if re.fullmatch(r"3\d\d", token):
        return f"3.{token[1:]}"
    return ""


def _version_tuple(version: str) -> tuple[int, int]:
    major, minor = version.split(".", 1)
    return int(major), int(minor)


def _versions_from_tox_envlist(tox_ini: Path) -> list[str]:
    text = tox_ini.read_text(encoding="utf-8", errors="ignore")
    versions: set[str] = set()

    for group in re.findall(r"py\{([^}]+)\}", text):
        for token in group.split(","):
            version = _python_version_from_token(token)
            if version:
                versions.add(version)

    for token in re.findall(r"\bpy(3\d\d?|3\.\d+)\b", text):
        version = _python_version_from_token(token)
        if version:
            versions.add(version)

    return sorted(versions, key=_version_tuple)


def _python_requires_from_pyproject(pyproject: Path) -> str:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    return str(data.get("project", {}).get("requires-python", ""))


def _python_requires_from_setup_cfg(setup_cfg: Path) -> str:
    text = setup_cfg.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"(?im)^\s*python_requires\s*=\s*(?P<spec>.+?)\s*$", text)
    return match.group("spec").strip() if match else ""


def _version_from_python_requires(specifier: str, current_version: str) -> str:
    versions = [_version_tuple(match) for match in re.findall(r"3\.\d+", specifier)]
    if not versions:
        return current_version

    lower_bounds = []
    upper_bounds = []
    for operator, version in re.findall(r"(>=|>|<=|<|==|~=)\s*(3\.\d+)", specifier):
        parsed = _version_tuple(version)
        if operator in {">=", ">", "==", "~="}:
            lower_bounds.append(parsed)
        if operator in {"<=", "<", "=="}:
            upper_bounds.append(parsed)

    current = _version_tuple(current_version)
    if upper_bounds and current >= min(upper_bounds):
        return ".".join(str(part) for part in max(lower_bounds or versions))
    if lower_bounds and current < max(lower_bounds):
        return ".".join(str(part) for part in max(lower_bounds))
    return current_version


def _uses_stdlib_distutils(repo_path: Path) -> bool:
    return _repo_python_text_matches(repo_path, r"(?m)^\s*(from\s+distutils\b|import\s+distutils\b)")


def _uses_removed_collections_abc_imports(repo_path: Path) -> bool:
    return _repo_python_text_matches(
        repo_path,
        r"(?m)^\s*from\s+collections\s+import\s+.*\b(Mapping|MutableMapping|Sequence|Iterable|Callable)\b",
    )


def _repo_python_text_matches(repo_path: Path, pattern: str) -> bool:
    compiled = re.compile(pattern)
    for path in Path(repo_path).rglob("*.py"):
        if any(part in {".git", ".tox", ".venv", "build", "dist", "__pycache__"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if compiled.search(text):
            return True
    return False


def detect_python_version(repo_path: Path, current_version: str | None = None) -> str:
    repo_path = Path(repo_path)
    current_version = current_version or f"{sys.version_info.major}.{sys.version_info.minor}"

    tox_versions = _versions_from_tox_envlist(repo_path / "tox.ini") if (repo_path / "tox.ini").is_file() else []
    if tox_versions:
        current = _version_tuple(current_version)
        compatible = [version for version in tox_versions if _version_tuple(version) <= current]
        return compatible[-1] if compatible else tox_versions[-1]

    pyproject_requires = (
        _python_requires_from_pyproject(repo_path / "pyproject.toml")
        if (repo_path / "pyproject.toml").is_file()
        else ""
    )
    setup_requires = (
        _python_requires_from_setup_cfg(repo_path / "setup.cfg")
        if (repo_path / "setup.cfg").is_file()
        else ""
    )
    detected = _version_from_python_requires(pyproject_requires or setup_requires, current_version)
    if _version_tuple(detected) > (3, 9) and _uses_removed_collections_abc_imports(repo_path):
        return "3.9"
    if _version_tuple(detected) > (3, 11) and _uses_stdlib_distutils(repo_path):
        return "3.11"
    return detected


def find_python_executable(version: str, base_python: Path) -> Path | None:
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    base_candidate = Path(base_python)
    if base_candidate.exists():
        completed = run_cmd([str(base_candidate), "--version"], timeout=10)
        if completed.returncode == 0 and version in (completed.stdout + completed.stderr):
            return base_candidate

    if version == current:
        return Path(sys.executable)

    candidate_names = [f"python{version}", f"python{version.replace('.', '')}"]
    for candidate_name in candidate_names:
        candidate = shutil.which(candidate_name)
        if not candidate:
            continue
        completed = run_cmd([candidate, "--version"], timeout=10)
        if completed.returncode == 0 and version in (completed.stdout + completed.stderr):
            return Path(candidate)
    return None


def _build_system_requires(repo_path: Path) -> list[str]:
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.is_file():
        return ["setuptools", "wheel"]
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ["setuptools", "wheel"]
    requires = data.get("build-system", {}).get("requires", [])
    return [str(requirement) for requirement in requires] or ["setuptools", "wheel"]


def _legacy_build_requires(repo_path: Path) -> list[str]:
    requirements = []
    seen: set[str] = set()
    for requirement in _build_system_requires(repo_path):
        normalized = requirement.lower().replace("_", "-")
        if normalized == "setuptools":
            replacement = "setuptools<60"
        elif normalized.startswith("setuptools-scm") or normalized.startswith("setuptools_scm"):
            replacement = "setuptools_scm>=8"
        else:
            replacement = requirement
        if replacement not in seen:
            seen.add(replacement)
            requirements.append(replacement)
    return requirements


def _legacy_build_env() -> dict[str, str]:
    env = os.environ.copy()
    existing_cflags = env.get("CFLAGS", "")
    compatibility_flag = "-Wno-error=incompatible-function-pointer-types"
    if compatibility_flag not in existing_cflags:
        env["CFLAGS"] = f"{existing_cflags} {compatibility_flag}".strip()
    return env


def _astropy_build_ext_env() -> dict[str, str]:
    env = _legacy_build_env()
    env["ASTROPY_USE_SYSTEM_CFITSIO"] = "1"
    pkg_config_path = env.get("PKG_CONFIG_PATH", "")
    homebrew_pkg_config = "/opt/homebrew/lib/pkgconfig"
    if homebrew_pkg_config not in pkg_config_path.split(os.pathsep):
        env["PKG_CONFIG_PATH"] = (
            f"{pkg_config_path}{os.pathsep}{homebrew_pkg_config}"
            if pkg_config_path
            else homebrew_pkg_config
        )
    return env


def _needs_legacy_build_fallback(result: subprocess.CompletedProcess) -> bool:
    output = f"{result.stdout}\n{result.stderr}"
    return (
        "setuptools.dep_util" in output
        or "incompatible-function-pointer-types" in output
    )


def _needs_non_editable_fallback(result: subprocess.CompletedProcess) -> bool:
    output = f"{result.stdout}\n{result.stderr}"
    return "missing the 'build_editable' hook" in output


def _needs_astropy_build_ext_fallback(result: subprocess.CompletedProcess) -> bool:
    output = f"{result.stdout}\n{result.stderr}"
    return (
        "Failed building wheel for astropy" in output
        or "Failed to build installable wheels" in output and "astropy" in output
    )


def _legacy_runtime_constraints(repo_slug: str, repo_path: Path) -> list[str]:
    constraints = []
    if repo_slug == "pydata__xarray" and _repo_python_text_matches(repo_path, r"\bnp\.unicode_\b"):
        constraints.extend(["numpy<2", "pandas<2"])
    return constraints


def _astropy_import_check(python_path: Path, repo_path: Path) -> subprocess.CompletedProcess:
    return run_cmd(
        [str(python_path), "-c", "import astropy"],
        cwd=repo_path,
        timeout=60,
    )


def _astropy_import_needs_build_ext(result: subprocess.CompletedProcess) -> bool:
    output = f"{result.stdout}\n{result.stderr}"
    return (
        "build_ext --inplace" in output
        or "extension modules are built" in output
        or "cannot import name '_compiler'" in output
    )


def _stale_astropy_profile(python_path: Path, build_repo_paths: list[Path]) -> subprocess.CompletedProcess | None:
    for build_repo_path in build_repo_paths:
        checked = _astropy_import_check(python_path, build_repo_path)
        if checked.returncode != 0 and _astropy_import_needs_build_ext(checked):
            return checked
    return None


def _write_profile_marker(
    marker_path: Path,
    *,
    repo: str,
    profile_key: str,
    dependency_hash: str,
    build_repo_paths: list[Path],
) -> None:
    marker_path.write_text(
        json.dumps(
            {
                "repo": repo,
                "profile_key": profile_key,
                "dependency_hash": dependency_hash,
                "build_repo_paths": [str(path) for path in build_repo_paths],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


@dataclass
class EnvironmentProfileManager:
    repo: str
    repo_path: Path
    extra_repo_paths: list[Path] = field(default_factory=list)
    env_root: Path = Path("envs")
    base_python: Path = field(default_factory=lambda: Path(sys.executable))
    allow_install: bool = False
    install_timeout: int = 900

    def prepare_profile(self) -> EnvironmentProfileResult:
        repo_slug = slugify_repo(self.repo)
        repo_path = Path(self.repo_path).resolve()
        env_root = Path(self.env_root).resolve()
        dependency_files = dependency_files_for_repo(repo_path)
        dependency_hash = hash_dependency_files(dependency_files, repo_path=repo_path)
        detected_python = detect_python_version(repo_path)
        current_python = f"{sys.version_info.major}.{sys.version_info.minor}"
        profile_key = f"{repo_slug}-py{detected_python.replace('.', '')}-{dependency_hash[:12]}"
        env_path = env_root / profile_key
        python_path = venv_python(env_path)
        profile_marker = env_path / ".echo-repro-profile-ready.json"
        build_repo_paths = [repo_path, *[Path(path).resolve() for path in self.extra_repo_paths]]

        base_result = EnvironmentProfileResult(
            repo=self.repo,
            repo_slug=repo_slug,
            profile_key=profile_key,
            env_root=env_root,
            env_path=env_path,
            python_path=python_path,
            profile_marker=profile_marker,
            detected_python=detected_python,
            current_python=current_python,
            dependency_hash=dependency_hash,
            dependency_files=dependency_files,
            build_repo_paths=build_repo_paths,
            heavy_install_allowed=self.allow_install,
        )

        if python_path.exists() and profile_marker.exists():
            runtime_constraints = _legacy_runtime_constraints(repo_slug, repo_path)
            if runtime_constraints and self.allow_install:
                install_cmd = [str(python_path), "-m", "pip", "install", *runtime_constraints]
                constrained = run_cmd(install_cmd, timeout=self.install_timeout)
                if constrained.returncode != 0:
                    base_result.attempted = True
                    base_result.install_command = install_cmd
                    base_result.returncode = constrained.returncode
                    base_result.stdout = constrained.stdout
                    base_result.stderr = constrained.stderr
                    base_result.reason = "Failed to install legacy runtime constraints."
                    return base_result

            stale_astropy = (
                _stale_astropy_profile(python_path, build_repo_paths)
                if repo_slug == "astropy__astropy"
                else None
            )
            if stale_astropy:
                base_result.stdout = stale_astropy.stdout
                base_result.stderr = stale_astropy.stderr
                if not self.allow_install:
                    base_result.reason = "Cached Astropy env exists, but extension modules are not built."
                    return base_result
                profile_marker.unlink(missing_ok=True)
            else:
                base_result.ready = True
                base_result.reused_existing = True
                base_result.reason = "Reusing cached environment profile."
                return base_result

        if repo_slug == "astropy__astropy" and python_path.exists() and not profile_marker.exists():
            stale_astropy = _stale_astropy_profile(python_path, build_repo_paths)
            if stale_astropy is None:
                _write_profile_marker(
                    profile_marker,
                    repo=self.repo,
                    profile_key=profile_key,
                    dependency_hash=dependency_hash,
                    build_repo_paths=build_repo_paths,
                )
                base_result.ready = True
                base_result.reused_existing = True
                base_result.reason = "Reusing validated Astropy environment profile."
                return base_result
            base_result.stdout = stale_astropy.stdout
            base_result.stderr = stale_astropy.stderr

        if python_path.exists() and profile_marker.exists():
            base_result.ready = True
            base_result.reused_existing = True
            base_result.reason = "Reusing cached environment profile."
            return base_result

        if not self.allow_install:
            if python_path.exists() and not profile_marker.exists():
                base_result.reason = "Cached env exists, but profile ready marker is missing."
                return base_result
            if detected_python != current_python:
                base_result.reason = (
                    f"Environment profile expects Python {detected_python}, but current interpreter is "
                    f"Python {current_python}; cached env is missing and installation is disabled."
                )
            else:
                base_result.reason = "Environment profile detected; cached env is missing and installation is disabled."
            return base_result

        interpreter = find_python_executable(detected_python, self.base_python)
        if not interpreter:
            base_result.reason = f"Python {detected_python} interpreter was not found; cannot create env profile."
            return base_result

        if not python_path.exists():
            env_path.parent.mkdir(parents=True, exist_ok=True)
            create_cmd = [str(interpreter), "-m", "venv", str(env_path)]
            created = run_cmd(create_cmd, timeout=self.install_timeout)
            if created.returncode != 0:
                base_result.attempted = True
                base_result.install_command = create_cmd
                base_result.returncode = created.returncode
                base_result.stdout = created.stdout
                base_result.stderr = created.stderr
                base_result.reason = "Failed to create environment profile."
                return base_result

        installed = None
        for build_repo_path in build_repo_paths:
            editable_supported = True
            install_cmd = [str(python_path), "-m", "pip", "install", "-e", "."]
            installed = run_cmd(install_cmd, cwd=build_repo_path, timeout=self.install_timeout)
            if installed.returncode != 0 and _needs_non_editable_fallback(installed):
                editable_supported = False
                install_cmd = [str(python_path), "-m", "pip", "install", "."]
                installed = run_cmd(install_cmd, cwd=build_repo_path, timeout=self.install_timeout)
            if installed.returncode == 0:
                if repo_slug == "astropy__astropy":
                    install_cmd = [str(python_path), "setup.py", "build_ext", "--inplace"]
                    installed = run_cmd(
                        install_cmd,
                        cwd=build_repo_path,
                        timeout=self.install_timeout,
                        env=_astropy_build_ext_env(),
                    )
                    if installed.returncode != 0:
                        break
                continue
            if not _needs_legacy_build_fallback(installed):
                break

            deps_cmd = [str(python_path), "-m", "pip", "install", *_legacy_build_requires(build_repo_path)]
            deps_installed = run_cmd(deps_cmd, timeout=self.install_timeout)
            if deps_installed.returncode != 0:
                installed = deps_installed
                install_cmd = deps_cmd
                break

            install_cmd = [str(python_path), "-m", "pip", "install"]
            if editable_supported:
                install_cmd.append("-e")
            install_cmd.extend([".", "--no-build-isolation"])
            installed = run_cmd(
                install_cmd,
                cwd=build_repo_path,
                timeout=self.install_timeout,
                env=_legacy_build_env(),
            )
            if installed.returncode != 0 and _needs_non_editable_fallback(installed):
                install_cmd = [
                    str(python_path),
                    "-m",
                    "pip",
                    "install",
                    ".",
                    "--no-build-isolation",
                ]
                installed = run_cmd(
                    install_cmd,
                    cwd=build_repo_path,
                    timeout=self.install_timeout,
                    env=_legacy_build_env(),
                )
            if installed.returncode != 0:
                if repo_slug == "astropy__astropy" and _needs_astropy_build_ext_fallback(installed):
                    runtime_deps_cmd = [str(python_path), "-m", "pip", "install", "pyerfa"]
                    runtime_deps = run_cmd(runtime_deps_cmd, timeout=self.install_timeout)
                    if runtime_deps.returncode != 0:
                        installed = runtime_deps
                        install_cmd = runtime_deps_cmd
                        break
                    install_cmd = [str(python_path), "setup.py", "build_ext", "--inplace"]
                    installed = run_cmd(
                        install_cmd,
                        cwd=build_repo_path,
                        timeout=self.install_timeout,
                        env=_astropy_build_ext_env(),
                    )
                    if installed.returncode == 0:
                        continue
                    installed.stdout = f"{runtime_deps.stdout}\n{installed.stdout}"
                    installed.stderr = f"{runtime_deps.stderr}\n{installed.stderr}"
                installed.stdout = f"{deps_installed.stdout}\n{installed.stdout}"
                installed.stderr = f"{deps_installed.stderr}\n{installed.stderr}"
                break

        assert installed is not None
        runtime_constraints = _legacy_runtime_constraints(repo_slug, repo_path)
        if installed.returncode == 0 and runtime_constraints:
            install_cmd = [str(python_path), "-m", "pip", "install", *runtime_constraints]
            installed = run_cmd(install_cmd, timeout=self.install_timeout)

        if installed.returncode == 0 and repo_slug == "astropy__astropy":
            stale_astropy = _stale_astropy_profile(python_path, build_repo_paths)
            if stale_astropy:
                install_cmd = [str(python_path), "-c", "import astropy"]
                installed = stale_astropy

        base_result.attempted = True
        base_result.ready = installed.returncode == 0
        base_result.install_command = install_cmd
        base_result.returncode = installed.returncode
        base_result.stdout = installed.stdout
        base_result.stderr = installed.stderr
        if base_result.ready:
            _write_profile_marker(
                profile_marker,
                repo=self.repo,
                profile_key=profile_key,
                dependency_hash=dependency_hash,
                build_repo_paths=build_repo_paths,
            )
        base_result.reason = (
            "Environment profile installed."
            if installed.returncode == 0
            else "Environment profile installation failed."
        )
        return base_result


@dataclass
class EnvironmentRepairManager:
    repo_slug: str
    env_root: Path = Path("envs")
    base_python: Path = field(default_factory=lambda: Path(sys.executable))
    env_path_override: Path | None = None

    @property
    def env_path(self) -> Path:
        if self.env_path_override:
            return Path(self.env_path_override).resolve()
        return (Path(self.env_root) / slugify_repo(self.repo_slug)).resolve()

    @property
    def python_path(self) -> Path:
        return venv_python(self.env_path)

    def repair_dependency(self, result: ExecutionResult) -> EnvironmentRepairResult:
        missing_module = parse_missing_module(result.stderr)
        if not missing_module:
            return EnvironmentRepairResult(
                attempted=False,
                success=False,
                reason="Could not parse missing module from stderr.",
            )

        package = package_for_module(missing_module)
        env_path = self.env_path
        python_path = self.python_path

        if not python_path.exists():
            env_path.parent.mkdir(parents=True, exist_ok=True)
            create_cmd = [str(self.base_python), "-m", "venv", str(env_path)]
            create = run_cmd(create_cmd)
            if create.returncode != 0:
                return EnvironmentRepairResult(
                    attempted=True,
                    success=False,
                    missing_module=missing_module,
                    package=package,
                    env_path=env_path,
                    python_path=python_path,
                    install_command=create_cmd,
                    returncode=create.returncode,
                    stdout=create.stdout,
                    stderr=create.stderr,
                    reason="Failed to create cached virtual environment.",
                )

        install_cmd = [str(python_path), "-m", "pip", "install", package]
        installed = run_cmd(install_cmd)
        return EnvironmentRepairResult(
            attempted=True,
            success=installed.returncode == 0,
            missing_module=missing_module,
            package=package,
            env_path=env_path,
            python_path=python_path,
            install_command=install_cmd,
            returncode=installed.returncode,
            stdout=installed.stdout,
            stderr=installed.stderr,
            reason="Dependency installed." if installed.returncode == 0 else "Dependency installation failed.",
        )
