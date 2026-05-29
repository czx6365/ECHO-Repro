from pathlib import Path

from echo_repro.bug_spec import extract_bug_spec
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.models import BugSpec
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


def test_retriever_ignores_build_and_dependency_artifacts(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project_file = repo / "package" / "core.py"
    project_file.parent.mkdir()
    project_file.write_text("def version_info():\n    return None\n", encoding="utf-8")
    test_file = repo / "tests" / "test_core.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_version_info():\n    pass\n", encoding="utf-8")
    egg_file = repo / ".eggs" / "packaging-1.0.egg" / "packaging" / "version.py"
    egg_file.parent.mkdir(parents=True)
    egg_file.write_text("class Version:\n    pass\n", encoding="utf-8")
    build_file = repo / "build" / "lib" / "package" / "core.py"
    build_file.parent.mkdir(parents=True)
    build_file.write_text("def generated():\n    pass\n", encoding="utf-8")

    files = collect_files(repo)
    source_files = retrieve_source_files(repo, "version_info Version", top_k=10)
    test_files = retrieve_test_files(repo, "version_info", top_k=10)

    assert project_file in files
    assert test_file in files
    assert egg_file not in files
    assert build_file not in files
    assert project_file in source_files
    assert all(".eggs" not in path.parts and "build" not in path.parts for path in source_files)
    assert test_file in test_files


def test_retriever_snippet_is_centered_on_query_match(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "package.py"
    source.write_text(
        "\n".join([f"irrelevant_header_{index} = {index}" for index in range(120)])
        + "\n\ndef target_symbol():\n    return 'needle behavior'\n",
        encoding="utf-8",
    )
    bug_spec = BugSpec(
        title="needle behavior",
        summary="Need target_symbol behavior",
        current_behavior="target_symbol returns the wrong value",
        expected_behavior="target_symbol should return the right value",
        failure_signature="target_symbol mismatch",
        keywords=["target_symbol", "needle"],
        suspect_symbols=["target_symbol"],
    )

    context = retrieve_context(repo, bug_spec)
    snippet = context.source_snippets[str(source)]

    assert "target_symbol" in snippet
    assert "irrelevant_header_0" not in snippet
