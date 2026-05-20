from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from echo_repro import __version__
from echo_repro.bug_spec import extract_bug_spec
from echo_repro.context_builder import build_concise_context
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.llm.openai_client import OpenAICompatibleLLMClient
from echo_repro.pipeline import run_pipeline, run_pipeline_with_feedback_loop
from echo_repro.repo_manager import prepare_swebench_repos
from echo_repro.retriever import retrieve_context
from echo_repro.swebench_adapter import (
    extract_issue_text,
    get_repo_metadata,
    load_instances,
    prepare_instance_stub,
    select_instance,
)

app = typer.Typer(help="ECHO-Repro research prototype CLI.")
console = Console()


def _load_issue_text(issue_file: Path) -> str:
    return Path(issue_file).read_text(encoding="utf-8")


def _get_client(use_mock: bool):
    return MockLLMClient() if use_mock else OpenAICompatibleLLMClient()


def _print_pipeline_result(result, buggy_repo: Path) -> None:
    console.print(Panel(result.bug_spec.model_dump_json(indent=2), title="BugSpec"))

    table = Table(title="Retrieved Files")
    table.add_column("Kind")
    table.add_column("Files")
    table.add_row("Source", "\n".join(str(path) for path in result.retrieved_context.source_files) or "(none)")
    table.add_row("Test", "\n".join(str(path) for path in result.retrieved_context.test_files) or "(none)")
    table.add_row("Env", "\n".join(str(path) for path in result.retrieved_context.env_files) or "(none)")
    console.print(table)

    if result.attempts:
        attempt_table = Table(title="Feedback Loop Attempts")
        attempt_table.add_column("Attempt")
        attempt_table.add_column("Action")
        attempt_table.add_column("Buggy")
        attempt_table.add_column("Fixed")
        for attempt in result.attempts:
            attempt_table.add_row(
                str(attempt.attempt),
                attempt.action,
                attempt.buggy_status,
                attempt.fixed_status or "-",
            )
        console.print(attempt_table)

    console.print(Panel(str(buggy_repo / result.harness_candidate.filename), title="Generated Harness Path"))
    console.print(Panel(result.buggy_execution.stdout or result.buggy_execution.stderr or "(no output)", title="Buggy Execution Output"))
    if result.fixed_execution:
        console.print(Panel(result.fixed_execution.stdout or result.fixed_execution.stderr or "(no output)", title="Fixed Execution Output"))
    console.print(Panel(result.validation.model_dump_json(indent=2), title="Validation Result"))


@app.command("run-one")
def run_one(
    issue_file: Path = typer.Option(..., exists=True, help="Path to issue text file."),
    buggy_repo: Path = typer.Option(..., exists=True, file_okay=False, help="Buggy repository path."),
    fixed_repo: Path | None = typer.Option(None, exists=True, file_okay=False, help="Fixed repository path."),
    mock: bool = typer.Option(True, "--mock/--no-mock", help="Use the mock LLM client."),
) -> None:
    issue_text = _load_issue_text(issue_file)
    result = run_pipeline(issue_text, buggy_repo=buggy_repo, fixed_repo=fixed_repo, use_mock_llm=mock)
    _print_pipeline_result(result, buggy_repo)


@app.command("run-loop")
def run_loop(
    issue_file: Path = typer.Option(..., exists=True, help="Path to issue text file."),
    buggy_repo: Path = typer.Option(..., exists=True, file_okay=False, help="Buggy repository path."),
    fixed_repo: Path | None = typer.Option(None, exists=True, file_okay=False, help="Fixed repository path."),
    max_attempts: int = typer.Option(3, min=1, help="Maximum feedback loop attempts."),
    mock: bool = typer.Option(True, "--mock/--no-mock", help="Use the mock LLM client."),
) -> None:
    issue_text = _load_issue_text(issue_file)
    result = run_pipeline_with_feedback_loop(
        issue_text,
        buggy_repo=buggy_repo,
        fixed_repo=fixed_repo,
        use_mock_llm=mock,
        max_attempts=max_attempts,
    )
    _print_pipeline_result(result, buggy_repo)


@app.command("inspect-context")
def inspect_context(
    issue_file: Path = typer.Option(..., exists=True, help="Path to issue text file."),
    repo: Path = typer.Option(..., exists=True, file_okay=False, help="Repository path."),
    mock: bool = typer.Option(True, "--mock/--no-mock", help="Use the mock LLM client."),
) -> None:
    issue_text = _load_issue_text(issue_file)
    llm_client = _get_client(mock)
    bug_spec = extract_bug_spec(issue_text, llm_client)
    retrieved_context = retrieve_context(repo, bug_spec)
    concise_context = build_concise_context(issue_text, bug_spec, retrieved_context)
    console.print(Panel(bug_spec.model_dump_json(indent=2), title="BugSpec"))
    console.print(Panel(concise_context, title="Concise Context"))


@app.command("swebench-preview")
def swebench_preview(
    instances_file: Path = typer.Option(..., exists=True, help="Path to SWE-bench-style JSONL instances file."),
    instance_id: str = typer.Option(..., help="SWE-bench instance identifier."),
) -> None:
    instances = load_instances(instances_file)
    instance = select_instance(instances, instance_id)
    stub = prepare_instance_stub(instance)

    console.print(Panel(extract_issue_text(instance) or "(empty issue text)", title="Issue Text"))
    console.print(Panel(json.dumps(get_repo_metadata(instance), indent=2), title="Repo Metadata"))
    console.print(Panel(json.dumps(stub, indent=2), title="Prepared Stub"))


@app.command("prepare-swebench")
def prepare_swebench(
    instances_file: Path = typer.Option(..., exists=True, help="Path to SWE-bench-style JSONL instances file."),
    instance_id: str = typer.Option(..., help="SWE-bench instance identifier."),
    workdir: Path = typer.Option(..., help="Directory for prepared buggy and fixed repos."),
) -> None:
    instances = load_instances(instances_file)
    instance = select_instance(instances, instance_id)
    prepared = prepare_swebench_repos(instance, workdir=workdir)

    console.print(
        Panel(
            json.dumps(
                {
                    "instance_id": prepared.instance_id,
                    "repo": prepared.repo,
                    "base_commit": prepared.base_commit,
                    "buggy_repo": str(prepared.buggy_repo),
                    "fixed_repo": str(prepared.fixed_repo),
                    "patch_applied": prepared.patch_applied,
                },
                indent=2,
            ),
            title="Prepared SWE-bench Repos",
        )
    )


@app.command("version")
def version() -> None:
    console.print(__version__)


if __name__ == "__main__":
    app()
