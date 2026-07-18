from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

from echo_ci.ci_context_retriever import ContextBundle
from echo_ci.harness_validator import validate_harness
from echo_ci.prompts import INPUT_SETTINGS, WORKFLOW_AWARE_SETTINGS, build_harness_prompt
from echo_ci.workflow_parser import extract_workflow_execution_signal


@dataclass
class HarnessGenerationResult:
    status: str
    setting: str
    output_dir: str
    prompt_path: str
    reproduce_path: str = ""
    run_path: str = ""
    oracle_path: str = ""
    error: str = ""
    llm_metadata: dict[str, Any] = dataclass_field(default_factory=dict)
    validation: dict[str, Any] = dataclass_field(default_factory=dict)
    validation_attempts: list[dict[str, Any]] = dataclass_field(default_factory=list)
    grounding: dict[str, Any] = dataclass_field(default_factory=dict)
    metadata_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json|python|bash|sh)?\s*\n(?P<body>.*?)\n```", stripped, re.DOTALL)
    if fenced:
        return fenced.group("body").strip()
    stripped = re.sub(r"^\s*```[a-zA-Z0-9_-]*\s*$", "", stripped, flags=re.MULTILINE)
    stripped = re.sub(r"^\s*```\s*$", "", stripped, flags=re.MULTILINE)
    return stripped.strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = _strip_fence(text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def default_oracle(context: ContextBundle) -> dict[str, Any]:
    spec = context.failure_spec
    signature = str(spec.get("error_signature", "")).splitlines()[-1:] or []
    signature = [item.strip() for item in signature if item.strip()]
    return {
        "failing_exit_nonzero": True,
        "fixed_exit_zero": True,
        "expected_failing_exit_code": "nonzero",
        "expected_passing_exit_code": 0,
        "any_failing_signal": signature[:1],
        "must_contain_on_failing": signature[:1],
        "must_not_contain_on_fixed": signature[:1],
        "oracle_type": spec.get("oracle_type") or spec.get("failure_type") or "unknown",
        "path_match_mode": "repo_relative",
    }


def preserve_workflow_execution_signals(
    *,
    run_text: str,
    reproduce_text: str,
    context: ContextBundle,
    grounding: dict[str, Any],
    setting: str,
) -> tuple[str, list[dict[str, str]]]:
    if setting not in WORKFLOW_AWARE_SETTINGS:
        return run_text, []
    failed_command = str(context.failure_spec.get("failed_command") or "")
    selected_command = str(grounding.get("selected_command") or "")
    query = "\n".join(
        value
        for value in (
            failed_command,
            selected_command,
            str(context.failure_spec.get("error_signature") or ""),
        )
        if value.strip()
    )
    signal = extract_workflow_execution_signal(context.workflow, command=query)
    working_directory = str(signal.get("working_directory") or "").strip().strip("./")
    combined = f"{run_text}\n{reproduce_text}".replace("\\", "/")
    if not signal.get("command_from_workflow") or not working_directory or working_directory in combined:
        return run_text, []
    normalized = ensure_run_sh_header(run_text)
    anchor = 'cd "$REPO_ROOT"\n'
    command = f'cd "$REPO_ROOT/{working_directory}"\n'
    if anchor in normalized:
        normalized = normalized.replace(anchor, anchor + command, 1)
    else:
        normalized = ensure_run_sh_header(command + normalized)
    return normalized, [
        {
            "kind": "working_directory",
            "value": working_directory,
            "source": "workflow",
        }
    ]


def _normalize_files(
    payload: dict[str, Any],
    context: ContextBundle,
    *,
    setting: str,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    reproduce = payload.get("reproduce.py") or payload.get("reproduce_py") or payload.get("reproduce")
    run = payload.get("run.sh") or payload.get("run_sh") or payload.get("run")
    oracle = payload.get("oracle.json") or payload.get("oracle_json") or payload.get("oracle")
    grounding = payload.get("grounding") or payload.get("generation_metadata") or {}

    if isinstance(reproduce, dict):
        reproduce = reproduce.get("content", "")
    if isinstance(run, dict):
        run = run.get("content", "")
    if isinstance(oracle, str):
        oracle = _extract_json_object(oracle)
    if not isinstance(oracle, dict):
        oracle = default_oracle(context)
    if isinstance(grounding, str):
        try:
            grounding = _extract_json_object(grounding)
        except Exception:
            grounding = {"raw": grounding}
    if not isinstance(grounding, dict):
        grounding = {}
    if "grounding" in oracle and not grounding:
        raw_grounding = oracle.get("grounding")
        grounding = raw_grounding if isinstance(raw_grounding, dict) else {"raw": str(raw_grounding)}

    reproduce_text = _strip_fence(str(reproduce or ""))
    run_text = _strip_fence(str(run or ""))
    if not reproduce_text.strip():
        raise ValueError("LLM response did not include reproduce.py content.")
    run_text = normalize_run_sh(run_text)
    if not run_text.strip():
        run_text = normalize_run_sh("python3 .echo_repro/reproduce.py\n")
    run_text = re.sub(r"\bpython\s+reproduce\.py\b", '"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"', run_text)
    run_text = re.sub(r"\bpython3\s+reproduce\.py\b", '"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"', run_text)
    run_text = re.sub(r"\bpython\s+\.echo_repro/reproduce\.py\b", '"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"', run_text)
    run_text = re.sub(r"\bpython3\s+\.echo_repro/reproduce\.py\b", '"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"', run_text)
    run_text = re.sub(r"(?<![\w./-])python3(?=\s)", '"$REPO_ROOT/.echo_venv/bin/python"', run_text)
    run_text = re.sub(r"(?m)^(\s*)pip\s+install\b", r"\1python3 -m pip install", run_text)
    run_text = ensure_run_sh_header(run_text)
    run_text, adjustments = preserve_workflow_execution_signals(
        run_text=run_text,
        reproduce_text=reproduce_text,
        context=context,
        grounding=grounding,
        setting=setting,
    )
    if adjustments:
        grounding = dict(grounding)
        grounding["execution_signal_adjustments"] = adjustments
    return reproduce_text.rstrip() + "\n", run_text.rstrip() + "\n", oracle, grounding


def normalize_run_sh(run_text: str) -> str:
    text = _strip_fence(str(run_text or ""))
    text = re.sub(r"(?m)^\s*cd\s+['\"]?\$?\(?dirname\s+['\"]?\$0['\"]?\)?['\"]?\s*$", "", text)
    text = re.sub(r"(?m)^\s*cd\s+['\"]?\$\(dirname\s+\"\$0\"\)['\"]?\s*$", "", text)
    text = re.sub(r"\bpython\s+reproduce\.py\b", '"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"', text)
    text = re.sub(r"\bpython3\s+reproduce\.py\b", '"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"', text)
    text = re.sub(r"\bpython\s+\.echo_repro/reproduce\.py\b", '"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"', text)
    text = re.sub(r"\bpython3\s+\.echo_repro/reproduce\.py\b", '"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"', text)
    text = re.sub(r"(?<![\w./-])python3(?=\s)", '"$REPO_ROOT/.echo_venv/bin/python"', text)
    return text.strip() + ("\n" if text.strip() else "")


def ensure_run_sh_header(run_text: str) -> str:
    body = run_text.strip()
    lines = body.splitlines()
    lines = [line for line in lines if not line.startswith("#!")]
    lines = [line for line in lines if not re.fullmatch(r"\s*set\s+-euo\s+pipefail\s*", line)]
    lines = [line for line in lines if not re.fullmatch(r"\s*set\s+-e\s*", line)]
    # This normalizer runs after generation and again during installation.
    # Remove prior canonical header lines globally so repeated normalization
    # cannot reset a workflow-derived ``cd`` later in the file.
    lines = [line for line in lines if not re.fullmatch(r"\s*set\s+-u\s*", line)]
    lines = [
        line
        for line in lines
        if not re.fullmatch(
            r'\s*REPO_ROOT="\$\(git rev-parse --show-toplevel 2>/dev/null \|\| pwd\)"\s*',
            line,
        )
    ]
    lines = [line for line in lines if not re.fullmatch(r'\s*cd\s+"\$REPO_ROOT"\s*', line)]
    header = [
        "#!/usr/bin/env bash",
        "set -u",
        'REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"',
        'cd "$REPO_ROOT"',
    ]
    return "\n".join(header + lines).rstrip() + "\n"


def _call_openai(prompt: str, *, model: str, api_key: str, base_url: str | None, temperature: float, max_tokens: int) -> tuple[str, dict[str, Any]]:
    from openai import OpenAI

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": "Return only valid JSON. No markdown fences."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or ""
    usage = getattr(response, "usage", None)
    metadata = {
        "provider": "openai_compatible",
        "model": model,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
        "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
    }
    return content, metadata


def _anthropic_messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def _call_anthropic(
    prompt: str,
    *,
    model: str,
    api_key: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout_seconds: int,
) -> tuple[str, dict[str, Any]]:
    try:
        import httpx
    except ImportError:
        httpx = None

    started = time.perf_counter()
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": "Return only valid JSON. No markdown fences.",
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            if httpx is not None:
                with httpx.Client(timeout=timeout_seconds) as client:
                    response = client.post(
                        _anthropic_messages_url(base_url),
                        headers=headers,
                        json=payload,
                    )
                    if response.status_code >= 400:
                        if response.status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                            raise ValueError(
                                f"Anthropic-compatible request failed with HTTP {response.status_code}: {response.text}"
                            )
                        last_error = ValueError(response.text)
                        time.sleep(attempt)
                        continue
                    raw = response.json()
                    break
            else:
                request = urllib.request.Request(
                    _anthropic_messages_url(base_url),
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                    break
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise ValueError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body_text}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if attempt == 3:
                raise ValueError(f"Anthropic-compatible request failed after retries: {exc}") from exc
            last_error = exc
        time.sleep(attempt)
    else:
        raise ValueError(f"Anthropic-compatible request failed after retries: {last_error}")

    content_blocks = raw.get("content") or []
    content_parts = [
        str(block.get("text") or "")
        for block in content_blocks
        if isinstance(block, dict) and block.get("type") in {None, "text"}
    ]
    content = "\n".join(part for part in content_parts if part).strip()
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = (
        int(input_tokens or 0) + int(output_tokens or 0)
        if input_tokens is not None or output_tokens is not None
        else None
    )
    metadata = {
        "provider": "anthropic_compatible",
        "model": model,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    return content, metadata


def _llm_backend_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))


def _write_files(
    output_dir: Path,
    reproduce: str,
    run: str,
    oracle: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    reproduce_path = output_dir / "reproduce.py"
    run_path = output_dir / "run.sh"
    oracle_path = output_dir / "oracle.json"
    metadata_path = output_dir / "generation_metadata.json"
    reproduce_path.write_text(reproduce, encoding="utf-8")
    run_path.write_text(run, encoding="utf-8")
    run_path.chmod(0o755)
    oracle_path.write_text(json.dumps(oracle, ensure_ascii=False, indent=2), encoding="utf-8")
    if metadata is not None:
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return reproduce_path, run_path, oracle_path, metadata_path


def write_skipped_no_llm(
    output_dir: Path,
    prompt: str,
    setting: str,
    *,
    extra_generation_metadata: dict[str, Any] | None = None,
) -> HarnessGenerationResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = output_dir / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    reproduce = (
        '"""Placeholder generated because OPENAI_API_KEY was not set."""\n'
        'print("skipped_no_llm")\n'
    )
    run = ensure_run_sh_header('"$REPO_ROOT/.echo_venv/bin/python" "$REPO_ROOT/.echo_repro/reproduce.py"\n')
    oracle = {
        "expected_failing_exit_code": "nonzero",
        "expected_passing_exit_code": 0,
        "must_contain_on_failing": [],
        "must_not_contain_on_fixed": [],
        "oracle_type": "unknown",
    }
    validation = {"valid": False, "issues": ["skipped_no_llm"], "warnings": []}
    metadata_payload = {
        "grounding": {},
        "validation": validation,
        "status": "skipped_no_llm",
        "setting": setting,
    }
    if extra_generation_metadata:
        metadata_payload.update(extra_generation_metadata)
    reproduce_path, run_path, oracle_path, metadata_path = _write_files(
        output_dir,
        reproduce,
        run,
        oracle,
        metadata=metadata_payload,
    )
    return HarnessGenerationResult(
        status="skipped_no_llm",
        setting=setting,
        output_dir=str(output_dir),
        prompt_path=str(prompt_path),
        reproduce_path=str(reproduce_path),
        run_path=str(run_path),
        oracle_path=str(oracle_path),
        metadata_path=str(metadata_path),
        error="OPENAI_API_KEY is not set; wrote prompt for audit.",
        validation=validation,
    )


def generate_harness(
    *,
    setting: str,
    context: ContextBundle,
    output_dir: Path,
    metadata: dict[str, Any] | None = None,
    refinement_feedback: dict[str, Any] | None = None,
    extra_generation_metadata: dict[str, Any] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4_000,
    max_validation_regenerations: int = 2,
) -> HarnessGenerationResult:
    if setting not in INPUT_SETTINGS:
        raise ValueError(f"Unknown setting {setting!r}. Expected one of {', '.join(INPUT_SETTINGS)}.")

    prompt = build_harness_prompt(
        setting=setting,
        context=context,
        metadata=metadata,
        refinement_feedback=refinement_feedback,
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = output_dir / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    if not _llm_backend_available():
        return write_skipped_no_llm(
            output_dir,
            prompt,
            setting,
            extra_generation_metadata=extra_generation_metadata,
        )

    openai_api_key = os.environ.get("OPENAI_API_KEY")
    anthropic_api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    provider = "openai_compatible" if openai_api_key else "anthropic_compatible"
    model = (
        os.environ.get("MODEL_NAME")
        or os.environ.get("OPENAI_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or ("gpt-4.1-mini" if provider == "openai_compatible" else "claude-3-5-sonnet-latest")
    )
    base_url = os.environ.get("OPENAI_BASE_URL")
    anthropic_base_url = os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    anthropic_timeout = int(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS") or os.environ.get("ANTHROPIC_TIMEOUT") or "300")
    validation_attempts: list[dict[str, Any]] = []
    accumulated_usage = {
        "provider": provider,
        "model": model,
        "latency_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    current_feedback = refinement_feedback
    last_error = ""
    try:
        for generation_attempt in range(1, max_validation_regenerations + 2):
            attempt_prompt = build_harness_prompt(
                setting=setting,
                context=context,
                metadata=metadata,
                refinement_feedback=current_feedback,
            )
            attempt_prompt_path = output_dir / f"prompt_attempt_{generation_attempt}.txt"
            attempt_prompt_path.write_text(attempt_prompt, encoding="utf-8")
            if generation_attempt == 1:
                prompt_path.write_text(attempt_prompt, encoding="utf-8")

            if provider == "openai_compatible":
                raw, metadata_out = _call_openai(
                    attempt_prompt,
                    model=model,
                    api_key=str(openai_api_key),
                    base_url=base_url,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            else:
                raw, metadata_out = _call_anthropic(
                    attempt_prompt,
                    model=model,
                    api_key=str(anthropic_api_key),
                    base_url=anthropic_base_url,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout_seconds=anthropic_timeout,
                )
            for key in ("latency_ms", "input_tokens", "output_tokens", "total_tokens"):
                accumulated_usage[key] = int(accumulated_usage.get(key) or 0) + int(metadata_out.get(key) or 0)
            payload = _extract_json_object(raw)
            reproduce, run, oracle, grounding = _normalize_files(payload, context, setting=setting)
            validation = validate_harness(
                reproduce_text=reproduce,
                run_text=run,
                oracle=oracle,
                context=context,
                setting=setting,
                grounding=grounding,
            )
            validation_attempts.append(
                {
                    "attempt": generation_attempt,
                    "prompt_path": str(attempt_prompt_path),
                    "validation": validation,
                    "grounding": grounding,
                    "llm_metadata": metadata_out,
                }
            )
            metadata_payload = {
                "status": "generated" if validation["valid"] else "validation_error",
                "setting": setting,
                "grounding": grounding,
                "validation": validation,
                "validation_attempts": validation_attempts,
                "llm_metadata": accumulated_usage,
            }
            if extra_generation_metadata:
                metadata_payload.update(extra_generation_metadata)
            reproduce_path, run_path, oracle_path, metadata_path = _write_files(
                output_dir,
                reproduce,
                run,
                oracle,
                metadata=metadata_payload,
            )
            if validation["valid"]:
                return HarnessGenerationResult(
                    status="generated",
                    setting=setting,
                    output_dir=str(output_dir),
                    prompt_path=str(prompt_path),
                    reproduce_path=str(reproduce_path),
                    run_path=str(run_path),
                    oracle_path=str(oracle_path),
                    metadata_path=str(metadata_path),
                    llm_metadata=accumulated_usage,
                    validation=validation,
                    validation_attempts=validation_attempts,
                    grounding=grounding,
                )
            current_feedback = {
                "validation_failed": True,
                "issues": validation["issues"],
                "warnings": validation["warnings"],
                "previous_grounding": grounding,
                "instruction": "Regenerate the JSON files. Fix these validator issues without adding heavy setup or fake failure.",
            }
            last_error = "; ".join(validation["issues"])
        return HarnessGenerationResult(
            status="validation_error",
            setting=setting,
            output_dir=str(output_dir),
            prompt_path=str(prompt_path),
            reproduce_path=str(reproduce_path),
            run_path=str(run_path),
            oracle_path=str(oracle_path),
            metadata_path=str(metadata_path),
            error=last_error or "Harness failed validation.",
            llm_metadata=accumulated_usage,
            validation=validation,
            validation_attempts=validation_attempts,
            grounding=grounding,
        )
    except Exception as exc:
        return HarnessGenerationResult(
            status="generation_error",
            setting=setting,
            output_dir=str(output_dir),
            prompt_path=str(prompt_path),
            error=str(exc),
        )
