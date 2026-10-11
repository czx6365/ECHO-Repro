# CI-Repair-Bench MVP Pipeline

This MVP adds a CI-log-driven, environment-aware reproduction harness experiment for CI-Repair-Bench. It does command-level minimal reproduction only; it does not re-run full GitHub Actions workflows.

## Install

```bash
pip install -e ".[dev]"
```

Optional LLM generation can use an OpenAI-compatible endpoint:

```bash
export OPENAI_API_KEY="..."
export MODEL_NAME="gpt-4.1-mini"
# optional
export OPENAI_BASE_URL="https://..."
```

Without `OPENAI_API_KEY`, the runner writes `generated/prompt.txt` and records `skipped_no_llm` instead of crashing.

Anthropic-compatible endpoints are also supported, including DeepSeek's
Anthropic-compatible API:

```bash
export ANTHROPIC_BASE_URL="https://..."
export ANTHROPIC_AUTH_TOKEN="..."
export ANTHROPIC_MODEL="..."
```

## Inspect Data

HuggingFace:

```bash
python scripts/inspect_ci_repair_bench.py --max_examples 3
```

Local JSON/JSONL/CSV:

```bash
python scripts/inspect_ci_repair_bench.py --dataset_path data/ci_repair_bench.jsonl --max_examples 3
```

## Prepare A Subset

Subset JSONL files are generated locally under `data/` and are intentionally
gitignored because raw benchmark logs can contain token-like strings.

```bash
python scripts/prepare_ci_repair_subset.py \
  --limit 50 \
  --seed 42 \
  --output data/ci_repair_subset_50.jsonl
```

If the dataset does not expose a recognizable failure type field, sampling falls back to deterministic random sampling.

To extend a frozen 30-instance pilot with the paper's exact 20-instance strata:

```bash
python scripts/prepare_ci_repair_stratified_extension.py \
  --base data/ci_repair_subset_30.jsonl \
  --seed 42 \
  --extension_output data/ci_repair_extension_20.jsonl \
  --combined_output data/ci_repair_subset_50.jsonl \
  --manifest_output data/ci_repair_subset_50_manifest.json
```

The default extension is 6 test, 6 dependency/install, 4
build/workflow/config, 2 lint, and 2 format instances. The manifest records
stratum IDs and SHA-256 hashes and guarantees no overlap with the base subset.

## Run One Instance

```bash
python scripts/run_ci_repair_mvp.py \
  --dataset data/ci_repair_subset_50.jsonl \
  --limit 1 \
  --setting S5_full_context \
  --output_dir outputs/ci_repair_mvp
```

Artifacts are written under:

```text
outputs/ci_repair_mvp/<instance_id>/<setting>/
```

Each setting directory contains `failure_spec.json`, `context/`, `generated/`, `execution/`, and a top-level `result.json`.

For a targeted acceptance replay, select exact IDs without rewriting a frozen
experiment directory:

```bash
python scripts/run_ci_repair_mvp.py \
  --dataset data/ci_repair_subset_30.jsonl \
  --instance_ids 111,154,296,346,484 \
  --settings S5_full_context \
  --reuse_generated_from outputs/ci_repair_mvp_e21_30x3 \
  --env_policy E2_tool_bootstrap \
  --cleanup_worktrees \
  --output_dir outputs/ci_repair_mvp_validityfix_replay
```

`--reuse_generated_from` copies generated harnesses into the new experiment;
it never edits the source experiment.
`--cleanup_worktrees` keeps persisted diagnostics but removes the two temporary
checkouts after each attempt, which is recommended for larger ablations.
Use `--resume` to skip instance/setting pairs that already have a persisted
top-level `result.json` after an interrupted run.

## Run A Small Ablation

```bash
python scripts/run_ci_repair_mvp.py \
  --dataset data/ci_repair_subset_50.jsonl \
  --settings S1_issue_only,S2_code_only,S3_log_only,S4_log_workflow,S5_full_context,S6_full_refine,S7_router,S8_router_typed_refine,S9_oracle_router \
  --limit 10 \
  --output_dir outputs/ci_repair_mvp
```

## Summarize

```bash
python scripts/summarize_ci_repair_results.py \
  --result_dir outputs/ci_repair_mvp
```

This writes:

- `outputs/ci_repair_mvp/results.csv`
- `outputs/ci_repair_mvp/summary.json`
- `outputs/ci_repair_mvp/summary.md`

## Settings

- `S1_issue_only`: metadata/failure label only.
- `S2_code_only`: FailureSpec plus source/test snippets.
- `S3_log_only`: FailureSpec plus CI log snippet.
- `S4_log_workflow`: CI log plus workflow snippet.
- `S5_full_context`: log, workflow, source, tests, and env/dependency snippets.
- `S6_full_refine`: full context plus up to 4 feedback attempts.
- `S7_router`: failure-type-aware context routing.
- `S8_router_typed_refine`: routed context plus typed feedback attempts.
- `S9_oracle_router`: routed context using the dataset/oracle failure label as an upper bound.

Router settings write both `context_full/` and routed `context/`, plus
`context/routing_decision.json` and top-level `routing_decision` fields in
`result.json`.

## Validity diagnostics

With `E2_tool_bootstrap`, every detected CI tool is installed and resolved only
inside the checkout's `.echo_venv`. Results record the interpreter, effective
PATH, resolved tools, unresolved tools, and ignored host binaries.

Execution results also record:

- structured workflow command, working directory, environment, setup command,
  config, and target-scope signals for workflow-aware settings;
- cross-commit target stability, including diff-proven deletion and rename;
- normalized repo-relative/formatter-semantic oracle matching;
- missing-module attribution to target imports, project dependency declarations,
  requirements, and workflow setup, plus a diagnosis-only minimal install hint.

Workflow-derived diagnostics and post-processing are disabled for S1–S3, so
the ablation treatment cannot receive workflow context indirectly.
