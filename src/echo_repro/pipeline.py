from __future__ import annotations

from pathlib import Path
import sys

from echo_repro.bug_spec import extract_bug_spec
from echo_repro.context_builder import build_concise_context
from echo_repro.executor import run_harness, write_harness
from echo_repro.feedback_loop import run_feedback_loop
from echo_repro.harness_generator import generate_harness
from echo_repro.llm.base import BaseLLMClient
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.llm.openai_client import OpenAICompatibleLLMClient
from echo_repro.models import PipelineResult
from echo_repro.retriever import retrieve_context
from echo_repro.validator import validate_fail_to_pass


def _make_llm_client(use_mock_llm: bool) -> BaseLLMClient:
    if use_mock_llm:
        return MockLLMClient()
    return OpenAICompatibleLLMClient()


def run_pipeline(
    issue_text: str,
    buggy_repo: Path,
    fixed_repo: Path | None = None,
    use_mock_llm: bool = True,
) -> PipelineResult:
    llm_client = _make_llm_client(use_mock_llm)
    bug_spec = extract_bug_spec(issue_text, llm_client)
    retrieved_context = retrieve_context(buggy_repo, bug_spec)
    concise_context = build_concise_context(issue_text, bug_spec, retrieved_context)
    harness_candidate = generate_harness(concise_context, llm_client)

    write_harness(buggy_repo, harness_candidate, filename=harness_candidate.filename)
    buggy_execution = run_harness(
        buggy_repo,
        command=f"{sys.executable} {harness_candidate.filename}",
    )

    fixed_execution = None
    if fixed_repo:
        write_harness(fixed_repo, harness_candidate, filename=harness_candidate.filename)
        fixed_execution = run_harness(
            fixed_repo,
            command=f"{sys.executable} {harness_candidate.filename}",
        )

    validation = validate_fail_to_pass(buggy_execution, fixed_execution)
    return PipelineResult(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        harness_candidate=harness_candidate,
        buggy_execution=buggy_execution,
        fixed_execution=fixed_execution,
        validation=validation,
    )


def run_pipeline_with_feedback_loop(
    issue_text: str,
    buggy_repo: Path,
    fixed_repo: Path | None = None,
    use_mock_llm: bool = True,
    max_attempts: int = 3,
) -> PipelineResult:
    llm_client = _make_llm_client(use_mock_llm)
    bug_spec = extract_bug_spec(issue_text, llm_client)
    retrieved_context = retrieve_context(buggy_repo, bug_spec)
    concise_context = build_concise_context(issue_text, bug_spec, retrieved_context)
    harness_candidate = generate_harness(concise_context, llm_client)
    return run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=harness_candidate,
        llm_client=llm_client,
        buggy_repo=buggy_repo,
        fixed_repo=fixed_repo,
        max_attempts=max_attempts,
    )
