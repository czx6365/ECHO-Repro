from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from echo_repro import __version__
from echo_repro.bug_spec import extract_bug_spec
from echo_repro.context_builder import build_concise_context
from echo_repro.environment import EnvironmentProfileManager, EnvironmentRepairManager
from echo_repro.llm.anthropic_client import AnthropicCompatibleLLMClient
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.llm.openai_client import OpenAICompatibleLLMClient
from echo_repro.pipeline import run_pipeline, run_pipeline_with_feedback_loop
from echo_repro.repo_manager import RepoPreparationError, prepare_swebench_repos
from echo_repro.result_writer import write_experiment_record, write_preparation_failure_record
from echo_repro.retriever import retrieve_context
from echo_repro.swebench_adapter import (
    extract_issue_text,
    get_repo_metadata,
    load_instances,
    prepare_instance_stub,
    select_instance,
)
from echo_repro.validator import classify_execution

app = typer.Typer(help="ECHO-Repro research prototype CLI.")
console = Console()


def _load_issue_text(issue_file: Path) -> str:
    return Path(issue_file).read_text(encoding="utf-8")


def _resolve_llm_provider(llm: str, mock: bool | None) -> str:
    if mock is True:
        return "mock"
    if mock is False:
        return "openai"
    return llm


def _get_client_for_provider(provider: str):
    if provider == "mock":
        return MockLLMClient()
    if provider == "openai":
        return OpenAICompatibleLLMClient()
    if provider == "anthropic":
        return AnthropicCompatibleLLMClient()
    raise ValueError(f"Unsupported LLM provider: {provider}")


def _raise_cli_error(exc: ValueError) -> None:
    console.print(f"LLM configuration error: {exc}")
    raise typer.Exit(code=2)


def _raise_prepare_error(exc: RepoPreparationError) -> None:
    console.print(f"Repository preparation error: {exc}")
    raise typer.Exit(code=2)


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


def _print_swebench_run_result(instance: dict, prepared, result, output_path: Path) -> None:
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
                    "repo_validated": prepared.repo_validated,
                    "buggy_commit": prepared.buggy_commit,
                    "fixed_commit": prepared.fixed_commit,
                    "fixed_diff_stat": prepared.fixed_diff_stat,
                    "repo_cache_path": str(prepared.repo_cache_path) if prepared.repo_cache_path else "",
                    "output_json": str(output_path),
                },
                indent=2,
            ),
            title="SWE-bench Run",
        )
    )

    table = Table(title="Retrieved Files")
    table.add_column("Kind")
    table.add_column("Files")
    table.add_row("Source", "\n".join(str(path) for path in result.retrieved_context.source_files) or "(none)")
    table.add_row("Test", "\n".join(str(path) for path in result.retrieved_context.test_files) or "(none)")
    table.add_row("Env", "\n".join(str(path) for path in result.retrieved_context.env_files) or "(none)")
    console.print(table)

    console.print(Panel(str(prepared.buggy_repo / result.harness_candidate.filename), title="Generated Harness Path"))
    if result.environment_profile:
        console.print(
            Panel(
                result.environment_profile.model_dump_json(indent=2),
                title="Environment Profile",
            )
        )
    console.print(
        Panel(
            json.dumps(
                {
                    "buggy_execution_status": classify_execution(result.buggy_execution),
                    "fixed_execution_status": classify_execution(result.fixed_execution) if result.fixed_execution else None,
                    "validation": result.validation.model_dump(mode="json"),
                },
                indent=2,
            ),
            title="Execution Summary",
        )
    )

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


@app.command("run-one")
def run_one(
    issue_file: Path = typer.Option(..., exists=True, help="Path to issue text file."),
    buggy_repo: Path = typer.Option(..., exists=True, file_okay=False, help="Buggy repository path."),
    fixed_repo: Path | None = typer.Option(None, exists=True, file_okay=False, help="Fixed repository path."),
    llm: str = typer.Option("mock", "--llm", help="LLM provider: mock, openai, or anthropic."),
    mock: bool | None = typer.Option(None, "--mock/--no-mock", help="Backward-compatible alias for --llm."),
) -> None:
    issue_text = _load_issue_text(issue_file)
    provider = _resolve_llm_provider(llm, mock)
    try:
        result = run_pipeline(
            issue_text,
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            use_mock_llm=provider == "mock",
            llm_provider=provider,
        )
    except ValueError as exc:
        _raise_cli_error(exc)
    _print_pipeline_result(result, buggy_repo)


@app.command("run-loop")
def run_loop(
    issue_file: Path = typer.Option(..., exists=True, help="Path to issue text file."),
    buggy_repo: Path = typer.Option(..., exists=True, file_okay=False, help="Buggy repository path."),
    fixed_repo: Path | None = typer.Option(None, exists=True, file_okay=False, help="Fixed repository path."),
    max_attempts: int = typer.Option(3, min=1, help="Maximum feedback loop attempts."),
    llm: str = typer.Option("mock", "--llm", help="LLM provider: mock, openai, or anthropic."),
    mock: bool | None = typer.Option(None, "--mock/--no-mock", help="Backward-compatible alias for --llm."),
) -> None:
    issue_text = _load_issue_text(issue_file)
    provider = _resolve_llm_provider(llm, mock)
    try:
        result = run_pipeline_with_feedback_loop(
            issue_text,
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            use_mock_llm=provider == "mock",
            max_attempts=max_attempts,
            llm_provider=provider,
        )
    except ValueError as exc:
        _raise_cli_error(exc)
    _print_pipeline_result(result, buggy_repo)


@app.command("inspect-context")
def inspect_context(
    issue_file: Path = typer.Option(..., exists=True, help="Path to issue text file."),
    repo: Path = typer.Option(..., exists=True, file_okay=False, help="Repository path."),
    llm: str = typer.Option("mock", "--llm", help="LLM provider: mock, openai, or anthropic."),
    mock: bool | None = typer.Option(None, "--mock/--no-mock", help="Backward-compatible alias for --llm."),
) -> None:
    issue_text = _load_issue_text(issue_file)
    provider = _resolve_llm_provider(llm, mock)
    try:
        llm_client = _get_client_for_provider(provider)
    except ValueError as exc:
        _raise_cli_error(exc)
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
    cache_dir: Path = typer.Option(Path("repos/cache"), help="Directory for cached repository mirrors."),
) -> None:
    instances = load_instances(instances_file)
    instance = select_instance(instances, instance_id)
    try:
        prepared = prepare_swebench_repos(instance, workdir=workdir, cache_dir=cache_dir)
    except RepoPreparationError as exc:
        write_preparation_failure_record(
            instance=instance,
            error=exc,
            workdir=workdir,
            cache_dir=cache_dir,
        )
        _raise_prepare_error(exc)

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
                    "repo_validated": prepared.repo_validated,
                    "buggy_commit": prepared.buggy_commit,
                    "fixed_commit": prepared.fixed_commit,
                    "fixed_diff_stat": prepared.fixed_diff_stat,
                    "repo_cache_path": str(prepared.repo_cache_path) if prepared.repo_cache_path else "",
                },
                indent=2,
            ),
            title="Prepared SWE-bench Repos",
        )
    )


@app.command("run-swebench-one")
def run_swebench_one(
    instances_file: Path = typer.Option(..., exists=True, help="Path to SWE-bench-style JSONL instances file."),
    instance_id: str = typer.Option(..., help="SWE-bench instance identifier."),
    workdir: Path = typer.Option(..., help="Directory for prepared buggy and fixed repos."),
    llm: str = typer.Option("mock", "--llm", help="LLM provider: mock, openai, or anthropic."),
    mock: bool | None = typer.Option(None, "--mock/--no-mock", help="Backward-compatible alias for --llm."),
    max_attempts: int | None = typer.Option(None, min=1, help="Use the feedback loop with this many attempts."),
    env_root: Path = typer.Option(Path("envs"), help="Directory for cached per-repository virtual environments."),
    env_python: Path | None = typer.Option(None, "--env-python", exists=True, dir_okay=False, help="Python interpreter used to create environment profiles."),
    env_profile: bool = typer.Option(True, "--env-profile/--no-env-profile", help="Detect and reuse repo-level environment profiles."),
    allow_env_install: bool = typer.Option(False, "--allow-env-install/--no-allow-env-install", help="Allow creating/installing cached repo environments."),
    cache_dir: Path = typer.Option(Path("repos/cache"), help="Directory for cached repository mirrors."),
    output_root: Path = typer.Option(Path("outputs"), help="Directory for experiment result artifacts."),
) -> None:
    instances = load_instances(instances_file)
    instance = select_instance(instances, instance_id)
    try:
        prepared = prepare_swebench_repos(instance, workdir=workdir, cache_dir=cache_dir)
    except RepoPreparationError as exc:
        write_preparation_failure_record(
            instance=instance,
            error=exc,
            workdir=workdir,
            cache_dir=cache_dir,
            output_root=output_root,
        )
        _raise_prepare_error(exc)
    issue_text = extract_issue_text(instance)
    provider = _resolve_llm_provider(llm, mock)

    try:
        if max_attempts is not None:
            environment_profile_manager = (
                EnvironmentProfileManager(
                    repo=prepared.repo,
                    repo_path=prepared.buggy_repo,
                    extra_repo_paths=[prepared.fixed_repo],
                    env_root=env_root,
                    base_python=env_python or Path(sys.executable),
                    allow_install=allow_env_install,
                )
                if env_profile
                else None
            )
            result = run_pipeline_with_feedback_loop(
                issue_text,
                buggy_repo=prepared.buggy_repo,
                fixed_repo=prepared.fixed_repo,
                use_mock_llm=provider == "mock",
                max_attempts=max_attempts,
                llm_provider=provider,
                environment_repair_manager=EnvironmentRepairManager(
                    repo_slug=prepared.repo,
                    env_root=env_root,
                ),
                environment_profile_manager=environment_profile_manager,
            )
        else:
            result = run_pipeline(
                issue_text,
                buggy_repo=prepared.buggy_repo,
                fixed_repo=prepared.fixed_repo,
                use_mock_llm=provider == "mock",
                llm_provider=provider,
            )
    except ValueError as exc:
        _raise_cli_error(exc)

    output_path = write_experiment_record(
        instance=instance,
        prepared=prepared,
        result=result,
        max_attempts=max_attempts,
        output_root=output_root,
    )
    _print_swebench_run_result(instance, prepared, result, output_path)


@app.command("version")
def version() -> None:
    console.print(__version__)


if __name__ == "__main__":
    app()
