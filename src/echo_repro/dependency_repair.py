"""LLM-driven dependency repair.

Reads the failing execution's stderr, mines the repository's dependency
declarations, asks the LLM which package (and version) to install, validates
the proposed commands, and executes them inside the cached virtualenv.

Validation is deliberately strict: every accepted command is normalized to
``<venv python> -m pip install ...`` so arbitrary LLM output can never run a
shell or escape the venv.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from echo_repro.environment import parse_missing_module
from echo_repro.models import EnvironmentRepairResult, ExecutionResult

if TYPE_CHECKING:
    from echo_repro.environment import EnvironmentRepairManager
    from echo_repro.llm.base import BaseLLMClient

STDERR_TAIL_CHARS = 3000
MAX_INSTALL_COMMANDS = 3
INSTALL_TIMEOUT = 900

# Flags we are willing to pass through unchanged.
ALLOWED_FLAGS = {
    "--upgrade",
    "-U",
    "--force-reinstall",
    "--no-cache-dir",
    "-q",
    "--quiet",
    "--no-deps",
    "--no-input",
    "--no-warn-script-location",
}
# Flags that take a URL value; https only.
INDEX_FLAGS = {"--index-url", "-i"}
# Flags that would escape the venv or mutate the host; the whole command is rejected.
REJECTED_FLAGS = {"--user", "--target", "--root", "--prefix", "--system", "--break-system-packages"}
# Non-flag argument charset: package names, version specs, extras, markers.
# Paths, URLs, and git+ references are rejected.
ARG_PATTERN = re.compile(r"^[A-Za-z0-9._\-\[\]<>=!~,;*]+$")


def validate_repair_spec(
    spec: Any,
    python_path: Path,
    repo_path: Path | None,
) -> list[tuple[list[str], Path | None]] | None:
    """Validate and normalize an LLM repair spec.

    Returns a list of ``(command, cwd)`` pairs, at most MAX_INSTALL_COMMANDS,
    or None when no command survived validation.
    """
    if not isinstance(spec, dict):
        return None
    raw_commands = spec.get("install_commands")
    if not isinstance(raw_commands, list) or not raw_commands:
        return None
    validated: list[tuple[list[str], Path | None]] = []
    for raw in raw_commands:
        if not isinstance(raw, str):
            continue
        try:
            tokens = shlex.split(raw)
        except ValueError:
            continue
        parsed = _parse_install_tokens(tokens, python_path, repo_path)
        if parsed is not None:
            validated.append(parsed)
        if len(validated) >= MAX_INSTALL_COMMANDS:
            break
    return validated or None


def _parse_install_tokens(
    tokens: list[str],
    python_path: Path,
    repo_path: Path | None,
) -> tuple[list[str], Path | None] | None:
    if not tokens:
        return None
    if tokens[:2] == ["pip", "install"]:
        args = tokens[2:]
    elif tokens[:4] == ["python", "-m", "pip", "install"]:
        args = tokens[4:]
    else:
        return None
    if not args:
        return None
    # Install the repository itself, exactly in dot form, run from repo_path.
    if args == ["."] or args == ["-e", "."]:
        if repo_path is None:
            return None
        return ([str(python_path), "-m", "pip", "install", *args], repo_path)

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in REJECTED_FLAGS:
            return None
        if arg in INDEX_FLAGS:
            value = args[i + 1] if i + 1 < len(args) else ""
            if not value.startswith("https://"):
                return None
            i += 2
            continue
        if arg in ALLOWED_FLAGS:
            i += 1
            continue
        if arg.startswith("-"):
            # Unknown flags (including -r and -e with non-dot targets) are rejected.
            return None
        if not ARG_PATTERN.fullmatch(arg):
            return None
        i += 1
    return ([str(python_path), "-m", "pip", "install", *args], None)


def _probe_python_version(python_path: Path) -> str:
    from echo_repro.environment import run_cmd

    try:
        proc = run_cmd([str(python_path), "--version"], timeout=60)
        out = (proc.stdout or proc.stderr or "").strip()
        if out:
            return out
    except Exception:
        pass
    return (
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} "
        f"(host interpreter {sys.executable})"
    )


def _pip_freeze(python_path: Path, max_lines: int = 60) -> str:
    """Snapshot of the venv's installed packages so the LLM can see versions."""
    from echo_repro.environment import run_cmd

    try:
        proc = run_cmd([str(python_path), "-m", "pip", "freeze"], timeout=120)
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        if lines:
            shown = lines[:max_lines]
            suffix = f"...({len(lines) - len(shown)} more lines)" if len(lines) > len(shown) else ""
            return "\n".join(shown) + (f"\n{suffix}" if suffix else "")
        if (proc.stderr or "").strip():
            return f"(pip freeze failed: {(proc.stderr or '').strip()[:200]})"
    except Exception:
        pass
    return "(could not read installed packages)"


def _declaration_summary(repo_path: Path, output_text: str) -> str:
    try:
        from echo_ci.dependency_diagnosis import diagnose_missing_dependency

        diagnosis = diagnose_missing_dependency(repo_path=repo_path, output_text=output_text)
    except Exception:
        return "(could not mine repository dependency declarations)"
    if not diagnosis:
        return "(no dependency declarations mention the missing module)"
    lines: list[str] = []
    for declaration in (diagnosis.get("declared_in") or [])[:15]:
        lines.append(
            f'{declaration.get("path", "?")}:{declaration.get("line", "?")}: {declaration.get("entry", "")}'
        )
    if diagnosis.get("recommended_minimal_install"):
        lines.append(f"recommended_minimal_install: {diagnosis['recommended_minimal_install']}")
    sources = ", ".join(diagnosis.get("attributed_sources") or [])
    if sources:
        lines.append(f"attributed_sources: {sources}")
    return "\n".join(lines)


def build_repair_context(
    manager: EnvironmentRepairManager,
    result: ExecutionResult,
    repo_path: Path,
    missing_module: str,
) -> dict[str, str]:
    stderr_tail = (result.stderr or result.stdout or "").strip()
    if len(stderr_tail) > STDERR_TAIL_CHARS:
        stderr_tail = f"...(truncated to last {STDERR_TAIL_CHARS} chars)\n{stderr_tail[-STDERR_TAIL_CHARS:]}"
    return {
        "repo_slug": manager.repo_slug,
        "missing_module": missing_module,
        "stderr_tail": stderr_tail or "(no stderr captured)",
        "python_version": _probe_python_version(manager.python_path),
        "declaration_summary": _declaration_summary(repo_path, stderr_tail),
        "installed_packages": _pip_freeze(manager.python_path),
    }


def _package_names(spec: Any) -> str:
    if not isinstance(spec, dict):
        return ""
    packages = spec.get("packages")
    names = [
        str(package.get("name"))
        for package in packages
        if isinstance(package, dict) and package.get("name")
    ]
    return ", ".join(names)


def install_with_llm(
    manager: EnvironmentRepairManager,
    result: ExecutionResult,
    repo_path: Path,
    llm_client: BaseLLMClient,
) -> EnvironmentRepairResult | None:
    """Run the LLM-driven repair flow. Returns None to signal mechanical fallback."""
    from echo_repro.environment import run_cmd

    missing_module = parse_missing_module(result.stderr)
    if not missing_module:
        return None
    try:
        context = build_repair_context(manager, result, repo_path, missing_module)
    except Exception:
        return None

    failure_feedback = ""
    for round_index in (1, 2):
        if round_index == 2:
            # Refresh the freeze so the LLM sees what round 1 actually installed.
            context["installed_packages"] = _pip_freeze(manager.python_path)
        try:
            spec = llm_client.repair_dependency(**context, feedback=failure_feedback)
        except Exception:
            return None
        llm_prompt = getattr(llm_client, "last_prompt", "")
        commands = validate_repair_spec(spec, manager.python_path, repo_path)
        if not commands:
            return None

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        all_ok = True
        first_failure_code: int | None = None
        failure_output = ""
        for cmd, cwd in commands:
            try:
                proc = run_cmd(cmd, cwd=cwd, timeout=INSTALL_TIMEOUT, env=getattr(manager, "_install_env", None))
            except subprocess.TimeoutExpired:
                proc = subprocess.CompletedProcess(
                    cmd,
                    -1,
                    stdout="",
                    stderr=f"Command timed out after {INSTALL_TIMEOUT}s.",
                )
            stdout_parts.append(proc.stdout or "")
            stderr_parts.append(proc.stderr or "")
            if proc.returncode != 0:
                all_ok = False
                if first_failure_code is None:
                    first_failure_code = proc.returncode
                if not failure_output:
                    failure_output = "\n".join(
                        part for part in (proc.stdout or "", proc.stderr or "") if part
                    )

        common = dict(
            attempted=True,
            missing_module=missing_module,
            package=_package_names(spec),
            env_path=manager.env_path,
            python_path=manager.python_path,
            install_command=commands[-1][0],
            returncode=0 if all_ok else (first_failure_code if first_failure_code is not None else -1),
            stdout="\n".join(stdout_parts),
            stderr="\n".join(stderr_parts),
            strategy="llm",
            llm_reason=str(spec.get("reason") or "")[:1000] if isinstance(spec, dict) else "",
            llm_prompt=llm_prompt,
            detected_python=getattr(manager, "_detected_python", ""),
        )
        if all_ok:
            return EnvironmentRepairResult(
                **common,
                success=True,
                reason="LLM-driven dependency installation succeeded.",
            )
        if round_index == 2:
            return EnvironmentRepairResult(
                **common,
                success=False,
                reason="LLM-driven dependency installation failed after 2 rounds.",
            )
        failure_feedback = (failure_output or "\n".join(stderr_parts))[-2000:]
    return None
