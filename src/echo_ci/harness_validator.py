from __future__ import annotations

import ast
import re
from typing import Any

from echo_ci.ci_context_retriever import ContextBundle
from echo_ci.result_classifier import detect_fake_reproduction
from echo_ci.workflow_parser import extract_workflow_execution_signal

GENERIC_ORACLE_TERMS = {"error", "failed", "failure", "fail", "exception", "traceback", "warning"}
INSTALL_FAILURE_TYPES = {"dependency", "build", "install"}
PROJECT_INTERACTION_RE = re.compile(
    r"(\.echo_repro/reproduce\.py|pytest|tox|nox|ruff|flake8|mypy|black|isort|pre-commit|pylint|python3?\s+-m\s+build|setup\.py|importlib|__import__|open\(|subprocess|pathlib|os\.path|glob\()",
    re.IGNORECASE,
)
HEAVY_SETUP_PATTERNS = (
    (re.compile(r"\bpip\s+install\b", re.IGNORECASE), "pip install"),
    (re.compile(r"\bpython3?\s+-m\s+pip\s+install\b", re.IGNORECASE), "python -m pip install"),
    (re.compile(r"\bpip\s+install\s+-r\s+\S*requirements", re.IGNORECASE), "pip install -r requirements"),
    (re.compile(r"\bpython3?\s+-m\s+pip\s+install\s+-r\s+\S*requirements", re.IGNORECASE), "python -m pip install -r requirements"),
    (re.compile(r"\bpip\s+install\s+\.", re.IGNORECASE), "pip install ."),
    (re.compile(r"\bpython3?\s+-m\s+pip\s+install\s+\.", re.IGNORECASE), "python -m pip install ."),
    (re.compile(r"\bpoetry\s+install\b", re.IGNORECASE), "poetry install"),
    (re.compile(r"\bapt(?:-get)?\s+install\b", re.IGNORECASE), "apt-get install"),
    (re.compile(r"\bdocker\s+build\b", re.IGNORECASE), "docker build"),
    (re.compile(r"\b(?:curl|wget)\b", re.IGNORECASE), "external download"),
)


def _is_install_allowed(context: ContextBundle) -> bool:
    spec = context.failure_spec
    failure_type = str(spec.get("failure_type") or "").lower()
    oracle_type = str(spec.get("oracle_type") or "").lower()
    failed_command = str(spec.get("failed_command") or "").lower()
    if failure_type in INSTALL_FAILURE_TYPES or oracle_type in INSTALL_FAILURE_TYPES:
        return True
    return any(token in failed_command for token in ("pip install", "poetry install", "apt-get", "docker build", "build"))


def _shell_has_unconditional_exit_one(run_text: str) -> bool:
    for line in run_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.fullmatch(r"(?:exit|return)\s+1", stripped):
            return True
    return False


def _only_echo_then_fail(run_text: str, error_signature: str) -> bool:
    meaningful = [
        line.strip()
        for line in run_text.splitlines()
        if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("set ")
    ]
    if not meaningful:
        return False
    if not any(re.match(r"echo\b|printf\b", line) for line in meaningful):
        return False
    if not any(re.fullmatch(r"(?:exit|return)\s+1", line) for line in meaningful):
        return False
    signature = " ".join(error_signature.split()).lower()[:120]
    joined = " ".join(meaningful).lower()
    return not signature or signature in joined or any(term in joined for term in GENERIC_ORACLE_TERMS)


def _python_has_only_print_then_fail(reproduce_text: str, error_signature: str) -> bool:
    try:
        tree = ast.parse(reproduce_text)
    except SyntaxError:
        return False
    body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    if not body:
        return False
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    has_print = any(isinstance(call.func, ast.Name) and call.func.id == "print" for call in calls)
    has_real_marker = bool(PROJECT_INTERACTION_RE.search(reproduce_text))
    has_exit_one = bool(re.search(r"(sys\.)?exit\s*\(\s*1\s*\)", reproduce_text))
    signature = " ".join(error_signature.split()).lower()[:120]
    joined = " ".join(reproduce_text.split()).lower()
    return has_print and has_exit_one and not has_real_marker and (not signature or signature in joined)


def _oracle_terms(oracle: dict[str, Any]) -> list[str]:
    value = oracle.get("any_failing_signal") or oracle.get("must_contain_on_failing", [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _failed_command_is_used(text: str, failed_command: str) -> bool:
    if not failed_command.strip():
        return False
    normalized_text = " ".join(text.lower().split())
    normalized_command = " ".join(failed_command.lower().split())
    if normalized_command in normalized_text:
        return True
    head = normalized_command.split()[0] if normalized_command.split() else ""
    return bool(head and head in normalized_text)


def _selected_command(grounding: dict[str, Any] | None, fallback: str) -> str:
    grounding = grounding or {}
    return str(grounding.get("selected_command") or fallback or "").strip()


def _command_is_preserved(text: str, command: str) -> bool:
    if not command:
        return False
    if _failed_command_is_used(text, command):
        return True
    normalized = " ".join(text.lower().replace("'", " ").replace('"', " ").split())
    parts = command.lower().split()
    tools = [
        tool
        for tool in ("pytest", "ruff", "flake8", "mypy", "black", "isort", "pylint", "pre-commit", "tox")
        if tool in parts
    ]
    return bool(tools and all(tool in normalized for tool in tools))


def _path_is_preserved(text: str, path: str) -> bool:
    normalized_text = text.replace("\\", "/").lower()
    normalized_path = path.replace("\\", "/").strip("./").lower()
    return bool(normalized_path and normalized_path in normalized_text)


def workflow_signal_preservation(
    *,
    combined_text: str,
    selected_command: str,
    workflow_text: str,
) -> dict[str, Any]:
    signal = extract_workflow_execution_signal(workflow_text, command=selected_command)
    command_from_workflow = bool(signal.get("command_from_workflow"))
    command_preserved = _command_is_preserved(combined_text, selected_command)
    working_directory = str(signal.get("working_directory") or "")
    working_directory_preserved = (
        _path_is_preserved(combined_text, working_directory)
        if working_directory
        else None
    )
    signal_command = str(signal.get("command") or "")
    config_files = [
        str(path)
        for path in signal.get("config_files") or []
        if _path_is_preserved(signal_command, str(path))
    ]
    target_scope = [str(path) for path in signal.get("target_scope") or []]
    config_preserved = (
        all(_path_is_preserved(combined_text, path) for path in config_files)
        if config_files
        else None
    )
    target_preserved = (
        all(_path_is_preserved(combined_text, path) for path in target_scope)
        if target_scope
        else None
    )
    checks = [command_preserved]
    checks.extend(
        value
        for value in (working_directory_preserved, config_preserved, target_preserved)
        if value is not None
    )
    return {
        "workflow_signal": signal,
        "command_from_workflow": command_from_workflow,
        "command_preserved": command_preserved,
        "working_directory_preserved": working_directory_preserved,
        "config_path_preserved": config_preserved,
        "target_scope_preserved": target_preserved,
        "preserved": bool(checks) and all(checks),
        "preserved_count": sum(1 for value in checks if value),
        "expected_count": len(checks),
    }


def validate_harness(
    *,
    reproduce_text: str,
    run_text: str,
    oracle: dict[str, Any],
    context: ContextBundle,
    setting: str = "",
    grounding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    spec = context.failure_spec
    failure_type = str(spec.get("failure_type") or "unknown").lower()
    oracle_type = str(oracle.get("oracle_type") or spec.get("oracle_type") or "unknown").lower()
    failed_command = str(spec.get("failed_command") or "")
    error_signature = str(spec.get("error_signature") or "")
    combined = f"{run_text}\n{reproduce_text}"
    selected_command = _selected_command(grounding, failed_command)
    workflow_settings = {
        "S4_log_workflow",
        "S5_full_context",
        "S6_full_refine",
        "S7_router",
        "S8_router_typed_refine",
        "S9_oracle_router",
    }
    if setting in workflow_settings:
        signal_preservation = {
            "applicable": True,
            **workflow_signal_preservation(
                combined_text=combined,
                selected_command=selected_command,
                workflow_text=context.workflow,
            ),
        }
    else:
        # Do not expose workflow-derived diagnostics to treatments whose
        # experimental input excludes workflow context.
        signal_preservation = {
            "applicable": False,
            "workflow_signal": {},
            "command_from_workflow": False,
            "command_preserved": None,
            "working_directory_preserved": None,
            "config_path_preserved": None,
            "target_scope_preserved": None,
            "preserved": None,
            "preserved_count": 0,
            "expected_count": 0,
        }

    fake, fake_reason = detect_fake_reproduction(
        reproduce_text,
        run_text,
        error_signature=error_signature,
    )
    if fake:
        issues.append(f"fake reproduction: {fake_reason}")
    if _shell_has_unconditional_exit_one(run_text):
        issues.append("fake reproduction: run.sh has unconditional exit 1")
    if _only_echo_then_fail(run_text, error_signature) or _python_has_only_print_then_fail(reproduce_text, error_signature):
        issues.append("fake reproduction: only prints/echoes the target failure then exits")

    if not _is_install_allowed(context):
        for pattern, label in HEAVY_SETUP_PATTERNS:
            if pattern.search(combined):
                issues.append(f"over-heavy setup: {label} is not allowed for {failure_type}/{oracle_type}")

    if not PROJECT_INTERACTION_RE.search(combined) and not _failed_command_is_used(combined, failed_command):
        issues.append("missing project interaction: harness does not call project code, test/lint/build tooling, or failed_command")

    if reproduce_text.strip() and ".echo_repro/reproduce.py" not in run_text:
        if _failed_command_is_used(run_text, failed_command) or PROJECT_INTERACTION_RE.search(run_text):
            warnings.append("path issue: run.sh does not call .echo_repro/reproduce.py, but it runs a plausible direct command")
        else:
            issues.append("path issue: run.sh does not call .echo_repro/reproduce.py")

    oracle_terms = [term.strip().lower() for term in _oracle_terms(oracle) if term.strip()]
    if oracle_type in {"exception", "assertion", "lint", "format", "test"} and not oracle_terms:
        issues.append(f"oracle issue: must_contain_on_failing is empty for {oracle_type}")
    if oracle_terms and all(term in GENERIC_ORACLE_TERMS for term in oracle_terms):
        issues.append("oracle issue: must_contain_on_failing only contains generic words")
    for term in oracle_terms:
        if term in GENERIC_ORACLE_TERMS:
            warnings.append(f"oracle issue: generic oracle term `{term}` may be too broad")

    if failed_command and not _failed_command_is_used(combined, failed_command):
        warnings.append("grounding issue: failed_command was available but not visibly reused")

    if setting in workflow_settings and signal_preservation[
        "command_from_workflow"
    ]:
        if not signal_preservation["command_preserved"]:
            issues.append("workflow signal issue: selected workflow command is not preserved")
        if signal_preservation["working_directory_preserved"] is False:
            working_directory = signal_preservation["workflow_signal"].get("working_directory")
            issues.append(
                "workflow signal issue: run.sh/reproduce.py does not preserve "
                f"working-directory `{working_directory}`"
            )
        if signal_preservation["config_path_preserved"] is False:
            issues.append("workflow signal issue: workflow config path is not preserved")
        if signal_preservation["target_scope_preserved"] is False:
            issues.append("workflow signal issue: workflow target scope is not preserved")

    return {
        "valid": not issues,
        "issues": issues,
        "warnings": warnings,
        "workflow_signal_preservation": signal_preservation,
    }
