from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

from echo_ci.failure_spec import FailureSpec
from echo_ci.log_parser import extract_ci_log_snippet, normalize_log
from echo_ci.schema_utils import coerce_text, field
from echo_ci.workflow_parser import extract_workflow_snippet

IGNORED_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
}

ENV_FILE_NAMES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "tox.ini",
    "pytest.ini",
    "noxfile.py",
    "dockerfile",
    "environment.yml",
    "environment.yaml",
    "poetry.lock",
    "pipfile",
    "pipfile.lock",
}

STOPWORDS = {
    "and",
    "are",
    "but",
    "ci",
    "error",
    "expected",
    "failed",
    "failure",
    "for",
    "from",
    "have",
    "issue",
    "not",
    "run",
    "should",
    "that",
    "the",
    "this",
    "with",
}


@dataclass
class ContextBundle:
    ci_log: str = ""
    workflow: str = ""
    source: str = ""
    tests: str = ""
    env: str = ""
    failure_spec: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_ignored_path(path: Path, repo_path: Path) -> bool:
    try:
        parts = path.relative_to(repo_path).parts
    except ValueError:
        parts = path.parts
    return any(part.lower() in IGNORED_DIR_NAMES for part in parts[:-1])


def _tokens(*parts: str) -> set[str]:
    text = "\n".join(part for part in parts if part)
    tokens = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|\d+", text.lower()):
        if token not in STOPWORDS:
            tokens.add(token)
    return tokens


def collect_repo_files(repo_path: Path) -> list[Path]:
    repo_path = Path(repo_path)
    if not repo_path.exists():
        return []
    files = []
    for path in repo_path.rglob("*"):
        if not path.is_file() or _is_ignored_path(path, repo_path):
            continue
        files.append(path)
    return sorted(files)


def _read_text(path: Path, max_chars: int = 50_000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(data) > max_chars:
        return data[:max_chars]
    return data


def _score_path(path: Path, query_tokens: set[str]) -> int:
    text = f"{path}\n{_read_text(path, 12_000)}".lower()
    score = 0
    for token in query_tokens:
        if token in str(path).lower():
            score += 4
        if token in text:
            score += 2
    return score


def _snippet(path: Path, query_tokens: set[str], max_chars: int) -> str:
    text = _read_text(path, 80_000)
    if len(text) <= max_chars:
        return text
    lowered = text.lower()
    positions = [lowered.find(token) for token in query_tokens if lowered.find(token) >= 0]
    if not positions:
        return text[:max_chars]
    best_start = 0
    best_score = -1
    for position in positions:
        start = max(0, position - max_chars // 3)
        end = min(len(text), start + max_chars)
        window = lowered[start:end]
        score = sum(1 for token in query_tokens if token in window)
        if score > best_score:
            best_start = start
            best_score = score
    snippet = text[best_start : best_start + max_chars]
    if best_start > 0:
        snippet = "...\n" + snippet
    if best_start + max_chars < len(text):
        snippet += "\n..."
    return snippet


def _format_snippets(paths: list[Path], repo_path: Path, query_tokens: set[str], max_chars: int) -> str:
    blocks = []
    for path in paths:
        try:
            rel = path.relative_to(repo_path)
        except ValueError:
            rel = path
        blocks.append(f"### {rel}\n{_snippet(path, query_tokens, max_chars)}")
    return "\n\n".join(blocks)


def _ranked(paths: list[Path], query_tokens: set[str], top_k: int) -> list[Path]:
    scored = [(_score_path(path, query_tokens), str(path), path) for path in paths]
    ranked = sorted(scored, reverse=True)
    positive = [path for score, _, path in ranked if score > 0]
    if positive:
        return positive[:top_k]
    return [path for _, _, path in ranked[:top_k]]


def _is_test_file(path: Path) -> bool:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or "tests" in parts
    )


def retrieve_context_bundle(
    *,
    instance: dict[str, Any],
    failure_spec: FailureSpec,
    repo_path: Path | None = None,
    source_top_k: int = 5,
    test_top_k: int = 5,
    env_top_k: int = 10,
    workflow_top_k: int = 3,
    max_snippet_chars: int = 1_500,
    max_log_chars: int = 20_000,
) -> ContextBundle:
    ci_log_raw = normalize_log(field(instance, "ci_log", default=""))
    workflow_raw = coerce_text(field(instance, "workflow_content", default=""))
    query_tokens = _tokens(
        failure_spec.error_signature,
        failure_spec.failed_command,
        " ".join(failure_spec.suspected_artifacts),
        " ".join(failure_spec.dependency_context),
        failure_spec.failure_type,
    )

    source = tests = env = ""
    workflow_blocks: list[str] = []
    if workflow_raw:
        workflow_blocks.append(
            "### dataset workflow\n"
            + extract_workflow_snippet(
                workflow_raw,
                failed_command=failure_spec.failed_command,
                # Workflow execution semantics often sit several steps after
                # setup. Keep enough of the dataset workflow to preserve job
                # defaults, `cd`, and the eventual failed command together.
                max_chars=max(8_000, max_snippet_chars),
            )
        )

    if repo_path and Path(repo_path).exists():
        repo_path = Path(repo_path)
        files = collect_repo_files(repo_path)
        source_files = [
            path for path in files if path.suffix == ".py" and not _is_test_file(path)
        ]
        test_files = [path for path in files if path.suffix == ".py" and _is_test_file(path)]
        env_files = [
            path
            for path in files
            if path.name.lower() in ENV_FILE_NAMES
            or path.name.lower().startswith("requirements")
            or path.name.lower().endswith((".ini", ".cfg", ".toml"))
        ]
        workflow_files = [
            path
            for path in files
            if ".github" in path.parts
            and "workflows" in path.parts
            and path.suffix.lower() in {".yml", ".yaml"}
        ]

        source = _format_snippets(
            _ranked(source_files, query_tokens, source_top_k),
            repo_path,
            query_tokens,
            max_snippet_chars,
        )
        tests = _format_snippets(
            _ranked(test_files, query_tokens, test_top_k),
            repo_path,
            query_tokens,
            max_snippet_chars,
        )
        env = _format_snippets(
            _ranked(env_files, query_tokens, env_top_k),
            repo_path,
            query_tokens,
            max_snippet_chars,
        )
        workflow_blocks.extend(
            _format_snippets(
                _ranked(workflow_files, query_tokens, workflow_top_k),
                repo_path,
                query_tokens,
                max_snippet_chars,
            ).split("\n\n")
        )

    return ContextBundle(
        ci_log=extract_ci_log_snippet(
            ci_log_raw,
            failed_command=failure_spec.failed_command,
            max_chars=max_log_chars,
        ),
        workflow="\n\n".join(block for block in workflow_blocks if block.strip()),
        source=source,
        tests=tests,
        env=env,
        failure_spec=failure_spec.to_dict(),
    )


def save_context_bundle(bundle: ContextBundle, output_dir: Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "ci_log_snippet.txt": bundle.ci_log,
        "workflow_snippet.yml": bundle.workflow,
        "source_snippets.txt": bundle.source,
        "test_snippets.txt": bundle.tests,
        "env_snippets.txt": bundle.env,
        "context_bundle.json": json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2),
    }
    for name, text in files.items():
        (output_dir / name).write_text(text, encoding="utf-8")
