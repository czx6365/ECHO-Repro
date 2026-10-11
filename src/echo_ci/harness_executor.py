from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from echo_ci.env_policy import (
    build_execution_env,
    install_ci_tool,
    parse_missing_ci_tools,
    record_execution_diagnostics,
    run_sh_uses_disallowed_install,
    setup_environment,
    validate_env_policy,
)
from echo_ci.repo_manager import CheckoutResult, RobustRepoManager, repo_clone_url
from echo_ci.result_classifier import (
    CommandExecution,
    classify_failing_error,
    classify_fixed_error,
    classify_result,
    detect_fake_reproduction,
    match_oracle,
)
from echo_ci.schema_utils import coerce_text, field, get_field, safe_instance_id
from echo_ci.harness_generator import ensure_run_sh_header, normalize_run_sh
from echo_ci.dependency_diagnosis import diagnose_missing_dependency
from echo_ci.target_preflight import cross_commit_target_preflight
from echo_ci.workflow_parser import extract_workflow_execution_signal


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _repo_clone_url(repo: str) -> str:
    return repo_clone_url(repo)


def clone_repo(repo: str, work_dir: Path) -> tuple[bool, str]:
    work_dir = Path(work_dir).resolve()
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    result = _run_command(["git", "clone", _repo_clone_url(repo), str(work_dir)], cwd=work_dir.parent, timeout=600)
    if result.returncode != 0:
        return False, result.stderr or result.stdout
    return True, ""


def checkout_commit(repo_path: Path, commit: str) -> tuple[bool, str]:
    if not commit:
        return False, "Missing commit SHA."
    result = _run_command(["git", "checkout", "--force", commit], cwd=repo_path, timeout=180)
    if result.returncode != 0:
        return False, result.stderr or result.stdout
    return True, ""


def _execution_from_checkout(
    *,
    phase: str,
    checkout: CheckoutResult,
    error_kind: str,
    project_name: str = "",
) -> CommandExecution:
    return CommandExecution(
        phase=phase,
        error=checkout.checkout_error,
        error_kind=error_kind,
        project_name=project_name,
        repo_prepare_strategy=checkout.repo_prepare_strategy,
        commit_check=checkout.commit_check,
        checkout_success=checkout.checkout_success,
        checkout_error=checkout.checkout_error,
        checkout_stage=checkout.checkout_stage,
        checkout_diagnostics=checkout.to_dict(),
    )


def _attach_checkout_diagnostics(execution: CommandExecution, checkout: CheckoutResult) -> CommandExecution:
    execution.repo_prepare_strategy = checkout.repo_prepare_strategy
    execution.commit_check = checkout.commit_check
    execution.checkout_success = checkout.checkout_success
    execution.checkout_error = checkout.checkout_error
    execution.checkout_stage = checkout.checkout_stage
    execution.checkout_diagnostics = checkout.to_dict()
    return execution


def install_harness(repo_path: Path, generated_dir: Path) -> tuple[bool, str, dict[str, Any]]:
    repo_path = Path(repo_path)
    generated_dir = Path(generated_dir)
    required = ["reproduce.py", "run.sh", "oracle.json"]
    missing = [name for name in required if not (generated_dir / name).is_file()]
    if missing:
        return False, f"Missing generated artifact(s): {', '.join(missing)}", {
            "missing_generated_artifacts": missing,
            "generated_dir": str(generated_dir),
        }

    target = repo_path / ".echo_repro"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(generated_dir / "reproduce.py", target / "reproduce.py")
    run_text = (generated_dir / "run.sh").read_text(encoding="utf-8", errors="replace")
    run_text = ensure_run_sh_header(normalize_run_sh(run_text))
    (target / "run.sh").write_text(run_text, encoding="utf-8")
    shutil.copy2(generated_dir / "oracle.json", target / "oracle.json")
    metadata = generated_dir / "generation_metadata.json"
    if metadata.is_file():
        shutil.copy2(metadata, target / "generation_metadata.json")
    (target / "run.sh").chmod(0o755)
    return True, "", preflight_harness(repo_path)


def preflight_harness(repo_path: Path) -> dict[str, Any]:
    repo_path = Path(repo_path)
    echo_dir = repo_path / ".echo_repro"
    run_sh = echo_dir / "run.sh"
    reproduce = echo_dir / "reproduce.py"
    oracle = echo_dir / "oracle.json"
    repo_root = ""
    try:
        result = _run_command(["git", "rev-parse", "--show-toplevel"], cwd=repo_path, timeout=10)
        if result.returncode == 0:
            repo_root = result.stdout.strip()
    except Exception:
        repo_root = ""
    run_text = run_sh.read_text(encoding="utf-8", errors="replace") if run_sh.is_file() else ""
    reproduce_text = reproduce.read_text(encoding="utf-8", errors="replace") if reproduce.is_file() else ""
    oracle_text = oracle.read_text(encoding="utf-8", errors="replace") if oracle.is_file() else ""
    metadata = echo_dir / "generation_metadata.json"
    metadata_text = metadata.read_text(encoding="utf-8", errors="replace") if metadata.is_file() else ""
    project_packages: list[str] = []
    for base in (repo_path, repo_path / "src"):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir() and (child / "__init__.py").is_file():
                project_packages.append(child.name)
    return {
        "repo_dir": str(repo_path),
        "echo_repro_dir_exists": echo_dir.is_dir(),
        "run_sh_exists": run_sh.is_file(),
        "reproduce_py_exists": reproduce.is_file(),
        "oracle_json_exists": oracle.is_file(),
        "run_sh_content": run_text[:4000],
        "reproduce_py_content": reproduce_text[:12000],
        "oracle_json_content": oracle_text[:4000],
        "generation_metadata_content": metadata_text[:4000],
        "repo_root": repo_root,
        "cwd": str(repo_path),
        "project_packages": sorted(set(project_packages)),
    }


def run_harness(
    repo_path: Path,
    *,
    phase: str,
    timeout_seconds: int,
    preflight: dict[str, Any] | None = None,
    env_policy: str = "E0_no_setup",
    failed_command: str = "",
    project_name: str = "",
) -> CommandExecution:
    started = time.perf_counter()
    env_policy = validate_env_policy(env_policy)
    preflight = preflight or preflight_harness(repo_path)
    if not preflight.get("echo_repro_dir_exists"):
        return CommandExecution(phase=phase, duration=0.0, error="Missing .echo_repro directory.", error_kind=f"{phase}_missing_echo_repro_dir", preflight=preflight)
    if not preflight.get("run_sh_exists"):
        return CommandExecution(phase=phase, duration=0.0, error="Missing .echo_repro/run.sh.", error_kind=f"{phase}_missing_run_sh", preflight=preflight)
    run_content = str(preflight.get("run_sh_content") or "")
    calls_reproduce = "reproduce.py" in run_content
    if calls_reproduce and not preflight.get("reproduce_py_exists"):
        return CommandExecution(phase=phase, duration=0.0, error="Missing .echo_repro/reproduce.py.", error_kind=f"{phase}_missing_reproduce_py", preflight=preflight)
    if not preflight.get("oracle_json_exists"):
        return CommandExecution(phase=phase, duration=0.0, error="Missing .echo_repro/oracle.json.", error_kind=f"{phase}_missing_oracle_json", preflight=preflight)
    reproduce_content = str(preflight.get("reproduce_py_content") or "")
    if env_policy == "E2_tool_bootstrap" and run_sh_uses_disallowed_install(f"{run_content}\n{reproduce_content}"):
        return CommandExecution(
            phase=phase,
            duration=0.0,
            error="run.sh contains disallowed dependency setup for E2_tool_bootstrap.",
            error_kind=f"{phase}_disallowed_dependency_setup",
            preflight=preflight,
        )
    env_setup, execution_env = setup_environment(
        repo_dir=repo_path,
        policy=env_policy,
        run_text=run_content,
        reproduce_text=reproduce_content,
        oracle_text=str(preflight.get("oracle_json_content") or ""),
        metadata_text=str(preflight.get("generation_metadata_content") or ""),
        failed_command=failed_command,
        timeout=max(60, min(timeout_seconds, 300)),
    )
    if env_policy == "E2_tool_bootstrap" and env_setup.unresolved_tools:
        unresolved = ", ".join(env_setup.unresolved_tools)
        return CommandExecution(
            phase=phase,
            duration=round(time.perf_counter() - started, 3),
            error=f"CI tool(s) not resolved inside .echo_venv: {unresolved}",
            error_kind=f"{phase}_tool_resolution_error",
            preflight=preflight,
            env_setup=env_setup.to_dict(),
            project_name=project_name,
        )
    def _execute_once() -> subprocess.CompletedProcess[str]:
        return _run_command(
            ["bash", ".echo_repro/run.sh"],
            cwd=repo_path,
            timeout=timeout_seconds,
            env=execution_env,
        )

    try:
        result = _execute_once()
        if env_policy == "E2_tool_bootstrap":
            output_text = f"{result.stdout}\n{result.stderr}"
            dynamic_tools = [tool for tool in parse_missing_ci_tools(output_text) if tool not in env_setup.tools_installed]
            if dynamic_tools:
                env_setup.dynamic_retry = True
                venv_path = Path(env_setup.venv_path)
                for tool in dynamic_tools:
                    install_ci_tool(
                        diagnostics=env_setup,
                        repo_dir=Path(repo_path),
                        venv_path=venv_path,
                        tool=tool,
                        env=execution_env,
                        timeout=max(60, min(timeout_seconds, 300)),
                        dynamic=True,
                    )
                execution_env = build_execution_env(Path(repo_path), venv_path, include_pythonpath=True)
                record_execution_diagnostics(
                    env_setup,
                    venv_path=venv_path,
                    execution_env=execution_env,
                )
                retry_result = _execute_once()
                env_setup.dynamic_retry_used = True
                env_setup.dynamic_retry_exit_code = retry_result.returncode
                result = retry_result
        execution = CommandExecution(
            phase=phase,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration=round(time.perf_counter() - started, 3),
            preflight=preflight,
            env_setup=env_setup.to_dict(),
            project_name=project_name,
        )
        return execution
    except subprocess.TimeoutExpired as exc:
        return CommandExecution(
            phase=phase,
            exit_code=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            duration=round(time.perf_counter() - started, 3),
            timeout=True,
            error=f"Timed out after {timeout_seconds}s",
            preflight=preflight,
            env_setup=env_setup.to_dict(),
            project_name=project_name,
        )
    except Exception as exc:
        return CommandExecution(
            phase=phase,
            duration=round(time.perf_counter() - started, 3),
            error=str(exc),
            preflight=preflight,
            env_setup=env_setup.to_dict(),
            project_name=project_name,
        )


def execute_generated_harness(
    *,
    instance: dict[str, Any],
    generated_dir: Path,
    output_dir: Path,
    work_dir: Path,
    timeout_seconds: int = 120,
    generation_status: str = "generated",
    env_policy: str = "E0_no_setup",
    cleanup_worktrees: bool = False,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    execution_dir = output_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    repo = str(field(instance, "repo", default=""))
    env_policy = validate_env_policy(env_policy)
    instance_id = safe_instance_id(instance)
    work_dir = Path(work_dir)
    instance_work_dir = work_dir / instance_id
    failing_repo_path = instance_work_dir / "failing_repo"
    fixed_repo_path = instance_work_dir / "fixed_repo"
    repo_manager = RobustRepoManager(
        cache_dir=work_dir.parent / "repo_cache",
        work_dir=work_dir,
        timeout=max(300, timeout_seconds),
    )
    oracle_path = Path(generated_dir) / "oracle.json"
    oracle = json.loads(oracle_path.read_text(encoding="utf-8")) if oracle_path.exists() else {}

    reproduce_text = (Path(generated_dir) / "reproduce.py").read_text(encoding="utf-8", errors="replace")
    run_text = (Path(generated_dir) / "run.sh").read_text(encoding="utf-8", errors="replace")
    fake, fake_reason = detect_fake_reproduction(
        reproduce_text,
        run_text,
        error_signature=" ".join(str(item) for item in oracle.get("must_contain_on_failing", [])),
    )

    if generation_status == "skipped_no_llm":
        payload = {
            "status": "skipped_no_llm",
            "classification": "skipped_no_llm",
            "repo": repo,
            "failing": None,
            "fixed": None,
            "oracle": oracle,
            "fake_reproduction_detected": fake,
            "fake_reproduction_reason": fake_reason,
        }
        (execution_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    if fake:
        payload = {
            "status": "fake_reproduction",
            "classification": "fake_reproduction",
            "repo": repo,
            "failing": None,
            "fixed": None,
            "oracle": oracle,
            "fake_reproduction_detected": True,
            "fake_reproduction_reason": fake_reason,
        }
        (execution_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    failing_commit = str(field(instance, "failing_commit", default=""))
    fixed_commit = str(field(instance, "fixed_commit", default=""))
    failed_command = str(get_field(instance, ["failed_command", "command", "ci_command"], default="") or "")
    metadata_path = Path(generated_dir) / "generation_metadata.json"
    try:
        generation_metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        generation_metadata = {}
    grounding = generation_metadata.get("grounding") if isinstance(generation_metadata, dict) else {}
    grounding = grounding if isinstance(grounding, dict) else {}
    generation_setting = str(generation_metadata.get("setting") or "")
    selected_command = str(grounding.get("selected_command") or failed_command)
    workflow_text = str(field(instance, "workflow_content", default=""))
    if generation_setting in {
        "S4_log_workflow",
        "S5_full_context",
        "S6_full_refine",
        "S7_router",
        "S8_router_typed_refine",
        "S9_oracle_router",
    }:
        workflow_signal = extract_workflow_execution_signal(
            workflow_text,
            command="\n".join(
                value
                for value in (
                    failed_command,
                    selected_command,
                    coerce_text(field(instance, "ci_log", default="")),
                )
                if value.strip()
            ),
        )
    else:
        workflow_signal = {}
    target_preflight: dict[str, Any] = {}
    fixed_checkout: CheckoutResult | None = None
    failing_checkout = repo_manager.checkout_commit(repo, failing_commit, failing_repo_path)
    if not failing_checkout.success:
        failing = _execution_from_checkout(
            phase="failing",
            checkout=failing_checkout,
            error_kind="failing_checkout_error",
            project_name=repo,
        )
        fixed = None
    else:
        fixed_checkout = repo_manager.checkout_commit(repo, fixed_commit, fixed_repo_path)
        if fixed_checkout.success:
            target_preflight = cross_commit_target_preflight(
                failing_repo=failing_repo_path,
                fixed_repo=fixed_repo_path,
                failing_commit=failing_commit,
                fixed_commit=fixed_commit,
                run_text=run_text,
                reproduce_text=reproduce_text,
                oracle_text=json.dumps(oracle, ensure_ascii=False),
            )
        install_ok, install_error, failing_preflight = install_harness(failing_repo_path, generated_dir)
        failing_preflight["cross_commit_targets"] = target_preflight
        if not install_ok:
            failing = _attach_checkout_diagnostics(
                CommandExecution(
                    phase="failing",
                    error=install_error,
                    error_kind="harness_artifact_error",
                    preflight=failing_preflight,
                ),
                failing_checkout,
            )
        else:
            failing = _attach_checkout_diagnostics(
                run_harness(
                    failing_repo_path,
                    phase="failing",
                    timeout_seconds=timeout_seconds,
                    preflight=failing_preflight,
                    env_policy=env_policy,
                    failed_command=failed_command,
                    project_name=repo,
                ),
                failing_checkout,
            )
        failing.target_preflight = target_preflight
        failing.workflow_signal = workflow_signal
        failing.matched_oracle = match_oracle(failing, oracle)

        if failing.error_kind == "harness_artifact_error":
            fixed = None
        elif not fixed_checkout.success:
            fixed = _execution_from_checkout(
                phase="fixed",
                checkout=fixed_checkout,
                error_kind="fixed_checkout_error",
                project_name=repo,
            )
        else:
            install_ok, install_error, fixed_preflight = install_harness(fixed_repo_path, generated_dir)
            fixed_preflight["cross_commit_targets"] = target_preflight
            if not install_ok:
                fixed = _attach_checkout_diagnostics(
                    CommandExecution(
                        phase="fixed",
                        error=install_error,
                        error_kind="harness_artifact_error",
                        preflight=fixed_preflight,
                    ),
                    fixed_checkout,
                )
            elif target_preflight.get("all_targets_deleted_by_fix"):
                deleted = [
                    str(record.get("target") or "")
                    for record in target_preflight.get("targets") or []
                ]
                fixed = _attach_checkout_diagnostics(
                    CommandExecution(
                        phase="fixed",
                        exit_code=0,
                        stdout="Targets deleted by the fixed commit with git-diff evidence: "
                        + ", ".join(deleted),
                        preflight=fixed_preflight,
                        project_name=repo,
                    ),
                    fixed_checkout,
                )
            else:
                fixed = _attach_checkout_diagnostics(
                    run_harness(
                        fixed_repo_path,
                        phase="fixed",
                        timeout_seconds=timeout_seconds,
                        preflight=fixed_preflight,
                        env_policy=env_policy,
                        failed_command=failed_command,
                        project_name=repo,
                    ),
                    fixed_checkout,
                )
            fixed.target_preflight = target_preflight
            fixed.workflow_signal = workflow_signal

        for execution, repo_path in ((failing, failing_repo_path), (fixed, fixed_repo_path)):
            if execution is None:
                continue
            execution.dependency_diagnosis = diagnose_missing_dependency(
                repo_path=repo_path,
                output_text=f"{execution.error}\n{execution.stdout}\n{execution.stderr}",
                target_preflight=target_preflight,
                workflow_signal=workflow_signal,
            ) or None

    for name, execution in (("failing", failing), ("fixed", fixed)):
        if execution is None:
            continue
        (execution_dir / f"{name}_stdout.txt").write_text(execution.stdout, encoding="utf-8")
        (execution_dir / f"{name}_stderr.txt").write_text(execution.stderr, encoding="utf-8")

    classification = classify_result(
        failing=failing,
        fixed=fixed,
        oracle=oracle,
        fake_reproduction_detected=False,
        generation_status=generation_status,
    )
    fixed_error_subtype = ""
    fixed_error_reason = ""
    failing_error_subtype = ""
    failing_error_reason = ""
    if classification.startswith("failing_"):
        failing_error_subtype, failing_error_reason = classify_failing_error(failing)
    if classification.startswith("fixed_") or classification == "fixed_error":
        fixed_error_subtype, fixed_error_reason = classify_fixed_error(fixed)
    payload = {
        "status": "executed",
        "classification": classification,
        "failing_error_subtype": failing_error_subtype,
        "failing_error_reason": failing_error_reason[:4000] if failing_error_reason else "",
        "fixed_error_subtype": fixed_error_subtype,
        "fixed_error_reason": fixed_error_reason[:4000] if fixed_error_reason else "",
        "repo": repo,
        "env_policy": env_policy,
        "failing": failing.to_dict() if failing else None,
        "fixed": fixed.to_dict() if fixed else None,
        "oracle": oracle,
        "fake_reproduction_detected": False,
        "fake_reproduction_reason": "",
        "target_preflight": target_preflight,
        "workflow_signal": workflow_signal,
    }
    if cleanup_worktrees:
        repo_cache_value = failing_checkout.repo_cache or (
            fixed_checkout.repo_cache if fixed_checkout is not None else ""
        )
        repo_cache = Path(repo_cache_value) if repo_cache_value else None
        payload["worktree_cleanup"] = {
            "requested": True,
            "failing_removed": repo_manager.cleanup_checkout(
                failing_repo_path,
                repo_cache=repo_cache,
            ),
            "fixed_removed": repo_manager.cleanup_checkout(
                fixed_repo_path,
                repo_cache=repo_cache,
            ),
        }
    else:
        payload["worktree_cleanup"] = {"requested": False}
    (execution_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
