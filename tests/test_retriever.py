from pathlib import Path

from echo_repro.bug_spec import extract_bug_spec
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.retriever import collect_files, retrieve_context, retrieve_env_files, retrieve_source_files, retrieve_test_files


def test_retriever_finds_source_test_and_env_files():
    repo = Path("examples/mock_buggy_repo")
    issue_text = Path("examples/issue_example.txt").read_text(encoding="utf-8")
    bug_spec = extract_bug_spec(issue_text, MockLLMClient())

    files = collect_files(repo)
    assert any(path.name == "buggy_module.py" for path in files)

    source_files = retrieve_source_files(repo, "divide ZeroDivisionError")
    test_files = retrieve_test_files(repo, "divide ZeroDivisionError")
    env_files = retrieve_env_files(repo)
    context = retrieve_context(repo, bug_spec)

    assert any(path.name == "buggy_module.py" for path in source_files)
    assert any(path.name == "test_buggy_module.py" for path in test_files)
    assert any(path.name == "requirements.txt" for path in env_files)
    assert context.source_snippets
    assert context.test_snippets
    assert context.env_snippets

