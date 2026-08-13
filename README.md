# ECHO-Repro

**Environment-aware LLM Agent for Reproducible Bug Reproduction and Fail-to-Pass CI Validation**

ECHO-Repro is a research prototype for synthesizing a **minimal executable reproduction harness** from issue descriptions, logs, traces, repository context, and environment files.

The goal is not to directly repair a bug. Instead, ECHO-Repro first asks a stricter question:

> Can an LLM agent generate a reproduction script that fails for the *right reason* on the buggy version and passes on the fixed version?

This makes the generated harness useful as a reliable oracle for downstream automated debugging, patch validation, and software-engineering evaluation.

---

## Research Question

Cross-repository bug reproduction is difficult because three sources of uncertainty are entangled:

1. **Incomplete context** — issue descriptions often omit code paths, fixtures, test conventions, or configuration details.
2. **Unreliable environments** — missing dependencies, incompatible versions, or incorrect commits can make a valid harness appear broken.
3. **Weak validation** — a script that crashes is not necessarily a successful reproduction; failures such as `ImportError`, `SyntaxError`, or artificial assertions are false positives.

ECHO-Repro treats bug reproduction as an **environment-aware synthesis and validation problem**, rather than a one-shot code-generation task.

## Core Contributions

- **Structured bug specification.** Extracts a compact `BugSpec` from raw issue text to make retrieval and generation less dependent on noisy long-form descriptions.
- **Environment-aware hierarchical retrieval.** Separates source, test, and environment evidence instead of retrieving code alone.
- **Executable harness synthesis.** Generates a minimal `reproduce.py` using mock, OpenAI-compatible, or Anthropic-compatible LLM backends.
- **Failure-type-aware feedback loop.** Distinguishes harness, oracle, dependency, environment, repository, and patch failures before deciding whether the LLM should revise the script.
- **Environment repair.** Builds or reuses per-repository virtual environments and performs minimal dependency repair when needed.
- **Fail-to-Pass validation.** Accepts a reproduction only when the buggy repository reproduces the issue and the fixed repository resolves it.
- **Reproducible experiment artifacts.** Stores prompts, attempts, final harnesses, execution outputs, and structured `result.json` files for later analysis.

## System Overview

```text
Issue / Log / Trace / Repository / Environment Files
                         |
                         v
              [1] BugSpec Extraction
                         |
                         v
       [2] Environment-aware Retrieval
          /              |              \
      Source            Tests         Environment
          \              |              /
                         v
         [3] Concise Context Construction
                         |
                         v
              [4] Harness Synthesis
                         |
                         v
              [5] Execute on Buggy Repo
                         |
                         v
             Failure Classification
         /        |        |        \
   Harness     Oracle   Dependency  Environment
      |           |        |            |
      v           v        v            v
   Repair     Strengthen  Env Repair   Record
         \        |        /
                 v
          [6] Execute on Fixed Repo
                 |
                 v
          Fail-to-Pass Validation
                 |
                 v
      Minimal Validated Reproduction Harness
```

## Success Criterion

A harness is accepted only when both conditions hold:

```text
buggy repo -> Issue reproduced
fixed repo -> Issue resolved
```

This prevents the system from counting generic crashes or environment failures as successful reproductions.

## Current Capabilities

Given an issue and buggy/fixed repositories, the current prototype can:

- extract a structured `BugSpec` with current behavior, expected behavior, failure signatures, keywords, and suspect symbols;
- retrieve relevant source files, tests, fixtures, and environment configuration;
- build a concise reproduction context for the LLM;
- synthesize and clean an executable `reproduce.py`;
- execute the harness in both buggy and fixed repositories;
- classify failures into `repo_error`, `patch_error`, `dependency_error`, `environment_error`, `harness_error`, and `oracle_error`;
- repair recoverable harness/oracle failures through execution feedback;
- create or reuse cached repository environments when dependencies are missing;
- prepare SWE-bench Lite instances with repository caching and shallow fetch;
- save stable experiment records for auditing and later analysis.

## Core Modules

| Module | Role |
| --- | --- |
| `bug_spec.py` | Converts issue text into structured bug specifications. |
| `retriever.py` | Retrieves source, test, and environment context. |
| `context_builder.py` | Builds the concise context sent to the LLM. |
| `harness_generator.py` | Synthesizes candidate reproduction harnesses. |
| `executor.py` | Executes harnesses and captures stdout, stderr, return codes, and timeouts. |
| `validator.py` | Classifies failures and performs Fail-to-Pass validation. |
| `feedback_loop.py` | Repairs harnesses, strengthens oracles, or triggers environment repair. |
| `environment.py` | Profiles repositories, manages cached venvs, and repairs dependencies. |
| `repo_manager.py` | Prepares SWE-bench repositories and validates commits/patches. |
| `result_writer.py` | Writes prompts, attempts, final harnesses, and structured results. |
| `swebench_adapter.py` | Loads SWE-bench Lite metadata and instances. |

## Repository Structure

```text
ECHO-Repro/
├── README.md
├── pyproject.toml
├── src/echo_repro/
│   ├── cli.py
│   ├── config.py
│   ├── models.py
│   ├── pipeline.py
│   ├── bug_spec.py
│   ├── retriever.py
│   ├── context_builder.py
│   ├── prompts.py
│   ├── harness_generator.py
│   ├── code_cleaner.py
│   ├── executor.py
│   ├── validator.py
│   ├── feedback_loop.py
│   ├── environment.py
│   ├── repo_manager.py
│   ├── result_writer.py
│   ├── swebench_adapter.py
│   └── llm/
├── scripts/
├── examples/
└── tests/
```

## LLM Backends

| Provider | CLI value | Intended use |
| --- | --- | --- |
| Mock | `--llm mock` | Local smoke tests and unit tests without API access. |
| OpenAI-compatible | `--llm openai` | Chat Completions-compatible models. |
| Anthropic-compatible | `--llm anthropic` | Anthropic Messages-compatible endpoints, including compatible third-party services. |

## Installation

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run tests:

```bash
.venv/bin/python -m pytest
```

## Quick Start

Run the built-in mock example:

```bash
echo-repro run-one \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --llm mock
```

Run with a feedback loop:

```bash
echo-repro run-loop \
  --issue-file examples/issue_example.txt \
  --buggy-repo examples/mock_buggy_repo \
  --fixed-repo examples/mock_fixed_repo \
  --llm mock \
  --max-attempts 3
```

Inspect the retrieved context:

```bash
echo-repro inspect-context \
  --issue-file examples/issue_example.txt \
  --repo examples/mock_buggy_repo \
  --llm mock
```

## SWE-bench Lite Workflow

Download benchmark metadata:

```bash
python scripts/download_swebench_lite.py
```

Create a small experiment subset:

```bash
python scripts/create_swebench_sample.py \
  --instances-file data/swebench_lite.jsonl \
  --output-file data/swebench_lite_small.jsonl \
  --sample-size 20
```

Preview an instance:

```bash
echo-repro swebench-preview \
  --instances-file data/swebench_lite.jsonl \
  --instance-id astropy__astropy-12907
```

Prepare buggy/fixed repositories:

```bash
echo-repro prepare-swebench \
  --instances-file data/swebench_lite.jsonl \
  --instance-id astropy__astropy-12907 \
  --workdir repos \
  --cache-dir repos/cache
```

Run one SWE-bench instance:

```bash
echo-repro run-swebench-one \
  --instances-file data/swebench_lite.jsonl \
  --instance-id astropy__astropy-12907 \
  --workdir repos \
  --cache-dir repos/cache \
  --env-root envs \
  --env-python /path/to/python3.10 \
  --env-profile \
  --output-root outputs \
  --llm mock \
  --max-attempts 3
```

Summarize experiment outputs:

```bash
python scripts/summarize_swebench_experiment.py \
  --instances-file data/swebench_lite_small.jsonl \
  --outputs-dir outputs \
  --output-md outputs/swebench_lite_small_summary.md \
  --output-csv outputs/swebench_lite_small_summary.csv
```

## Reproducibility Design

ECHO-Repro records more than the final generated script. Each experiment can preserve:

- the extracted bug specification;
- retrieved context;
- prompts sent to the model;
- each synthesis/repair attempt;
- buggy and fixed execution outputs;
- the final harness;
- failure categories and validation decisions;
- structured experiment metadata in `result.json`.

This design is intended to make failure analysis and ablation studies auditable instead of relying on manually inspected LLM outputs.

## Repository Cache and Environment Repair

Repeated benchmark runs reuse repository caches under paths such as:

```text
repos/cache/astropy__astropy/
repos/cache/django__django/
repos/cache/sympy__sympy/
```

When a required commit is missing, ECHO-Repro can fetch it shallowly rather than recloning the entire repository.

Environment installation is intentionally conservative. By default, the system profiles the repository but does not automatically install a full environment. Explicitly enable installation when needed to avoid expensive benchmark-wide setup and unnecessary disk usage.

## Research Status

ECHO-Repro is an active research prototype. The current emphasis is on:

- improving retrieval quality and context compression;
- distinguishing environment failures from genuine reproduction failures;
- strengthening reproduction oracles;
- evaluating reproduction success under controlled benchmark settings;
- using validated reproducers as downstream patch-validation signals.

## Safety and Secrets

Do **not** commit API keys or credentials. Configure model endpoints through shell environment variables or a local `.env` file excluded from version control.

## Citation

A formal citation will be added if/when the associated research manuscript is publicly released.
