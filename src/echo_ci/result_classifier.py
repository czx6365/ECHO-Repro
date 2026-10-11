from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass
from typing import Any

from echo_ci.env_policy import CI_TOOLS


@dataclass
class CommandExecution:
    phase: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timeout: bool = False
    error: str = ""
    matched_oracle: bool = False
    error_kind: str = ""
    repo_prepare_strategy: str = ""
    commit_check: dict[str, Any] | None = None
    checkout_success: bool | None = None
    checkout_error: str = ""
    checkout_stage: str = ""
    checkout_diagnostics: dict[str, Any] | None = None
    preflight: dict[str, Any] | None = None
    env_setup: dict[str, Any] | None = None
    project_name: str = ""
    target_preflight: dict[str, Any] | None = None
    dependency_diagnosis: dict[str, Any] | None = None
    workflow_signal: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _literal_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(value) for value in node.values)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args:
        return _literal_text(node.args[0])
    return ""


def _is_sys_exit_one(node: ast.AST) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    call = node.value
    func = call.func
    is_exit = (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "sys"
        and func.attr == "exit"
    ) or (isinstance(func, ast.Name) and func.id == "exit")
    if not is_exit:
        return False
    if not call.args:
        return False
    arg = call.args[0]
    return isinstance(arg, ast.Constant) and arg.value not in (0, None)


def _is_unconditional_raise(node: ast.AST) -> bool:
    if not isinstance(node, ast.Raise) or node.exc is None:
        return False
    text = ast.unparse(node.exc) if hasattr(ast, "unparse") else ""
    literal = _literal_text(node.exc)
    return "AssertionError" in text or re.search(r"test failed|failed|reproduced", literal, re.I)


def detect_fake_reproduction(
    reproduce_text: str,
    run_text: str = "",
    *,
    error_signature: str = "",
) -> tuple[bool, str]:
    lowered = reproduce_text.lower()
    real_execution_markers = (
        "open(",
        "subprocess",
        "pytest",
        "importlib",
        "__import__",
        "pathlib",
        "os.path",
        "glob",
        "ruff",
        "flake8",
        "mypy",
        "black",
        "isort",
        "pre-commit",
    )
    if re.search(r"print\([^)]*(error_signature|expected error|target error)", lowered) and re.search(
        r"(sys\.exit|exit)\s*\(\s*1\s*\)|raise\s+assertionerror", lowered
    ):
        return True, "prints an error signature and exits/raises without real execution"

    try:
        tree = ast.parse(reproduce_text)
    except SyntaxError:
        return False, ""

    meaningful_calls = 0
    top_level_fake = False
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)):
            continue
        if _is_unconditional_raise(node) or _is_sys_exit_one(node):
            top_level_fake = True
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                meaningful_calls += 1

    if top_level_fake and meaningful_calls <= 1:
        return True, "top-level unconditional failure without project execution"

    if error_signature:
        short_signature = " ".join(error_signature.split())[:120].lower()
        if short_signature and short_signature in " ".join(reproduce_text.split()).lower():
            if re.search(r"(sys\.exit|exit)\s*\(\s*1\s*\)|raise\s+assertionerror", lowered) and not any(
                marker in lowered for marker in real_execution_markers
            ):
                return True, "embeds the target error signature and forces failure"

    if re.fullmatch(r"\s*(?:import\s+sys\s*)?(?:sys\.)?exit\(\s*1\s*\)\s*", reproduce_text, re.S):
        return True, "unconditional exit(1)"
    if "raise assertionerror" in lowered and not re.search(r"pytest|subprocess|importlib|__import__|\bfrom\b|\bimport\b", lowered):
        return True, "unconditional AssertionError"
    return False, ""


def _combined(exec_result: CommandExecution | None) -> str:
    if exec_result is None:
        return ""
    return f"{exec_result.stdout}\n{exec_result.stderr}"


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PROJECT_PATH_PREFIX_RE = re.compile(
    r"(?:[A-Za-z]:)?(?:/[^/\s:'\"]+)+/(?=(?:src|test|tests|lib|libs|examples|scripts|tools|py)/)",
    re.IGNORECASE,
)


def normalize_oracle_text(
    text: str,
    *,
    repo_root: str = "",
    path_match_mode: str = "",
) -> str:
    normalized = _ANSI_RE.sub("", str(text or "")).replace("\\", "/")
    normalized_root = str(repo_root or "").replace("\\", "/").rstrip("/")
    if normalized_root:
        normalized = normalized.replace(normalized_root + "/", "")
    if path_match_mode == "repo_relative":
        normalized = _PROJECT_PATH_PREFIX_RE.sub("", normalized)
    lowered = normalized.lower()
    semantic: list[str] = []
    if re.search(r"\b(?:would reformat|reformatted)\b|files were modified by this hook", lowered):
        semantic.append("__formatter_change__")
    if re.search(r"\bfixing\b.*|imports? (?:are|is) incorrectly sorted|incorrectly sorted and/or formatted", lowered):
        semantic.append("__import_sort_change__")
    if semantic:
        lowered = f"{lowered}\n{' '.join(semantic)}"
    return " ".join(lowered.split())


def _oracle_signals(oracle: dict[str, Any]) -> list[str]:
    values = oracle.get("any_failing_signal")
    if not values:
        values = oracle.get("must_contain_on_failing", [])
    if isinstance(values, str):
        return [values]
    if isinstance(values, list):
        return [str(item) for item in values if str(item).strip()]
    return []


def _normalize_oracle_signal(signal: str, *, repo_root: str, path_match_mode: str) -> str:
    normalized = normalize_oracle_text(
        signal,
        repo_root=repo_root,
        path_match_mode=path_match_mode,
    )
    if "__formatter_change__" in normalized:
        return "__formatter_change__"
    if "__import_sort_change__" in normalized:
        return "__import_sort_change__"
    return normalized


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _project_candidates(exec_result: CommandExecution | None) -> set[str]:
    candidates: set[str] = set()
    if exec_result is None:
        return candidates
    project_name = exec_result.project_name or ""
    if "/" in project_name:
        project_name = project_name.rsplit("/", 1)[-1]
    for value in {project_name, project_name.replace("-", "_"), project_name.replace("_", "-")}:
        normalized = _normalized_name(value)
        if normalized:
            candidates.add(normalized)
    preflight = exec_result.preflight or {}
    for package in preflight.get("project_packages") or []:
        normalized = _normalized_name(str(package))
        if normalized:
            candidates.add(normalized)
    return candidates


def _extract_missing_module(text: str) -> str:
    patterns = [
        r"modulenotfounderror:\s+no module named ['\"]([^'\"]+)['\"]",
        r"importerror:\s+no module named ['\"]([^'\"]+)['\"]",
        r"no module named ['\"]([^'\"]+)['\"]",
        r"cannot import name ['\"]?([A-Za-z0-9_]+)['\"]?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).split(".")[0]
    return ""


def _missing_command(text: str) -> str:
    lowered = text.lower()
    patterns = [
        r"([a-z0-9_.+-]+): command not found",
        r"command not found: ([a-z0-9_.+-]+)",
        r"filenotfounderror: .*no such file or directory: '([^']+)'",
        r"no such file or directory: '([^']+)'",
        r"make: ([a-z0-9_.+-]+): no such file or directory",
    ]
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        raw_command = match.group(1).strip()
        if "/" in raw_command or "." in raw_command:
            continue
        command = raw_command.split("/")[-1]
        if command:
            return command
    return ""


def classify_dependency_subtype(exec_result: CommandExecution | None, *, prefix: str) -> tuple[str, str] | None:
    if exec_result is None:
        return None
    text = _combined(exec_result)
    if exec_result.error:
        text = f"{exec_result.error}\n{text}"
    lowered = text.lower()
    if not lowered:
        return None

    python_version_markers = (
        "requires python",
        "python version",
        "unsupported python",
        "invalid syntax",
        "syntaxerror",
        "match statements require",
        "cannot use starred expression",
    )
    if any(marker in lowered for marker in python_version_markers):
        return f"{prefix}_python_version_mismatch", text

    conflict_markers = (
        "version conflict",
        "resolutionimpossible",
        "incompatible",
        "requires ",
        "but you have",
        "dependency conflict",
    )
    if any(marker in lowered for marker in conflict_markers):
        return f"{prefix}_package_version_conflict", text

    missing_command = _missing_command(lowered)
    if missing_command in CI_TOOLS:
        return f"{prefix}_missing_ci_tool", text
    if missing_command:
        return f"{prefix}_dependency_unknown", text

    missing_module = _extract_missing_module(lowered)
    if missing_module:
        normalized_module = _normalized_name(missing_module)
        tool_modules = {tool.replace("-", "_") for tool in CI_TOOLS}
        if missing_module in tool_modules or missing_module.replace("_", "-") in CI_TOOLS:
            return f"{prefix}_missing_ci_tool", text
        project_candidates = _project_candidates(exec_result)
        if normalized_module in project_candidates or any(
            normalized_module.startswith(candidate) or candidate.startswith(normalized_module)
            for candidate in project_candidates
            if candidate
        ):
            return f"{prefix}_missing_project_package", text
        return f"{prefix}_missing_third_party_package", text

    dependency_markers = (
        "modulenotfounderror",
        "importerror",
        "dependency resolution",
        "distributionnotfound",
        "packagenotfounderror",
        "could not find a version",
        "no matching distribution",
        "python3 -m pip",
        "pip:",
        "command not found",
        "not found: ",
        "python: command not found",
        "python: no such file",
        "no module named",
        ".venv/bin/activate",
        "venv/bin/activate",
    )
    if any(marker in lowered for marker in dependency_markers):
        return f"{prefix}_dependency_unknown", text
    return None


def match_oracle(exec_result: CommandExecution, oracle: dict[str, Any]) -> bool:
    if exec_result.timeout:
        return False
    expected = "nonzero" if oracle.get("failing_exit_nonzero") is True else oracle.get(
        "expected_failing_exit_code", "nonzero"
    )
    if expected == "nonzero":
        exit_ok = exec_result.exit_code not in (0, None)
    elif expected is None:
        exit_ok = True
    else:
        exit_ok = exec_result.exit_code == int(expected)
    if not exit_ok:
        return False
    oracle_type = str(oracle.get("oracle_type") or "").lower()
    path_mode = str(oracle.get("path_match_mode") or ("repo_relative" if oracle_type == "format" else ""))
    repo_root = str((exec_result.preflight or {}).get("repo_root") or "")
    output = normalize_oracle_text(
        _combined(exec_result),
        repo_root=repo_root,
        path_match_mode=path_mode,
    )
    needles = [
        _normalize_oracle_signal(needle, repo_root=repo_root, path_match_mode=path_mode)
        for needle in _oracle_signals(oracle)
    ]
    needles = [needle for needle in needles if needle]
    if not needles:
        return bool(oracle.get("exit_transition_primary")) or not _oracle_signals(oracle)
    match_mode = str(oracle.get("failing_signal_match") or oracle.get("match_mode") or "all").lower()
    if oracle_type == "format" and len(needles) > 1 and "failing_signal_match" not in oracle:
        match_mode = "any"
    if match_mode in {"any", "any_of"}:
        return any(needle in output for needle in needles)
    return all(needle in output for needle in needles)


def _fixed_passes(exec_result: CommandExecution | None, oracle: dict[str, Any]) -> bool:
    if exec_result is None or exec_result.timeout:
        return False
    expected = 0 if oracle.get("fixed_exit_zero") is True else oracle.get("expected_passing_exit_code", 0)
    if exec_result.exit_code != expected:
        return False
    oracle_type = str(oracle.get("oracle_type") or "").lower()
    path_mode = str(oracle.get("path_match_mode") or ("repo_relative" if oracle_type == "format" else ""))
    repo_root = str((exec_result.preflight or {}).get("repo_root") or "")
    lines = [
        normalize_oracle_text(line, repo_root=repo_root, path_match_mode=path_mode)
        for line in _combined(exec_result).splitlines()
    ]
    forbidden = [
        _normalize_oracle_signal(str(item), repo_root=repo_root, path_match_mode=path_mode)
        for item in oracle.get("must_not_contain_on_fixed", [])
        if str(item).strip()
    ]
    for item in forbidden:
        for line in lines:
            if item not in line:
                continue
            if any(marker in line for marker in ("pass", "passed", "resolved", "no ", "not found", "without")):
                continue
            return False
    return True


def classify_error_text(text: str) -> str | None:
    lowered = text.lower()
    if "syntaxerror" in lowered or ".echo_repro/run.sh" in lowered or "no such file or directory" in lowered:
        return "harness_error"
    if "modulenotfounderror" in lowered or "importerror" in lowered or "dependency resolution" in lowered:
        return "dependency_error"
    if "command not found" in lowered or "permission denied" in lowered or "python version" in lowered:
        return "environment_error"
    return None


def classify_fixed_error(fixed: CommandExecution | None) -> tuple[str, str]:
    if fixed is None:
        return "fixed_checkout_error", "Fixed commit was not checked out or no fixed execution was recorded."
    if fixed.error_kind:
        return fixed.error_kind, fixed.error or fixed.stderr or fixed.stdout
    text = _combined(fixed)
    lowered = text.lower()
    preflight = fixed.preflight or {}
    if fixed.error:
        lowered = f"{fixed.error}\n{lowered}".lower()

    if preflight:
        if preflight.get("echo_repro_dir_exists") is False:
            return "fixed_missing_echo_repro_dir", fixed.error or text
        if preflight.get("run_sh_exists") is False:
            return "fixed_missing_run_sh", fixed.error or text
        run_content = str(preflight.get("run_sh_content") or "")
        if "reproduce.py" in run_content and preflight.get("reproduce_py_exists") is False:
            return "fixed_missing_reproduce_py", fixed.error or text
        if re.search(r"/(?:work|outputs|failing_repo|fixed_repo|ci_repair)", run_content):
            return "fixed_hardcoded_failing_path", fixed.error or text

    target_preflight = fixed.target_preflight or preflight.get("cross_commit_targets") or {}
    if target_preflight.get("has_renamed_by_fix"):
        return "fixed_target_renamed_by_fix", text
    if target_preflight.get("has_deleted_by_fix") and fixed.exit_code not in (0, None):
        return "fixed_target_deleted_by_fix", text
    if target_preflight.get("has_invalid_target"):
        return "fixed_target_hallucinated", text

    checkout_markers = (
        "reference is not a tree",
        "pathspec",
        "did not match any file",
        "bad object",
        "not a git repository",
        "missing commit sha",
        "fixed checkout",
    )
    if any(marker in lowered for marker in checkout_markers):
        return "fixed_checkout_error", fixed.error or text

    dependency_subtype = classify_dependency_subtype(fixed, prefix="fixed")
    if dependency_subtype is not None:
        return dependency_subtype

    if "not a git repository" in lowered or "git rev-parse" in lowered or "cd: " in lowered:
        return "fixed_wrong_workdir", text

    if ("can't open file" in lowered or "cannot open file" in lowered) and "reproduce.py" in lowered:
        if preflight.get("reproduce_py_exists"):
            return "fixed_wrong_run_sh_path", text
        return "fixed_missing_reproduce_py", text

    quoted_missing = re.search(r"no such file or directory: '([^']+)'", lowered)
    if quoted_missing:
        missing_path = quoted_missing.group(1)
        if ".echo_repro" in missing_path and not missing_path.endswith(("reproduce.py", "run.sh", "oracle.json")):
            return "fixed_missing_project_target_file", text
        if re.search(r"\.(?:py|toml|ya?ml|ini|cfg|txt|tsx|ts|js|jsx)$", missing_path):
            return "fixed_missing_project_target_file", text

    project_file_missing = re.search(
        r"(?:no such file or directory|filenotfounderror|file not found|not found).*?([A-Za-z0-9_./-]+\.(?:py|toml|ya?ml|ini|cfg|txt|tsx|ts|js|jsx))",
        lowered,
    )
    if project_file_missing and ".echo_repro" not in project_file_missing.group(1):
        return "fixed_missing_project_target_file", text

    harness_path_markers = (
        ".echo_repro/run.sh",
        ".echo_repro/reproduce.py",
        "can't open file",
        "cannot open file",
        "no such file or directory",
        "filenotfounderror",
        "permission denied",
        "syntaxerror",
        "unterminated",
    )
    if any(marker in lowered for marker in harness_path_markers):
        return "fixed_path_unknown", text

    if fixed.exit_code not in (0, None):
        return "fixed_command_fail", text

    return "fixed_error", fixed.error or text


def classify_failing_error(failing: CommandExecution | None) -> tuple[str, str]:
    if failing is None:
        return "failing_checkout_error", "Failing commit was not checked out or no failing execution was recorded."
    if failing.error_kind:
        return failing.error_kind, failing.error or failing.stderr or failing.stdout
    text = _combined(failing)
    lowered = f"{failing.error}\n{text}".lower()
    checkout_markers = (
        "reference is not a tree",
        "unable to read tree",
        "pathspec",
        "did not match any file",
        "bad object",
        "missing commit sha",
    )
    if any(marker in lowered for marker in checkout_markers):
        return "failing_checkout_error", failing.error or text
    dependency_subtype = classify_dependency_subtype(failing, prefix="failing")
    if dependency_subtype is not None:
        return dependency_subtype
    error_type = classify_error_text(lowered)
    return error_type or "harness_error", failing.error or text


def classify_result(
    *,
    failing: CommandExecution | None,
    fixed: CommandExecution | None,
    oracle: dict[str, Any],
    fake_reproduction_detected: bool = False,
    generation_status: str = "generated",
) -> str:
    if generation_status == "skipped_no_llm":
        return "skipped_no_llm"
    if fake_reproduction_detected:
        return "fake_reproduction"
    if (failing and failing.timeout) or (fixed and fixed.timeout):
        return "timeout"
    if failing is None or failing.error:
        return classify_failing_error(failing)[0]
    if fixed is None or fixed.error:
        return classify_fixed_error(fixed)[0]

    failing_text = _combined(failing)
    fixed_text = _combined(fixed)
    fixed_error_type, _ = classify_fixed_error(fixed)
    if fixed.exit_code not in (0, None) and fixed_error_type != "fixed_error":
        return fixed_error_type

    error_type = classify_error_text(failing_text + "\n" + fixed_text)
    if error_type in {"dependency_error", "environment_error", "harness_error"} and fixed.exit_code not in (0, None):
        return error_type

    failing_matched = match_oracle(failing, oracle)
    fixed_passed = _fixed_passes(fixed, oracle)
    if failing.exit_code == 0:
        return "buggy_pass"
    if failing.exit_code not in (0, None) and fixed.exit_code not in (0, None):
        return "buggy_fail_fixed_fail"
    if failing_matched and fixed_passed:
        return "reproduced"
    if failing.exit_code not in (0, None) and fixed_passed:
        return "oracle_error"
    return "unknown"
