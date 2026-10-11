from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ENV_POLICIES = {"E0_no_setup", "E1_pythonpath_only", "E2_tool_bootstrap"}
CI_TOOLS = {
    "pytest",
    "ruff",
    "flake8",
    "mypy",
    "black",
    "isort",
    "pylint",
    "pre-commit",
    "tox",
    "poetry",
}
TOOL_PACKAGES = {tool: tool for tool in CI_TOOLS}


@dataclass
class EnvSetupResult:
    policy: str
    venv_path: str
    tools_detected: list[dict[str, str]] = field(default_factory=list)
    tools_missing: list[str] = field(default_factory=list)
    tools_installed: list[str] = field(default_factory=list)
    install_commands: list[list[str]] = field(default_factory=list)
    install_failed: list[str] = field(default_factory=list)
    dynamic_retry: bool = False
    dynamic_missing_tools: list[str] = field(default_factory=list)
    dynamic_installed_tools: list[str] = field(default_factory=list)
    dynamic_retry_exit_code: int | None = None
    dynamic_retry_used: bool = False
    setup_success: bool = True
    setup_stdout_tail: str = ""
    setup_stderr_tail: str = ""
    setup_duration: float = 0.0
    missing_tool: str = ""
    setup_error: str = ""
    execution_path: str = ""
    python_executable: str = ""
    resolved_tools: dict[str, str] = field(default_factory=dict)
    unresolved_tools: list[str] = field(default_factory=list)
    external_tools_ignored: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_env_policy(policy: str) -> str:
    if policy not in ENV_POLICIES:
        raise ValueError(f"Unknown env policy {policy!r}. Expected one of {', '.join(sorted(ENV_POLICIES))}.")
    return policy


def _add_match(matches: list[dict[str, str]], seen: set[tuple[str, str, str]], *, tool: str, source: str, pattern: str) -> None:
    key = (tool, source, pattern)
    if key in seen:
        return
    seen.add(key)
    matches.append({"tool": tool, "source": source, "pattern": pattern})


def detect_ci_tool_matches(sources: dict[str, str] | str, *, default_source: str = "run.sh") -> list[dict[str, str]]:
    if isinstance(sources, str):
        sources = {default_source: sources}
    matches: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for tool in CI_TOOLS:
        escaped = re.escape(tool)
        module_name = re.escape(tool.replace("-", "_"))
        for source, text in sources.items():
            lowered = str(text or "").lower()
            if not lowered:
                continue
            checks = [
                ("shell_command", rf"(^|[;&|()\s]){escaped}(\s|$)"),
                ("python_module", rf"\bpython(?:3)?\s+-m\s+{escaped}\b"),
                ("poetry_run", rf"\bpoetry\s+run\s+{escaped}\b"),
                ("command_not_found", rf"\b{escaped}: command not found\b"),
                ("module_not_found", rf"\bno module named ['\"]?{module_name}['\"]?"),
                ("subprocess.run", rf"subprocess\.(?:run|call|check_call|check_output|popen)\s*\([^)]*['\"]{escaped}"),
                ("subprocess.run", rf"subprocess\.(?:run|call|check_call|check_output|popen)\s*\([^)]*['\"][^'\"]*\b{escaped}\b"),
                ("os.system", rf"os\.system\s*\([^)]*['\"][^'\"]*\b{escaped}\b"),
                ("shutil.which", rf"shutil\.which\s*\(\s*['\"]{escaped}['\"]"),
                ("quoted_command", rf"['\"]{escaped}['\"]"),
            ]
            for pattern_name, regex in checks:
                if re.search(regex, lowered, re.MULTILINE):
                    _add_match(matches, seen, tool=tool, source=source, pattern=pattern_name)
    return sorted(matches, key=lambda item: (item["tool"], item["source"], item["pattern"]))


def detect_ci_tools(text: str | dict[str, str]) -> list[str]:
    return sorted({item["tool"] for item in detect_ci_tool_matches(text)})


def detected_tool_names(tools_detected: list[Any]) -> list[str]:
    names: set[str] = set()
    for item in tools_detected or []:
        if isinstance(item, dict):
            tool = str(item.get("tool") or "")
        else:
            tool = str(item or "")
        if tool:
            names.add(tool)
    return sorted(names)


def extract_grounding_text(metadata_text: str, oracle_text: str = "") -> str:
    chunks: list[str] = []
    for text in (metadata_text, oracle_text):
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        grounding = data.get("grounding") if isinstance(data, dict) else None
        if not isinstance(grounding, dict):
            continue
        for key in ("selected_command", "expected_failing_signal"):
            value = grounding.get(key)
            if value:
                chunks.append(str(value))
    return "\n".join(chunks)


def parse_missing_ci_tools(text: str) -> list[str]:
    lowered = str(text or "").lower()
    found: set[str] = set()
    patterns = [
        r"([a-z0-9_.+-]+): command not found",
        r"command not found: ([a-z0-9_.+-]+)",
        r"filenotfounderror: .*no such file or directory: '([^']+)'",
        r"no such file or directory: '([^']+)'",
        r"make: ([a-z0-9_.+-]+): no such file or directory",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, lowered):
            raw = match.group(1).strip()
            if "/" in raw or "." in raw:
                continue
            command = raw.split("/")[-1]
            if command in CI_TOOLS:
                found.add(command)
    for tool in CI_TOOLS:
        module = tool.replace("-", "_")
        if re.search(rf"no module named ['\"]?{re.escape(module)}['\"]?", lowered):
            found.add(tool)
    return sorted(found)


def run_sh_uses_disallowed_install(run_text: str) -> bool:
    lowered = str(run_text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "pip install -r",
            "python -m pip install -r",
            "python3 -m pip install -r",
            "pip install .",
            "python -m pip install .",
            "python3 -m pip install .",
            "poetry install",
            "apt-get",
            "docker build",
        )
    )


def _tail(text: str, limit: int = 4000) -> str:
    return str(text or "")[-limit:]


def _run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _python_executable(venv_path: Path) -> Path:
    return venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_bin(venv_path: Path) -> Path:
    return venv_path / ("Scripts" if os.name == "nt" else "bin")


def build_execution_env(repo_dir: Path, venv_path: Path, *, include_pythonpath: bool) -> dict[str, str]:
    env = dict(os.environ)
    repo_dir = Path(repo_dir).resolve()
    bin_dir = _venv_bin(venv_path)
    env["REPO_ROOT"] = str(repo_dir)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    if include_pythonpath:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(repo_dir) if not existing else f"{repo_dir}{os.pathsep}{existing}"
    return env


def record_execution_diagnostics(
    diagnostics: EnvSetupResult,
    *,
    venv_path: Path,
    execution_env: dict[str, str],
) -> None:
    """Record and enforce tool resolution inside the experiment venv.

    Looking up a tool on the full inherited PATH can silently reuse a globally
    installed executable and make results depend on run order. E2 therefore
    records a tool as resolved only when it is present in ``.echo_venv``.
    """

    venv_path = Path(venv_path).resolve()
    venv_bin = _venv_bin(venv_path)
    diagnostics.execution_path = execution_env.get("PATH", "")
    diagnostics.python_executable = str(_python_executable(venv_path))
    diagnostics.resolved_tools = {}
    diagnostics.unresolved_tools = []
    for tool in detected_tool_names(diagnostics.tools_detected):
        resolved = shutil.which(tool, path=str(venv_bin))
        if resolved:
            diagnostics.resolved_tools[tool] = str(Path(resolved).resolve())
        else:
            diagnostics.unresolved_tools.append(tool)


def setup_environment(
    *,
    repo_dir: Path,
    policy: str,
    run_text: str,
    reproduce_text: str = "",
    oracle_text: str = "",
    metadata_text: str = "",
    failed_command: str = "",
    timeout: int = 120,
) -> tuple[EnvSetupResult, dict[str, str]]:
    policy = validate_env_policy(policy)
    repo_dir = Path(repo_dir).resolve()
    venv_path = repo_dir / ".echo_venv"
    started = time.perf_counter()
    diagnostics = EnvSetupResult(policy=policy, venv_path=str(venv_path))
    base_env = dict(os.environ)
    include_pythonpath = policy in {"E1_pythonpath_only", "E2_tool_bootstrap"}

    try:
        if not venv_path.exists():
            created = _run(["python3", "-m", "venv", str(venv_path)], cwd=repo_dir, env=base_env, timeout=timeout)
            diagnostics.setup_stdout_tail += _tail(created.stdout)
            diagnostics.setup_stderr_tail += _tail(created.stderr)
            if created.returncode != 0:
                diagnostics.setup_success = False
                diagnostics.setup_error = created.stderr or created.stdout
                diagnostics.setup_duration = round(time.perf_counter() - started, 3)
                return diagnostics, build_execution_env(repo_dir, venv_path, include_pythonpath=include_pythonpath)

        exec_env = build_execution_env(repo_dir, venv_path, include_pythonpath=include_pythonpath)
        python = str(_python_executable(venv_path))
        upgraded = _run([python, "-m", "pip", "install", "-U", "pip", "wheel"], cwd=repo_dir, env=exec_env, timeout=timeout)
        diagnostics.setup_stdout_tail += _tail(upgraded.stdout)
        diagnostics.setup_stderr_tail += _tail(upgraded.stderr)
        if upgraded.returncode != 0:
            diagnostics.setup_success = False
            diagnostics.setup_error = upgraded.stderr or upgraded.stdout

        grounding_text = extract_grounding_text(metadata_text, oracle_text)
        diagnostics.tools_detected = detect_ci_tool_matches(
            {
                "run.sh": run_text or "",
                "reproduce.py": reproduce_text or "",
                "failed_command": failed_command or "",
                "oracle.json": oracle_text or "",
                "grounding": grounding_text,
            }
        )
        if policy == "E2_tool_bootstrap":
            for tool in detected_tool_names(diagnostics.tools_detected):
                isolated_tool = shutil.which(tool, path=str(_venv_bin(venv_path)))
                if isolated_tool:
                    continue
                external_tool = shutil.which(tool, path=base_env.get("PATH", ""))
                if external_tool:
                    diagnostics.external_tools_ignored[tool] = external_tool
                install_ci_tool(
                    diagnostics=diagnostics,
                    repo_dir=repo_dir,
                    venv_path=venv_path,
                    tool=tool,
                    env=exec_env,
                    timeout=timeout,
                    dynamic=False,
                )
                exec_env = build_execution_env(repo_dir, venv_path, include_pythonpath=include_pythonpath)
        record_execution_diagnostics(
            diagnostics,
            venv_path=venv_path,
            execution_env=exec_env,
        )
        if policy == "E2_tool_bootstrap" and diagnostics.unresolved_tools:
            diagnostics.setup_success = False
            diagnostics.missing_tool = diagnostics.missing_tool or diagnostics.unresolved_tools[0]
            unresolved = ", ".join(diagnostics.unresolved_tools)
            diagnostics.setup_error = diagnostics.setup_error or f"CI tool(s) not resolved inside .echo_venv: {unresolved}"
        diagnostics.setup_duration = round(time.perf_counter() - started, 3)
        return diagnostics, exec_env
    except subprocess.TimeoutExpired as exc:
        diagnostics.setup_success = False
        diagnostics.setup_error = f"Env setup timed out after {timeout}s"
        diagnostics.setup_stdout_tail += _tail(exc.stdout or "")
        diagnostics.setup_stderr_tail += _tail(exc.stderr or "")
        diagnostics.setup_duration = round(time.perf_counter() - started, 3)
        return diagnostics, build_execution_env(repo_dir, venv_path, include_pythonpath=include_pythonpath)
    except Exception as exc:
        diagnostics.setup_success = False
        diagnostics.setup_error = str(exc)
        diagnostics.setup_duration = round(time.perf_counter() - started, 3)
        return diagnostics, build_execution_env(repo_dir, venv_path, include_pythonpath=include_pythonpath)


def install_ci_tool(
    *,
    diagnostics: EnvSetupResult,
    repo_dir: Path,
    venv_path: Path,
    tool: str,
    env: dict[str, str],
    timeout: int = 120,
    dynamic: bool = False,
) -> bool:
    if tool not in CI_TOOLS:
        return False
    repo_dir = Path(repo_dir).resolve()
    venv_path = Path(venv_path).resolve()
    python = str(_python_executable(venv_path))
    if not dynamic and tool not in diagnostics.tools_missing:
        diagnostics.tools_missing.append(tool)
    if dynamic and tool not in diagnostics.dynamic_missing_tools:
        diagnostics.dynamic_missing_tools.append(tool)
    command = [python, "-m", "pip", "install", TOOL_PACKAGES[tool]]
    diagnostics.install_commands.append(command)
    try:
        installed = _run(command, cwd=repo_dir, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        diagnostics.setup_success = False
        diagnostics.setup_error = f"Install for {tool} timed out after {timeout}s"
        diagnostics.setup_stdout_tail += _tail(exc.stdout or "")
        diagnostics.setup_stderr_tail += _tail(exc.stderr or "")
        if tool not in diagnostics.install_failed:
            diagnostics.install_failed.append(tool)
        return False
    diagnostics.setup_stdout_tail += _tail(installed.stdout)
    diagnostics.setup_stderr_tail += _tail(installed.stderr)
    if installed.returncode == 0:
        target = diagnostics.dynamic_installed_tools if dynamic else diagnostics.tools_installed
        if tool not in target:
            target.append(tool)
        return True
    diagnostics.setup_success = False
    if tool not in diagnostics.install_failed:
        diagnostics.install_failed.append(tool)
    diagnostics.missing_tool = diagnostics.missing_tool or tool
    diagnostics.setup_error = installed.stderr or installed.stdout
    return False
