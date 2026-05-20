# ECHO-Repro

ECHO-Repro is a lightweight research prototype for environment-aware bug reproduction harness synthesis.

Given an issue description, optional logs/traces, and a local repository path, the MVP can:

1. Extract a structured `BugSpec`
2. Retrieve relevant source, test, and environment/config files
3. Build a concise reproduction context
4. Generate a minimal executable harness, usually `reproduce.py`
5. Execute the harness on a buggy repo and optionally a fixed repo
6. Repair or strengthen the harness in a feedback loop when needed
7. Validate a Fail-to-Pass outcome

The first version is intentionally simple and runs locally without a real LLM by default.

## Why this exists

Reproducing bugs across repositories often fails because issue text, source context, tests, and environment assumptions are scattered. ECHO-Repro provides a compact pipeline for turning those inputs into a runnable reproduction harness.

## MVP Architecture

```text
Issue text + optional logs
        |
        v
  bug_spec.extract_bug_spec
        |
        v
 retriever.retrieve_context
        |
        v
 context_builder.build_concise_context
        |
        v
 harness_generator.generate_harness
        |
        v
 feedback_loop.run_feedback_loop
        |
        v
 validator.validate_fail_to_pass
```

## Installation

```bash
cd echo-repro
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quick Start

Run the full mock pipeline:

```bash
echo-repro run-one \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --mock
```

Run the same flow with feedback-driven repair:

```bash
echo-repro run-loop \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --max-attempts 3 \
  --mock
```

Inspect the retrieval and concise context only:

```bash
echo-repro inspect-context \
  --issue-file examples/issue_example.txt \
  --repo examples/mock_buggy_repo
```

Show the installed version:

```bash
echo-repro version
```

Download the SWE-bench Lite `test` split into local JSONL:

```bash
python scripts/download_swebench_lite.py
```

This writes all instances to `data/swebench_lite.jsonl`, prints the total number
of instances, and prints the first 5 `instance_id` values.

Prepare one SWE-bench instance into local buggy and fixed repositories:

```bash
echo-repro prepare-swebench \
  --instances-file data/swebench_lite.jsonl \
  --instance-id django__django-12345 \
  --workdir repos/
```

Run one prepared SWE-bench instance through ECHO-Repro:

```bash
echo-repro run-swebench-one \
  --instances-file data/swebench_lite.jsonl \
  --instance-id django__django-12345 \
  --workdir repos/ \
  --mock \
  --max-attempts 3
```

This prepares the buggy/fixed repos, runs the reproduction pipeline, prints a
compact summary, and saves the full JSON artifact to
`outputs/<instance_id>/result.json`.

## Output Artifacts

Each `run-swebench-one` execution writes a research-friendly record under:

```text
outputs/{instance_id}/
  result.json
  concise_context.md
  final_reproduce.py
  attempts.jsonl
  prompts/
  attempts/
```

Key files:

- `result.json`: stable experiment record with `schema_version`
- `concise_context.md`: exact context used for harness generation
- `final_reproduce.py`: final harness after generation or repair
- `attempts.jsonl`: one JSON record per generation/repair attempt
- `prompts/`: prompt snapshots for BugSpec extraction and each harness attempt
- `attempts/`: the harness code emitted at each attempt

Use `result.json` to inspect:

- instance metadata
- run configuration
- retrieved source/test/env files
- final statuses and Fail-to-Pass outcome
- artifact paths for downstream analysis

This layout is designed to support ablation experiments, prompt comparisons,
repair-loop analysis, and offline auditability of what the system actually ran.

## Example Behavior

The included example models a bug where `buggy_module.divide(a, b)` returns `0` when `b == 0`, but the expected behavior is to raise `ZeroDivisionError`.

The generated harness checks that:

- Buggy repo prints `Issue reproduced`
- Fixed repo prints `Issue resolved`

## Module Guide

- `models.py`: typed payloads moving through the pipeline
- `config.py`: environment-based configuration
- `bug_spec.py`: issue-to-`BugSpec` extraction
- `retriever.py`: simple keyword retrieval over source, test, and env files
- `context_builder.py`: compact context assembly for harness generation
- `harness_generator.py`: turns context into an executable Python harness
- `feedback_loop.py`: repairs harnesses or strengthens their oracle using execution feedback
- `repo_manager.py`: clones, checks out, copies, and patches repos for SWE-bench-style instance preparation
- `executor.py`: safe-ish local write and subprocess execution helpers
- `validator.py`: execution classification and Fail-to-Pass validation
- `pipeline.py`: orchestration
- `llm/`: pluggable LLM clients, including `MockLLMClient`
- `utils/`: file and logging helpers

## LLM Configuration

The MVP defaults to `MockLLMClient`, which requires no network access.

Use mock mode explicitly:

```bash
echo-repro run-swebench-one \
  --instances-file data/swebench_lite.jsonl \
  --instance-id django__django-12345 \
  --workdir repos/ \
  --llm mock \
  --max-attempts 3
```

An OpenAI-compatible endpoint can be used via environment variables:

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://api.openai.com/v1"  # optional
export OPENAI_MODEL="gpt-4o-mini"
export OPENAI_TEMPERATURE="0.2"
```

Then run with `--llm openai`:

```bash
echo-repro run-swebench-one \
  --instances-file data/swebench_lite.jsonl \
  --instance-id django__django-12345 \
  --workdir repos/ \
  --llm openai \
  --max-attempts 3
```

`--mock` / `--no-mock` is still supported as a backward-compatible alias, but
`--llm mock|openai` is the preferred interface.

## Safety Note

This project writes and executes generated code inside the target repository directory only.

That is still not strong isolation. Production-grade evaluation should run generated harnesses inside Docker or another sandbox boundary before using this on untrusted repositories.

## Current MVP Limitations

- Retrieval is keyword-based only
- Harness generation is single-candidate
- Feedback repair is still single-candidate and heuristic
- Execution is local subprocess-based, not containerized
- Cross-language repos are not first-class
- The mock LLM is heuristic and example-oriented

## Future Roadmap

- BM25 retrieval
- Embedding retrieval
- Function-level chunking
- Docker sandbox execution
- SWE-bench Lite integration
- Multi-candidate ranking

## How To Connect To SWE-bench Lite Later

The project now includes a lightweight adapter for SWE-bench-style JSONL files in `swebench_adapter.py`.

To create a local JSONL file from the public benchmark dataset, use:

```bash
python scripts/download_swebench_lite.py
```

You can preview one instance without downloading or executing the benchmark:

```bash
echo-repro swebench-preview \
  --instances-file data/instances.jsonl \
  --instance-id django__django-12345
```

What this supports today:

- Reading JSONL instance files
- Selecting a single instance by `instance_id`
- Extracting issue text for ECHO-Repro inputs
- Preparing a small stub with repo metadata
- Preparing local buggy/fixed repos for a single instance from `repo`, `base_commit`, and `patch`

What to add later for real SWE-bench Lite workflows:

- Materialize buggy and fixed repos from benchmark metadata
- Connect `problem_statement` directly into `run-one` or `run-loop`
- Use `patch` and `test_patch` for richer validation and analysis
- Add Docker-based execution before running generated harnesses at scale
