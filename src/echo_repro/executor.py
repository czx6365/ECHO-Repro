from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from echo_repro.models import ExecutionResult, HarnessCandidate

# WARNING:
# This MVP writes and executes generated code only inside the target repository.
# That is not strong isolation. Production evaluation should use Docker or a
# comparable sandbox boundary before running untrusted generated code.


def write_harness(repo_path: Path, harness_candidate: HarnessCandidate, filename: str = "reproduce.py") -> Path:
    repo_path = Path(repo_path).resolve()
    harness_path = (repo_path / filename).resolve()
    if repo_path not in harness_path.parents and harness_path != repo_path / filename:
        raise ValueError("Harness path must stay inside the target repository.")
    harness_path.write_text(harness_candidate.code, encoding="utf-8")
    return harness_path


def run_harness(repo_path: Path, command: str | None = None, timeout: int = 30) -> ExecutionResult:
    repo_path = Path(repo_path).resolve()
    command = command or f"{sys.executable} reproduce.py"
    try:
        completed = subprocess.run(
            shlex.split(command),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return ExecutionResult(
            repo_path=repo_path,
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            harness_path=repo_path / "reproduce.py",
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(
            repo_path=repo_path,
            command=command,
            returncode=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            harness_path=repo_path / "reproduce.py",
            timed_out=True,
        )
