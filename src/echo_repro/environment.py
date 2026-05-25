from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from echo_repro.models import EnvironmentRepairResult, ExecutionResult

MODULE_PACKAGE_OVERRIDES = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "erfa": "pyerfa",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
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


def run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


@dataclass
class EnvironmentRepairManager:
    repo_slug: str
    env_root: Path = Path("envs")
    base_python: Path = field(default_factory=lambda: Path(sys.executable))

    @property
    def env_path(self) -> Path:
        return Path(self.env_root) / slugify_repo(self.repo_slug)

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
