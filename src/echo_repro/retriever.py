from __future__ import annotations

from pathlib import Path
import re

from echo_repro.models import BugSpec, RetrievedContext
from echo_repro.utils.file_utils import read_text_safe

ENV_FILE_NAMES = {
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "dockerfile",
    "docker-compose.yml",
    ".env.example",
}

TEST_FILE_NAMES = {"tests.py", "conftest.py"}

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
    "venv",
}

IGNORED_DIR_SUFFIXES = (
    ".egg",
    ".egg-info",
    ".dist-info",
)

STOPWORDS = {
    "able",
    "about",
    "after",
    "also",
    "and",
    "are",
    "available",
    "behavior",
    "but",
    "can",
    "cannot",
    "current",
    "does",
    "expected",
    "for",
    "from",
    "has",
    "have",
    "into",
    "issue",
    "only",
    "return",
    "returns",
    "should",
    "that",
    "the",
    "this",
    "when",
    "with",
    "without",
}


def _query_tokens(query: str) -> set[str]:
    tokens = set()
    for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*|\d+", query.lower()):
        if token in STOPWORDS:
            continue
        if len(token) < 3 and not token.startswith("_"):
            continue
        tokens.add(token)
    return tokens


def _is_ignored_path(path: Path, repo_path: Path) -> bool:
    try:
        parts = path.relative_to(repo_path).parts
    except ValueError:
        parts = path.parts
    for part in parts[:-1]:
        lowered = part.lower()
        if lowered in IGNORED_DIR_NAMES or lowered.endswith(IGNORED_DIR_SUFFIXES):
            return True
    return False


def collect_files(repo_path: Path) -> list[Path]:
    repo_path = Path(repo_path)
    files = []
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        if path.name == "reproduce.py":
            continue
        if _is_ignored_path(path, repo_path):
            continue
        files.append(path)
    return sorted(files)


def _score_file(path: Path, query: str, preferred_kind: str) -> int:
    query_tokens = _query_tokens(query)
    path_text = str(path).lower()
    content = read_text_safe(path, max_chars=12_000).lower()
    score = 0
    for token in query_tokens:
        if token in path_text:
            score += 3
        if token in content:
            score += 5

    if preferred_kind == "source":
        if path.suffix == ".py":
            score += 5
        if "test" in path.name.lower():
            score -= 3
        if any(part.lower() in {"doc", "docs", "example", "examples"} for part in path.parts):
            score -= 6
    elif preferred_kind == "test":
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            score += 8
        if path.name in TEST_FILE_NAMES or path.name == "conftest.py":
            score += 7
        if "test" in path.parts or "tests" in path.parts:
            score += 4

    return score


def _top_files(files: list[Path], query: str, preferred_kind: str, top_k: int) -> list[Path]:
    scored = [
        (_score_file(path, query, preferred_kind), path)
        for path in files
    ]
    ranked = sorted(scored, key=lambda item: (item[0], item[1].suffix == ".py"), reverse=True)
    return [path for score, path in ranked if score > 0][:top_k]


def retrieve_source_files(repo_path: Path, query: str, top_k: int = 5) -> list[Path]:
    files = [path for path in collect_files(repo_path) if path.suffix == ".py"]
    return _top_files(files, query, preferred_kind="source", top_k=top_k)


def retrieve_test_files(repo_path: Path, query: str, top_k: int = 5) -> list[Path]:
    files = []
    for path in collect_files(repo_path):
        if path.suffix != ".py":
            continue
        name = path.name.lower()
        if (
            name.startswith("test_")
            or name.endswith("_test.py")
            or name in TEST_FILE_NAMES
            or name == "conftest.py"
            or "tests" in path.parts
        ):
            files.append(path)
    return _top_files(files, query, preferred_kind="test", top_k=top_k)


def retrieve_env_files(repo_path: Path) -> list[Path]:
    files = []
    for path in collect_files(repo_path):
        name = path.name.lower()
        if name in ENV_FILE_NAMES or name.endswith((".ini", ".cfg", ".toml", ".yaml", ".yml")):
            files.append(path)
    return files[:10]


def _read_relevant_snippet(path: Path, query: str, max_chars: int = 700) -> str:
    text = read_text_safe(path, max_chars=50_000)
    if len(text) <= max_chars:
        return text

    lowered = text.lower()
    tokens = _query_tokens(query)
    positions = [lowered.find(token) for token in tokens if token in lowered]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return text[:max_chars]

    best_start = 0
    best_score = -1
    for position in positions:
        start = max(0, position - max_chars // 3)
        end = min(len(text), start + max_chars)
        start = max(0, end - max_chars)
        window = lowered[start:end]
        score = sum(1 for token in tokens if token in window)
        if score > best_score or (score == best_score and start < best_start):
            best_score = score
            best_start = start

    snippet = text[best_start:best_start + max_chars]
    if best_start > 0:
        snippet = "...\n" + snippet
    if best_start + max_chars < len(text):
        snippet = snippet + "\n..."
    return snippet


def _build_snippets(paths: list[Path], query: str = "", max_chars: int = 700) -> dict[str, str]:
    return {str(path): _read_relevant_snippet(path, query=query, max_chars=max_chars) for path in paths}


def retrieve_context(repo_path: Path, bug_spec: BugSpec) -> RetrievedContext:
    repo_path = Path(repo_path)
    query = " ".join(
        [
            bug_spec.title,
            bug_spec.current_behavior,
            bug_spec.expected_behavior,
            " ".join(bug_spec.keywords),
            " ".join(bug_spec.suspect_symbols),
        ]
    )
    source_files = retrieve_source_files(repo_path, query=query)
    test_files = retrieve_test_files(repo_path, query=query)
    env_files = retrieve_env_files(repo_path)
    return RetrievedContext(
        repo_path=repo_path,
        source_files=source_files,
        test_files=test_files,
        env_files=env_files,
        source_snippets=_build_snippets(source_files, query=query),
        test_snippets=_build_snippets(test_files, query=query),
        env_snippets=_build_snippets(env_files, max_chars=400),
    )
