from __future__ import annotations

from pathlib import Path
import sys

from echo_repro.bug_spec import extract_bug_spec
from echo_repro.context_builder import build_concise_context
from echo_repro.environment import EnvironmentRepairManager
from echo_repro.executor import run_harness, write_harness
from echo_repro.feedback_loop import run_feedback_loop
from echo_repro.harness_generator import generate_harness
from echo_repro.llm.anthropic_client import AnthropicCompatibleLLMClient
from echo_repro.llm.base import BaseLLMClient
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.llm.openai_client import OpenAICompatibleLLMClient
from echo_repro.models import LLMCallMetadata, PipelineResult
from echo_repro.retriever import retrieve_context
from echo_repro.validator import validate_fail_to_pass
from echo_repro.config import get_llm_settings


def _make_llm_client(use_mock_llm: bool = True, llm_provider: str | None = None) -> BaseLLMClient:
    provider = llm_provider or ("mock" if use_mock_llm else get_llm_settings().llm_provider)
    if provider == "mock":
        return MockLLMClient()
    if provider == "openai":
        return OpenAICompatibleLLMClient()
    if provider == "anthropic":
        return AnthropicCompatibleLLMClient()
    raise ValueError(f"Unsupported LLM provider: {provider}")


def _client_model_name(llm_client: BaseLLMClient) -> str:
    settings = getattr(llm_client, "settings", get_llm_settings())
    return str(getattr(llm_client, "model_name", getattr(settings, "model", "")))


def _client_temperature(llm_client: BaseLLMClient) -> float | None:
    settings = getattr(llm_client, "settings", get_llm_settings())
    return getattr(llm_client, "temperature", getattr(settings, "temperature", None))


def run_pipeline(
    issue_text: str,
    buggy_repo: Path,
    fixed_repo: Path | None = None,
    use_mock_llm: bool = True,
    llm_provider: str | None = None,
) -> PipelineResult:
    llm_client = _make_llm_client(use_mock_llm, llm_provider=llm_provider)
    bug_spec = extract_bug_spec(issue_text, llm_client)
    bug_spec_prompt = llm_client.last_prompt
    bug_spec_llm_metadata = llm_client.last_call_metadata or LLMCallMetadata()
    retrieved_context = retrieve_context(buggy_repo, bug_spec)
    concise_context = build_concise_context(issue_text, bug_spec, retrieved_context)
    harness_candidate = generate_harness(concise_context, llm_client)
    initial_harness_prompt = llm_client.last_prompt
    initial_harness_llm_metadata = llm_client.last_call_metadata or LLMCallMetadata()

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
        llm_provider=llm_provider or ("mock" if use_mock_llm else "openai"),
        llm_model=_client_model_name(llm_client),
        llm_temperature=_client_temperature(llm_client),
        bug_spec_prompt=bug_spec_prompt,
        bug_spec_llm_metadata=bug_spec_llm_metadata,
        initial_harness_prompt=initial_harness_prompt,
        initial_harness_llm_metadata=initial_harness_llm_metadata,
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
    llm_provider: str | None = None,
    environment_repair_manager: EnvironmentRepairManager | None = None,
) -> PipelineResult:
    llm_client = _make_llm_client(use_mock_llm, llm_provider=llm_provider)
    bug_spec = extract_bug_spec(issue_text, llm_client)
    bug_spec_prompt = llm_client.last_prompt
    bug_spec_llm_metadata = llm_client.last_call_metadata or LLMCallMetadata()
    retrieved_context = retrieve_context(buggy_repo, bug_spec)
    concise_context = build_concise_context(issue_text, bug_spec, retrieved_context)
    harness_candidate = generate_harness(concise_context, llm_client)
    initial_harness_prompt = llm_client.last_prompt
    initial_harness_llm_metadata = llm_client.last_call_metadata or LLMCallMetadata()
    return run_feedback_loop(
        bug_spec=bug_spec,
        retrieved_context=retrieved_context,
        concise_context=concise_context,
        initial_harness=harness_candidate,
        llm_client=llm_client,
        buggy_repo=buggy_repo,
        fixed_repo=fixed_repo,
        max_attempts=max_attempts,
        initial_prompt_text=initial_harness_prompt,
        initial_llm_metadata=initial_harness_llm_metadata,
        llm_provider=llm_provider or ("mock" if use_mock_llm else "openai"),
        llm_model=_client_model_name(llm_client),
        llm_temperature=_client_temperature(llm_client),
        bug_spec_prompt=bug_spec_prompt,
        bug_spec_llm_metadata=bug_spec_llm_metadata,
        environment_repair_manager=environment_repair_manager,
    )
