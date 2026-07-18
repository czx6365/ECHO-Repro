from __future__ import annotations

import json
import re
from typing import Any

from echo_ci.schema_utils import coerce_text

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
COMMAND_RE = re.compile(
    r"(^|\s)(python|python3|pytest|tox|nox|pip|make|flake8|ruff|mypy|black|isort|pre-commit|npm|yarn|pnpm|go|cargo|mvn|gradle)\b"
)
RUN_STEP_RE = re.compile(r"(?:##\[group\])?Run\s+(?P<command>.+)$")
ERROR_RE = re.compile(
    r"(ERROR|FAILED|FAILURES|AssertionError|ImportError|ModuleNotFoundError|Traceback|Exception|SyntaxError|TypeError|ValueError)",
    re.IGNORECASE,
)
TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def normalize_log(value: Any, *, max_chars: int | None = None) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(
                    str(
                        item.get("log")
                        or item.get("content")
                        or item.get("message")
                        or json.dumps(item, ensure_ascii=False, default=str)
                    )
                )
            else:
                parts.append(coerce_text(item))
        text = "\n".join(parts)
    elif isinstance(value, dict):
        text = str(
            value.get("log")
            or value.get("content")
            or value.get("message")
            or json.dumps(value, ensure_ascii=False, indent=2, default=str)
        )
    else:
        text = coerce_text(value)
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    return strip_ansi(text).replace("\r\n", "\n")


def tail_lines(text: str, line_count: int = 200) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-line_count:])


def _line_window(lines: list[str], index: int, before: int = 30, after: int = 40) -> list[str]:
    start = max(0, index - before)
    end = min(len(lines), index + after + 1)
    return lines[start:end]


def find_keyword_lines(log_text: str, keywords: list[str], *, context: int = 8) -> str:
    lines = log_text.splitlines()
    lowered_keywords = [keyword.lower() for keyword in keywords]
    chunks: list[str] = []
    seen: set[tuple[int, int]] = set()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if not any(keyword in lowered for keyword in lowered_keywords):
            continue
        start = max(0, index - context)
        end = min(len(lines), index + context + 1)
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        chunks.append("\n".join(lines[start:end]))
    return "\n\n".join(chunks)


def extract_failed_command(log_text: str) -> str:
    lines = [line.strip() for line in normalize_log(log_text).splitlines()]
    run_steps: list[str] = []
    candidates: list[str] = []
    for line in lines:
        run_match = RUN_STEP_RE.search(line)
        if run_match:
            command = run_match.group("command").strip()
            if command:
                run_steps.append(command)
        elif COMMAND_RE.search(line):
            cleaned = re.sub(r"^(?:\+|\$|>|\|)\s*", "", line).strip()
            if cleaned and len(cleaned) <= 240:
                candidates.append(cleaned)
    if run_steps:
        return run_steps[-1]
    return candidates[-1] if candidates else ""


def extract_failed_step(log_text: str) -> str:
    lines = [line.strip() for line in normalize_log(log_text).splitlines()]
    candidates = []
    for line in lines:
        if line.startswith("##[group]Run "):
            candidates.append(line.removeprefix("##[group]").strip())
        elif line.startswith("Run "):
            candidates.append(line)
        elif "failed" in line.lower() and len(line) < 180:
            candidates.append(line)
    return candidates[-1] if candidates else ""


def extract_error_signature(log_text: str, *, max_lines: int = 5) -> str:
    text = normalize_log(log_text)
    lines = text.splitlines()
    traceback_indices = [idx for idx, line in enumerate(lines) if TRACEBACK_RE.search(line)]
    if traceback_indices:
        start = traceback_indices[-1]
        tail = [line for line in lines[start:] if line.strip()]
        important = tail[-max_lines:] if len(tail) > max_lines else tail
        return "\n".join(important).strip()

    matches = [line.strip() for line in lines if ERROR_RE.search(line)]
    if matches:
        return "\n".join(matches[-max_lines:]).strip()

    return "\n".join(line for line in lines[-max_lines:] if line.strip()).strip()


def extract_dependency_context(log_text: str, *, max_lines: int = 20) -> list[str]:
    keywords = [
        "pip install",
        "requirements",
        "dependency",
        "dependencies",
        "setup.py",
        "pyproject.toml",
        "poetry",
        "tox",
        "ModuleNotFoundError",
        "ImportError",
    ]
    lines = []
    for line in normalize_log(log_text).splitlines():
        if any(keyword.lower() in line.lower() for keyword in keywords):
            lines.append(line.strip())
    return lines[-max_lines:]


def extract_environment_context(log_text: str, *, max_lines: int = 20) -> list[str]:
    keywords = [
        "python version",
        "python-version",
        "setup-python",
        "ubuntu",
        "macos",
        "windows",
        "permission denied",
        "no such file",
        "command not found",
        "missing",
        "PATH",
    ]
    lines = []
    for line in normalize_log(log_text).splitlines():
        if any(keyword.lower() in line.lower() for keyword in keywords):
            lines.append(line.strip())
    return lines[-max_lines:]


def extract_suspected_artifacts(*texts: str) -> list[str]:
    pattern = re.compile(
        r"(?:(?:[A-Za-z0-9_.-]+/)+)?[A-Za-z0-9_.-]+\.(?:py|yml|yaml|toml|ini|cfg|txt|lock|json|sh)"
    )
    artifacts: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in pattern.findall(text or ""):
            if match not in seen:
                seen.add(match)
                artifacts.append(match)
    return artifacts[:30]


def infer_failure_type(log_text: str, failed_command: str = "", default: str = "unknown") -> str:
    text = f"{failed_command}\n{log_text}".lower()
    if any(token in text for token in ("flake8", "ruff check", "mypy", "pylint")):
        return "lint"
    if any(token in text for token in ("black", "isort", "ruff format", "pre-commit", "trailing whitespace")):
        return "format"
    if default != "unknown":
        return default
    if any(token in text for token in ("modulenotfounderror", "importerror", "pip install", "dependency")):
        return "dependency"
    if any(token in text for token in ("pytest", "assertionerror", "failed")):
        return "test"
    if any(token in text for token in ("setup.py", "build", "wheel", "sdist")):
        return "build"
    if ".github/workflows" in text or "workflow" in text:
        return "workflow"
    return default


def extract_ci_log_snippet(log_text: str, failed_command: str = "", *, max_chars: int = 20_000) -> str:
    text = normalize_log(log_text)
    lines = text.splitlines()
    chunks: list[str] = []

    for index, line in enumerate(lines):
        if TRACEBACK_RE.search(line):
            chunks.append("\n".join(_line_window(lines, index, before=20, after=80)))
    if failed_command:
        for index, line in enumerate(lines):
            if failed_command in line:
                chunks.append("\n".join(_line_window(lines, index, before=25, after=80)))
    for index, line in enumerate(lines):
        if ERROR_RE.search(line):
            chunks.append("\n".join(_line_window(lines, index, before=12, after=24)))
    chunks.append(tail_lines(text, 200))

    merged: list[str] = []
    seen_chunks: set[str] = set()
    for chunk in chunks:
        chunk = chunk.strip()
        if chunk and chunk not in seen_chunks:
            seen_chunks.add(chunk)
            merged.append(chunk)

    snippet = "\n\n--- log context ---\n\n".join(merged)
    if len(snippet) > max_chars:
        return snippet[-max_chars:]
    return snippet
