from pathlib import Path

from echo_ci.dependency_diagnosis import diagnose_missing_dependency


def test_diagnoses_target_import_and_optional_dependency(tmp_path: Path):
    target = tmp_path / "libs" / "agno" / "tests" / "test_firecrawl.py"
    target.parent.mkdir(parents=True)
    target.write_text("from firecrawl import FirecrawlApp\n", encoding="utf-8")
    pyproject = tmp_path / "libs" / "agno" / "pyproject.toml"
    pyproject.write_text(
        '[project.optional-dependencies]\nfirecrawl = ["firecrawl-py"]\n',
        encoding="utf-8",
    )
    preflight = {
        "targets": [
            {
                "target": "libs/agno/tests/test_firecrawl.py",
                "fixed_resolved_path": "libs/agno/tests/test_firecrawl.py",
            }
        ]
    }

    diagnosis = diagnose_missing_dependency(
        repo_path=tmp_path,
        output_text="ModuleNotFoundError: No module named 'firecrawl'",
        target_preflight=preflight,
        workflow_signal={"setup_commands": ["pip install -r requirements.txt"]},
    )

    assert diagnosis["missing_module"] == "firecrawl"
    assert diagnosis["imported_by"] == ["libs/agno/tests/test_firecrawl.py"]
    assert diagnosis["declared_in"][0]["source"] == "project_optional_extras"
    assert diagnosis["recommended_minimal_install"] == "python -m pip install firecrawl-py"
    assert diagnosis["diagnosis_only"] is True


def test_prefers_target_local_dependency_over_unrelated_tool_config(tmp_path: Path):
    unrelated = tmp_path / "aaa" / "pyproject.toml"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('[tool.mypy]\nplugins = ["numpy.typing.mypy_plugin"]\n', encoding="utf-8")
    target = tmp_path / "framework" / "tests" / "test_array.py"
    target.parent.mkdir(parents=True)
    target.write_text("import numpy\n", encoding="utf-8")
    pyproject = tmp_path / "framework" / "pyproject.toml"
    pyproject.write_text('[tool.poetry.dependencies]\nnumpy = ">=1.26"\n', encoding="utf-8")

    diagnosis = diagnose_missing_dependency(
        repo_path=tmp_path,
        output_text="ModuleNotFoundError: No module named 'numpy'",
        target_preflight={
            "targets": [{"fixed_resolved_path": "framework/tests/test_array.py"}]
        },
    )

    assert diagnosis["declared_in"][0]["path"] == "framework/pyproject.toml"
    assert diagnosis["recommended_minimal_install"] == "python -m pip install numpy"
