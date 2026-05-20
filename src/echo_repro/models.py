from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class BugSpec(BaseModel):
    title: str
    summary: str
    current_behavior: str
    expected_behavior: str
    failure_signature: str
    reproduction_hint: str = ""
    keywords: list[str] = Field(default_factory=list)
    suspect_symbols: list[str] = Field(default_factory=list)


class RetrievedContext(BaseModel):
    repo_path: Path
    source_files: list[Path] = Field(default_factory=list)
    test_files: list[Path] = Field(default_factory=list)
    env_files: list[Path] = Field(default_factory=list)
    source_snippets: dict[str, str] = Field(default_factory=dict)
    test_snippets: dict[str, str] = Field(default_factory=dict)
    env_snippets: dict[str, str] = Field(default_factory=dict)


class HarnessCandidate(BaseModel):
    filename: str = "reproduce.py"
    code: str
    rationale: str = ""


ExecutionStatus = Literal[
    "reproduced",
    "resolved",
    "syntax_error",
    "import_error",
    "file_error",
    "timeout",
    "other",
]


class ExecutionResult(BaseModel):
    repo_path: Path
    command: str
    returncode: int | None
    stdout: str
    stderr: str
    harness_path: Path | None = None
    timed_out: bool = False


class LLMCallMetadata(BaseModel):
    provider: str = ""
    model: str = ""
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    raw_usage: dict = Field(default_factory=dict)


class ValidationResult(BaseModel):
    success: bool
    buggy_status: ExecutionStatus
    fixed_status: ExecutionStatus | None = None
    summary: str


class PreparedRepos(BaseModel):
    instance_id: str
    repo: str
    base_commit: str
    buggy_repo: Path
    fixed_repo: Path
    patch_applied: bool = False


class FeedbackLoopAttempt(BaseModel):
    attempt: int
    action: str
    note: str = ""
    prompt_text: str = ""
    llm_metadata: LLMCallMetadata = Field(default_factory=LLMCallMetadata)
    harness_candidate: HarnessCandidate
    buggy_execution: ExecutionResult
    buggy_status: ExecutionStatus
    fixed_execution: ExecutionResult | None = None
    fixed_status: ExecutionStatus | None = None


class PipelineResult(BaseModel):
    llm_provider: str = ""
    llm_model: str = ""
    llm_temperature: float | None = None
    bug_spec_prompt: str = ""
    bug_spec_llm_metadata: LLMCallMetadata = Field(default_factory=LLMCallMetadata)
    initial_harness_prompt: str = ""
    initial_harness_llm_metadata: LLMCallMetadata = Field(default_factory=LLMCallMetadata)
    bug_spec: BugSpec
    retrieved_context: RetrievedContext
    concise_context: str
    harness_candidate: HarnessCandidate
    buggy_execution: ExecutionResult
    fixed_execution: ExecutionResult | None = None
    validation: ValidationResult
    attempts: list[FeedbackLoopAttempt] = Field(default_factory=list)
