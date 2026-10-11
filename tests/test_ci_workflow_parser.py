from echo_ci.workflow_parser import extract_workflow_execution_signal


def test_extracts_command_workdir_environment_config_and_setup():
    workflow = """
jobs:
  lint:
    defaults:
      run:
        working-directory: libs/agno
    env:
      PYTHONWARNINGS: default
    steps:
      - name: Install
        run: pip install --no-deps -r requirements.txt
      - name: Lint
        run: ruff check . --config pyproject.toml
"""
    signal = extract_workflow_execution_signal(workflow, command="ruff check .")

    assert signal["command"] == "ruff check . --config pyproject.toml"
    assert signal["working_directory"] == "libs/agno"
    assert signal["environment"] == {"PYTHONWARNINGS": "default"}
    assert signal["config_files"] == ["pyproject.toml"]
    assert signal["setup_commands"] == ["pip install --no-deps -r requirements.txt"]
    assert signal["command_from_workflow"] is True


def test_infers_workdir_from_nearby_setup_cd_for_wrapped_test_command():
    workflow = """
steps:
  - name: Install dependencies
    run: |
      cd framework
      python -m poetry install --all-extras
  - name: Lint + Test (pytest)
    run: ./framework/dev/test.sh
"""
    signal = extract_workflow_execution_signal(
        workflow,
        command="pytest py/flwr/supernode/servicer/test.py",
    )

    assert signal["command"] == "./framework/dev/test.sh"
    assert signal["working_directory"] == "framework"


def test_does_not_treat_defaults_run_mapping_as_a_command():
    workflow = """
jobs:
  run:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: libs/agno
    steps:
      - name: Install
        run: |
          pip install ruff
      - name: Check
        run: |
          ruff format .
"""
    signal = extract_workflow_execution_signal(workflow, command="ruff format .")

    assert signal["command"] == "ruff format ."
    assert signal["working_directory"] == "libs/agno"


def test_uses_strongest_evidence_line_for_multiline_query():
    workflow = """
steps:
  - name: Install pre-commit
    run: pre-commit install
  - name: Pre-commit starts
    run: |
      pre-commit run --all-files > pre-commit.log 2>&1 || true
      cat pre-commit.log
      if grep -q Failed pre-commit.log; then exit 1; fi
"""
    signal = extract_workflow_execution_signal(
        workflow,
        command="flake8 file.py\npre-commit run --all-files\nblack Failed",
    )

    assert signal["command"].startswith("pre-commit run --all-files")
    assert signal["command_from_workflow"] is True


def test_prefers_explicit_failed_command_over_broad_later_log_match():
    workflow = """
steps:
  - name: Install pre-commit
    run: pre-commit install
  - name: Pre-commit starts
    run: pre-commit run --all-files
"""
    signal = extract_workflow_execution_signal(
        workflow,
        command=(
            "pre-commit run --all-files\n"
            "flake8 file.py\n"
            "the log mentions both pre-commit install and pre-commit run --all-files"
        ),
    )

    assert signal["command"] == "pre-commit run --all-files"


def test_repo_root_workdir_and_coverage_output_are_not_required_inputs():
    workflow = """
steps:
  - name: Test
    working-directory: .
    run: |
      source .venv/bin/activate
      python -m pytest --cov-report=json:coverage-agno.json ./libs/agno/tests/unit
"""
    signal = extract_workflow_execution_signal(workflow, command="source .venv/bin/activate")

    assert signal["working_directory"] == ""
    assert signal["config_files"] == []
    assert "coverage-agno.json" not in signal["target_scope"]


def test_github_matrix_expression_is_not_a_python_target():
    workflow = """
steps:
  - name: Install
    run: pipenv install --dev --python=${{ matrix.python-version }}
"""
    signal = extract_workflow_execution_signal(workflow, command="pipenv install --dev")

    assert signal["target_scope"] == []
