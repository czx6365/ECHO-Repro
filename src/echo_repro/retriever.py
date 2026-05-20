from __future__ import annotations

from pathlib import Path

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


def collect_files(repo_path: Path) -> list[Path]:
    repo_path = Path(repo_path)
    files = []
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        if path.name == "reproduce.py":
            continue
        if any(part.startswith(".pytest_cache") or part == "__pycache__" for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def _score_file(path: Path, query: str, preferred_kind: str) -> int:
    query_tokens = {token for token in query.lower().split() if token}
    path_text = str(path).lower()
    content = read_text_safe(path, max_chars=3_000).lower()
    haystack = f"{path_text}\n{content}"
    score = sum(3 for token in query_tokens if token in haystack)

    if preferred_kind == "source":
        if path.suffix == ".py":
            score += 5
        if "test" in path.name.lower():
            score -= 3
    elif preferred_kind == "test":
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            score += 8
        if path.name in TEST_FILE_NAMES or path.name == "conftest.py":
            score += 7
        if "test" in path.parts or "tests" in path.parts:
            score += 4

    return score


def _top_files(files: list[Path], query: str, preferred_kind: str, top_k: int) -> list[Path]:
    ranked = sorted(
        files,
        key=lambda path: (_score_file(path, query, preferred_kind), path.suffix == ".py"),
        reverse=True,
    )
    return [path for path in ranked if _score_file(path, query, preferred_kind) > 0][:top_k]


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


def _build_snippets(paths: list[Path], max_chars: int = 700) -> dict[str, str]:
    return {str(path): read_text_safe(path, max_chars=max_chars) for path in paths}


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
        source_snippets=_build_snippets(source_files),
        test_snippets=_build_snippets(test_files),
        env_snippets=_build_snippets(env_files, max_chars=400),
    )
