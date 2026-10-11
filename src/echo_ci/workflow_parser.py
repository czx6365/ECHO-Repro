from __future__ import annotations

import re
from typing import Any

from echo_ci.schema_utils import coerce_text


def normalize_workflow(value: Any, *, max_chars: int | None = None) -> str:
    return coerce_text(value, max_chars=max_chars).replace("\r\n", "\n")


def extract_workflow_snippet(
    workflow_text: str,
    *,
    failed_command: str = "",
    max_chars: int = 8_000,
) -> str:
    text = normalize_workflow(workflow_text)
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()
    keywords = [failed_command, "run:", "uses:", "python-version", "pytest", "tox", "pip", "ruff", "mypy"]
    chunks: list[str] = []
    for index, line in enumerate(lines):
        lowered = line.lower()
        if any(keyword and keyword.lower() in lowered for keyword in keywords):
            start = max(0, index - 20)
            end = min(len(lines), index + 35)
            chunks.append("\n".join(lines[start:end]))
    snippet = "\n\n--- workflow context ---\n\n".join(dict.fromkeys(chunks))
    if not snippet:
        snippet = text[:max_chars]
    return snippet[:max_chars]


def extract_workflow_context(workflow_text: str, *, failed_command: str = "") -> list[str]:
    snippet = extract_workflow_snippet(workflow_text, failed_command=failed_command, max_chars=4_000)
    context = []
    for line in snippet.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if (
            stripped.startswith("name:")
            or stripped.startswith("jobs:")
            or stripped.startswith("runs-on:")
            or stripped.startswith("run:")
            or "python-version" in lowered
            or "pytest" in lowered
            or "tox" in lowered
            or "pip" in lowered
        ):
            context.append(stripped)
    return context[:30]


_CONFIG_SUFFIXES = (".toml", ".ini", ".cfg", ".yaml", ".yml", ".json")
_TARGET_SUFFIXES = (
    ".py",
    ".pyi",
    ".toml",
    ".ini",
    ".cfg",
    ".yaml",
    ".yml",
    ".json",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
)
_SETUP_RE = re.compile(
    r"(?:^|\s)(?:pip|uv\s+pip|poetry)\s+install\b|python\d*\s+-m\s+pip\s+install\b|"
    r"(?:^|/)(?:dev_)?setup\.sh\b",
    re.IGNORECASE,
)
_CI_TOOL_NAMES = (
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
)
_KNOWN_CONFIG_NAMES = {
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "mypy.ini",
    ".flake8",
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _workflow_commands(lines: list[str]) -> list[tuple[int, str]]:
    commands: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^(?P<space>\s*)(?:-\s*)?run:\s*(?P<value>.*)$", line)
        if not match:
            index += 1
            continue
        value = match.group("value").strip()
        base_indent = len(match.group("space"))
        if value and value not in {"|", ">", "|-", ">-"}:
            commands.append((index, value.strip('"\'')))
            index += 1
            continue
        # ``defaults: {run: {working-directory: ...}}`` and a job named
        # ``run`` are mappings, not executable steps.  Treat only an explicit
        # YAML block marker as a block command; otherwise the tolerant parser
        # can accidentally consume the remainder of the job as one command.
        if not value:
            index += 1
            continue
        block: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor]
            if candidate.strip() and _indent(candidate) <= base_indent:
                break
            stripped = candidate.strip()
            if stripped and not stripped.startswith("#"):
                block.append(stripped.rstrip("\\"))
            cursor += 1
        if block:
            commands.append((index, " ".join(block)))
        index = max(index + 1, cursor)
    return commands


def _path_tokens(text: str, suffixes: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    pattern = r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_./\\${}-]+(?:" + "|".join(
        re.escape(suffix) for suffix in suffixes
    ) + r"))"
    for match in re.finditer(pattern, text, re.IGNORECASE):
        prefix = text[max(0, match.start() - 6) : match.start()]
        if "{{" in prefix:
            # GitHub expressions such as ``${{ matrix.python-version }}``
            # are values, not repository paths (``matrix.py``).
            continue
        value = match.group(1).replace("\\", "/").strip("'\".,:;()[]{}")
        if value and value not in values:
            values.append(value)
    return values


def _is_config_path(path: str, command: str) -> bool:
    lowered = path.lower().replace("\\", "/")
    basename = lowered.rsplit("/", 1)[-1]
    if basename in _KNOWN_CONFIG_NAMES or "config" in basename:
        return True
    escaped = re.escape(path)
    return bool(
        re.search(
            rf"(?:--config(?:-file)?|--rcfile|-c)\s+(?:['\"])?{escaped}(?:['\"])?(?:\s|$)",
            command,
            re.IGNORECASE,
        )
    )


def _is_generated_output_path(path: str, command: str) -> bool:
    basename = path.lower().replace("\\", "/").rsplit("/", 1)[-1]
    if basename.startswith("coverage") and basename.endswith(".json"):
        return True
    return bool(re.search(rf">[^\n]*{re.escape(path)}(?:\s|$)", command))


def _command_score_segment(command: str, requested: str) -> int:
    normalized_command = " ".join(command.lower().split())
    normalized_requested = " ".join(requested.lower().split())
    if not normalized_requested:
        return 0 if _SETUP_RE.search(command) else 1
    score = 0
    if normalized_requested in normalized_command or normalized_command in normalized_requested:
        score += 100
    requested_parts = normalized_requested.split()
    command_parts = normalized_command.split()
    requested_tool = next((tool for tool in _CI_TOOL_NAMES if tool in requested_parts), "")
    requested_head = requested_tool or (requested_parts[0] if requested_parts else "")
    if requested_head and requested_head in command_parts:
        score += 40
    requested_paths = set(_path_tokens(requested, _TARGET_SUFFIXES))
    command_paths = set(_path_tokens(command, _TARGET_SUFFIXES))
    score += 10 * len(requested_paths & command_paths)
    if _SETUP_RE.search(command) and not _SETUP_RE.search(requested):
        score -= 25
    return score


def _command_score(command: str, requested: str) -> int:
    """Score against independent evidence lines instead of one log blob.

    Callers intentionally provide the extracted failed command, the generated
    command, and selected error lines together.  Joining those into one token
    sequence destroys exact matches (notably ``pre-commit run``), so retain the
    strongest individual piece of evidence.
    """

    segments = [segment.strip() for segment in requested.splitlines() if segment.strip()]
    if not segments:
        return _command_score_segment(command, "")
    # Evidence is ordered deliberately: extracted failed command, generated
    # command, then log lines. Prefer an equally strong earlier source so a
    # broad log line cannot tie and displace the explicit CI command.
    return max(
        _command_score_segment(command, segment) + max(0, 20 - index * 2)
        for index, segment in enumerate(segments)
    )


def _nearest_working_directory(
    lines: list[str],
    commands: list[tuple[int, str]],
    command_index: int,
) -> str:
    candidates: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^\s*working-directory:\s*(.+?)\s*$", line)
        if not match:
            continue
        value = match.group(1).strip().strip('"\'')
        distance = abs(index - command_index)
        preceding_penalty = 0 if index <= command_index else 8
        if distance <= 60:
            candidates.append((distance + preceding_penalty, index, value))
    for index, command in commands:
        if index > command_index or command_index - index > 25:
            continue
        match = re.search(r"(?:^|&&|;)\s*cd\s+([^;&|\s]+)", command)
        if match:
            value = match.group(1).strip().strip('"\'')
            candidates.append((command_index - index + 2, index, value))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], -item[1]))
    selected = candidates[0][2]
    return "" if selected in {".", "./", "$GITHUB_WORKSPACE", "${{ github.workspace }}"} else selected


def _workflow_environment(lines: list[str], command_index: int) -> dict[str, str]:
    environment: dict[str, str] = {}
    start = max(0, command_index - 30)
    end = min(len(lines), command_index + 12)
    in_env = False
    env_indent = -1
    for line in lines[start:end]:
        stripped = line.strip()
        if re.match(r"^(?:-\s*)?env:\s*$", stripped):
            in_env = True
            env_indent = _indent(line)
            continue
        if not in_env:
            continue
        if stripped and _indent(line) <= env_indent:
            in_env = False
            continue
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$", line)
        if match:
            environment[match.group(1)] = match.group(2).strip().strip('"\'')
    return environment


def _step_name(lines: list[str], command_index: int) -> str:
    """Return only the current step label, avoiding adjacent setup commands."""

    for index in range(command_index - 1, max(-1, command_index - 8), -1):
        stripped = lines[index].strip()
        match = re.match(r"^-\s*name:\s*(.*?)\s*$", stripped)
        if match:
            return match.group(1).strip().strip('"\'')
        if re.match(r"^-\s*(?:run|uses):", stripped):
            break
    return ""


def extract_workflow_execution_signal(
    workflow_text: str,
    *,
    command: str = "",
) -> dict[str, Any]:
    """Extract the execution semantics surrounding a workflow command.

    This intentionally uses a small tolerant parser instead of requiring a YAML
    dependency. It handles the GitHub Actions forms used by the benchmark:
    inline and block ``run``, job/step working directories, nearby ``env``, and
    setup commands.
    """

    text = normalize_workflow(workflow_text)
    empty = {
        "command": "",
        "working_directory": "",
        "environment": {},
        "config_files": [],
        "setup_commands": [],
        "target_scope": [],
        "source_line": None,
        "command_from_workflow": False,
    }
    if not text.strip():
        return empty
    lines = text.splitlines()
    commands = _workflow_commands(lines)
    if not commands:
        return empty
    requested_parts = command.strip().lower().split()
    requested_tools = {tool for tool in _CI_TOOL_NAMES if tool in requested_parts}
    requested_head = next(iter(requested_tools), "")
    if not requested_head and requested_parts:
        requested_head = requested_parts[0]
    ranked = sorted(
        (
            (
                _command_score(candidate, command)
                + (
                    30
                    if requested_tools
                    and any(tool in _step_name(lines, index).lower() for tool in requested_tools)
                    else 0
                ),
                index,
                candidate,
            )
            for index, candidate in commands
        ),
        key=lambda item: (item[0], -item[1]),
        reverse=True,
    )
    score, index, selected = ranked[0]
    setup_commands = [candidate for _, candidate in commands if _SETUP_RE.search(candidate)]
    nearby = "\n".join(lines[max(0, index - 25) : min(len(lines), index + 25)])
    config_files = [
        path
        for path in _path_tokens(f"{selected}\n{nearby}", _CONFIG_SUFFIXES)
        if _is_config_path(path, selected)
    ]
    target_scope = [
        path
        for path in _path_tokens(selected, _TARGET_SUFFIXES)
        if path not in config_files and not _is_generated_output_path(path, selected)
    ]
    return {
        "command": selected,
        "working_directory": _nearest_working_directory(lines, commands, index),
        "environment": _workflow_environment(lines, index),
        "config_files": config_files,
        "setup_commands": setup_commands,
        "target_scope": target_scope,
        "source_line": index + 1,
        "command_from_workflow": score > 0 if command.strip() else True,
    }
