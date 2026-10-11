import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

import echo_repro.cli as cli_module
import echo_repro.native_oracle as native_oracle
from echo_repro.cli import app
from echo_repro.llm.mock_client import MockLLMClient
from echo_repro.models import (
    BugSpec,
    ExecutionResult,
    HarnessCandidate,
    NativeOracleResult,
    PipelineResult,
    PreparedRepos,
    RetrievedContext,
    ValidationResult,
)
from echo_repro.result_writer import SCHEMA_VERSION
from echo_repro.validator import validate_fail_to_pass

runner = CliRunner()


def _make_mock_repo(repo_dir: Path, fixed: bool) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    if fixed:
        body = (
            'def divide(a, b):\n'
            '    if b == 0:\n'
            '        raise ZeroDivisionError("division by zero")\n'
            "    return a / b\n"
        )
    else:
        body = (
            "def divide(a, b):\n"
            "    if b == 0:\n"
            "        return 0\n"
            "    return a / b\n"
        )
    (repo_dir / "buggy_module.py").write_text(body, encoding="utf-8")
    (repo_dir / "test_buggy_module.py").write_text(
        "from buggy_module import divide\n",
        encoding="utf-8",
    )
    (repo_dir / "requirements.txt").write_text("pytest>=8\n", encoding="utf-8")


def test_run_swebench_one_creates_result_json(tmp_path: Path, monkeypatch):
    instances_file = tmp_path / "instances.jsonl"
    instance = {
        "instance_id": "demo__repo-1",
        "repo": "demo/repo",
        "version": "lite",
        "base_commit": "abc123",
        "problem_statement": "divide(a, b) should raise ZeroDivisionError when b is zero",
        "patch": "unused in mocked prepare",
    }
    instances_file.write_text(json.dumps(instance) + "\n", encoding="utf-8")

    buggy_repo = tmp_path / "prepared" / "demo__repo-1" / "buggy"
    fixed_repo = tmp_path / "prepared" / "demo__repo-1" / "fixed"
    _make_mock_repo(buggy_repo, fixed=False)
    _make_mock_repo(fixed_repo, fixed=True)

    def fake_prepare(instance: dict, workdir: Path, cache_dir: Path | None = None) -> PreparedRepos:
        assert cache_dir == Path("repos/cache")
        return PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            patch_applied=True,
            repo_cache_path=cache_dir,
        )

    monkeypatch.setattr(cli_module, "prepare_swebench_repos", fake_prepare)

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-1",
                "--workdir",
                "repos",
                "--mock",
                "--max-attempts",
                "3",
            ],
        )
        assert result.exit_code == 0, result.stdout
        output_path = Path("outputs/demo__repo-1/result.json")
        assert output_path.exists()
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert payload["instance_metadata"]["instance_id"] == "demo__repo-1"
        assert payload["instance_metadata"]["patch_applied"] is True
        assert payload["final_result"]["buggy_status"] == "reproduced"
        assert payload["final_result"]["fixed_status"] == "resolved"
        assert payload["final_result"]["is_f2p"] is True
        assert payload["schema_version"] == SCHEMA_VERSION
        # new research experiments default to the patch-blind protocol
        assert payload["run_config"]["feedback_mode"] == "patch_blind"
        assert Path("outputs/demo__repo-1/concise_context.md").exists()
        assert Path("outputs/demo__repo-1/final_reproduce.py").exists()
        assert Path("outputs/demo__repo-1/attempts.jsonl").exists()
        assert Path("outputs/demo__repo-1/prompts/bug_spec.md").exists()


def test_existing_version_command_still_works():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_run_swebench_one_accepts_llm_mock(tmp_path: Path, monkeypatch):
    instances_file = tmp_path / "instances.jsonl"
    instance = {
        "instance_id": "demo__repo-2",
        "repo": "demo/repo",
        "version": "lite",
        "base_commit": "abc123",
        "problem_statement": "divide(a, b) should raise ZeroDivisionError when b is zero",
        "patch": "unused in mocked prepare",
    }
    instances_file.write_text(json.dumps(instance) + "\n", encoding="utf-8")

    buggy_repo = tmp_path / "prepared" / "demo__repo-2" / "buggy"
    fixed_repo = tmp_path / "prepared" / "demo__repo-2" / "fixed"
    _make_mock_repo(buggy_repo, fixed=False)
    _make_mock_repo(fixed_repo, fixed=True)

    def fake_prepare(instance: dict, workdir: Path, cache_dir: Path | None = None) -> PreparedRepos:
        return PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            patch_applied=True,
        )

    monkeypatch.setattr(cli_module, "prepare_swebench_repos", fake_prepare)

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-2",
                "--workdir",
                "repos",
                "--llm",
                "mock",
            ],
        )
        assert result.exit_code == 0, result.stdout


def test_run_swebench_one_openai_without_api_key_gives_clear_error(tmp_path: Path, monkeypatch):
    instances_file = tmp_path / "instances.jsonl"
    instance = {
        "instance_id": "demo__repo-3",
        "repo": "demo/repo",
        "version": "lite",
        "base_commit": "abc123",
        "problem_statement": "divide(a, b) should raise ZeroDivisionError when b is zero",
        "patch": "unused in mocked prepare",
    }
    instances_file.write_text(json.dumps(instance) + "\n", encoding="utf-8")

    buggy_repo = tmp_path / "prepared" / "demo__repo-3" / "buggy"
    fixed_repo = tmp_path / "prepared" / "demo__repo-3" / "fixed"
    _make_mock_repo(buggy_repo, fixed=False)
    _make_mock_repo(fixed_repo, fixed=True)

    def fake_prepare(instance: dict, workdir: Path, cache_dir: Path | None = None) -> PreparedRepos:
        return PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            patch_applied=True,
        )

    monkeypatch.setattr(cli_module, "prepare_swebench_repos", fake_prepare)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-3",
                "--workdir",
                "repos",
                "--llm",
                "openai",
            ],
        )
        assert result.exit_code != 0
        assert "OPENAI_API_KEY" in result.stdout


def test_run_swebench_one_no_max_attempts_defaults_to_patch_blind(tmp_path: Path, monkeypatch):
    # Without --max-attempts the CLI walks the non-feedback-loop path; the
    # declared patch_blind default must still reach run_pipeline and the
    # result artifacts, not silently fall back to patch_aware.
    instances_file = tmp_path / "instances.jsonl"
    instance = {
        "instance_id": "demo__repo-4",
        "repo": "demo/repo",
        "version": "lite",
        "base_commit": "abc123",
        "problem_statement": "divide(a, b) should raise ZeroDivisionError when b is zero",
        "patch": "unused in mocked prepare",
    }
    instances_file.write_text(json.dumps(instance) + "\n", encoding="utf-8")

    buggy_repo = tmp_path / "prepared" / "demo__repo-4" / "buggy"
    fixed_repo = tmp_path / "prepared" / "demo__repo-4" / "fixed"
    _make_mock_repo(buggy_repo, fixed=False)
    _make_mock_repo(fixed_repo, fixed=True)

    def fake_prepare(instance: dict, workdir: Path, cache_dir: Path | None = None) -> PreparedRepos:
        return PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            patch_applied=True,
        )

    monkeypatch.setattr(cli_module, "prepare_swebench_repos", fake_prepare)

    captured = {}
    real_run_pipeline = cli_module.run_pipeline

    def spy_run_pipeline(
        issue_text,
        buggy_repo,
        fixed_repo=None,
        use_mock_llm=True,
        llm_provider=None,
        feedback_mode="patch_aware",
    ):
        captured["feedback_mode"] = feedback_mode
        captured["result"] = real_run_pipeline(
            issue_text,
            buggy_repo,
            fixed_repo,
            use_mock_llm,
            llm_provider,
            feedback_mode,
        )
        return captured["result"]

    monkeypatch.setattr(cli_module, "run_pipeline", spy_run_pipeline)

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-4",
                "--workdir",
                "repos",
                "--mock",
            ],
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(Path("outputs/demo__repo-4/result.json").read_text(encoding="utf-8"))

        assert captured["feedback_mode"] == "patch_blind"
        assert captured["result"].feedback_mode == "patch_blind"
        assert payload["run_config"]["feedback_mode"] == "patch_blind"
        assert payload["final_result"]["buggy_status"] == "reproduced"
        assert payload["final_result"]["is_f2p"] is True


def _crafted_pipeline_result(feedback_mode: str) -> PipelineResult:
    buggy_repo = Path("examples/mock_buggy_repo")
    fixed_repo = Path("examples/mock_fixed_repo")
    buggy_execution = ExecutionResult(
        repo_path=buggy_repo,
        command="python reproduce.py",
        returncode=0,
        stdout="tool log\nIssue reproduced\n",
        stderr="",
    )
    fixed_execution = ExecutionResult(
        repo_path=fixed_repo,
        command="python reproduce.py",
        returncode=0,
        stdout="Issue resolved\n",
        stderr="",
    )
    strict = feedback_mode == "patch_blind"
    return PipelineResult(
        llm_provider="mock",
        feedback_mode=feedback_mode,
        bug_spec=BugSpec(
            title="t",
            summary="s",
            current_behavior="c",
            expected_behavior="e",
            failure_signature="f",
        ),
        retrieved_context=RetrievedContext(repo_path=buggy_repo),
        concise_context="context",
        harness_candidate=HarnessCandidate(code="print('x')\n"),
        buggy_execution=buggy_execution,
        fixed_execution=fixed_execution,
        validation=validate_fail_to_pass(buggy_execution, fixed_execution, strict=strict),
    )


def test_run_swebench_one_display_matches_strict_classification_in_result_json(
    tmp_path: Path, monkeypatch
):
    instances_file = tmp_path / "instances.jsonl"
    instances_file.write_text(
        json.dumps(
            {
                "instance_id": "demo__repo-5",
                "repo": "demo/repo",
                "base_commit": "abc123",
                "problem_statement": "x",
                "patch": "y",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_module,
        "prepare_swebench_repos",
        lambda instance, workdir, cache_dir=None: PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=Path("examples/mock_buggy_repo"),
            fixed_repo=Path("examples/mock_fixed_repo"),
            patch_applied=True,
        ),
    )
    monkeypatch.setattr(cli_module, "run_pipeline", lambda *a, **k: _crafted_pipeline_result("patch_blind"))

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-5",
                "--workdir",
                "repos",
                "--mock",
            ],
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(Path("outputs/demo__repo-5/result.json").read_text(encoding="utf-8"))

        # the terminal summary and the persisted record must agree
        assert '"buggy_execution_status": "oracle_error"' in result.stdout
        assert payload["run_config"]["feedback_mode"] == "patch_blind"
        assert payload["final_result"]["buggy_status"] == "oracle_error"
        assert payload["final_result"]["is_f2p"] is False


def test_run_swebench_one_patch_aware_keeps_legacy_tolerance_without_max_attempts(
    tmp_path: Path, monkeypatch
):
    instances_file = tmp_path / "instances.jsonl"
    instances_file.write_text(
        json.dumps(
            {
                "instance_id": "demo__repo-6",
                "repo": "demo/repo",
                "base_commit": "abc123",
                "problem_statement": "x",
                "patch": "y",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_module,
        "prepare_swebench_repos",
        lambda instance, workdir, cache_dir=None: PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=Path("examples/mock_buggy_repo"),
            fixed_repo=Path("examples/mock_fixed_repo"),
            patch_applied=True,
        ),
    )
    monkeypatch.setattr(cli_module, "run_pipeline", lambda *a, **k: _crafted_pipeline_result("patch_aware"))

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-6",
                "--workdir",
                "repos",
                "--mock",
                "--feedback-mode",
                "patch_aware",
            ],
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(Path("outputs/demo__repo-6/result.json").read_text(encoding="utf-8"))

        # legacy tolerance: the marker behind an extra stdout line is accepted
        assert '"buggy_execution_status": "reproduced"' in result.stdout
        assert payload["run_config"]["feedback_mode"] == "patch_aware"
        assert payload["final_result"]["buggy_status"] == "reproduced"
        assert payload["final_result"]["is_f2p"] is True


def _fake_native_oracle_internals(monkeypatch, stdout_text: str = ""):
    """Deterministic native oracle: patch always applies, the buggy JUnit XML
    shows one failure and the fixed JUnit XML shows a clean pass."""

    def fake_apply(repo_dir: Path, test_patch: str):
        return subprocess.CompletedProcess(["patch"], 0, "", "")

    def fake_run(cmd, *, cwd, timeout, input_text=None):
        role = "buggy" if Path(cwd).name == "buggy_eval" else "fixed"
        spec = (
            {"tests": 1, "failures": 1, "returncode": 1}
            if role == "buggy"
            else {"tests": 1, "returncode": 0}
        )
        xml_index = next(i for i, part in enumerate(cmd) if part.startswith("--junitxml="))
        xml_path = Path(cmd[xml_index].split("=", 1)[1])
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(
            '<?xml version="1.0"?><testsuites><testsuite name="pytest" '
            f'tests="{spec["tests"]}" failures="{spec.get("failures", 0)}" '
            'errors="0" skipped="0"></testsuite></testsuites>',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, spec["returncode"], stdout_text, "")

    monkeypatch.setattr(native_oracle, "_apply_test_patch", fake_apply)
    monkeypatch.setattr(native_oracle, "_run_command", fake_run)


def test_run_swebench_one_native_oracle_defaults_off(tmp_path: Path, monkeypatch):
    instances_file = tmp_path / "instances.jsonl"
    instances_file.write_text(
        json.dumps(
            {
                "instance_id": "demo__repo-7",
                "repo": "demo/repo",
                "base_commit": "abc123",
                "problem_statement": "divide(a, b) should raise ZeroDivisionError when b is zero",
                "patch": "unused in mocked prepare",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    buggy_repo = tmp_path / "prepared" / "demo__repo-7" / "buggy"
    fixed_repo = tmp_path / "prepared" / "demo__repo-7" / "fixed"
    _make_mock_repo(buggy_repo, fixed=False)
    _make_mock_repo(fixed_repo, fixed=True)
    monkeypatch.setattr(
        cli_module,
        "prepare_swebench_repos",
        lambda instance, workdir, cache_dir=None: PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            patch_applied=True,
        ),
    )

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-7",
                "--workdir",
                "repos",
                "--mock",
            ],
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(Path("outputs/demo__repo-7/result.json").read_text(encoding="utf-8"))

        assert payload["run_config"]["native_oracle"] == "off"
        assert payload["run_config"]["native_test_timeout"] == 600
        assert payload["native_oracle"]["mode"] == "off"
        assert payload["native_oracle"]["status"] == "not_run"
        assert payload["final_result"]["harness_f2p"] is True
        assert payload["final_result"]["is_f2p"] is True
        assert payload["final_result"]["native_f2p"] is None
        assert payload["final_result"]["verified_f2p"] is None


def test_run_swebench_one_native_oracle_runs_after_pipeline(tmp_path: Path, monkeypatch):
    instances_file = tmp_path / "instances.jsonl"
    instances_file.write_text(
        json.dumps(
            {
                "instance_id": "demo__repo-8",
                "repo": "demo/repo",
                "base_commit": "abc123",
                "problem_statement": "x",
                "patch": "y",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    order: list[str] = []
    crafted = _crafted_pipeline_result("patch_blind").model_copy(
        update={
            "validation": ValidationResult(
                success=True,
                buggy_status="reproduced",
                fixed_status="resolved",
                summary="ok",
            )
        }
    )
    monkeypatch.setattr(cli_module, "run_pipeline", lambda *a, **k: order.append("pipeline") or crafted)
    monkeypatch.setattr(
        cli_module,
        "run_native_oracle",
        lambda **kwargs: (
            order.append("native"),
            NativeOracleResult(
                mode=kwargs["mode"],
                framework="pytest",
                status="confirmed",
                target_tests=["tests/test_x.py::test_y"],
                native_f2p=True,
                reason="ok",
            ),
        )[1],
    )
    monkeypatch.setattr(
        cli_module,
        "prepare_swebench_repos",
        lambda instance, workdir, cache_dir=None: PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=Path("examples/mock_buggy_repo"),
            fixed_repo=Path("examples/mock_fixed_repo"),
            patch_applied=True,
        ),
    )

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-8",
                "--workdir",
                "repos",
                "--mock",
                "--native-oracle",
                "pytest",
            ],
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(Path("outputs/demo__repo-8/result.json").read_text(encoding="utf-8"))

        assert order == ["pipeline", "native"]
        assert payload["run_config"]["native_oracle"] == "pytest"
        assert payload["final_result"]["harness_f2p"] is True
        assert payload["final_result"]["native_f2p"] is True
        assert payload["final_result"]["verified_f2p"] is True


def test_run_swebench_one_native_oracle_makes_no_extra_llm_calls(tmp_path: Path, monkeypatch):
    counts: dict[str, int] = {}
    real_generate_harness = MockLLMClient.generate_harness
    real_extract_bug_spec = MockLLMClient.extract_bug_spec

    def counting_harness(self, concise_context):
        counts["harness"] = counts.get("harness", 0) + 1
        return real_generate_harness(self, concise_context)

    def counting_bug_spec(self, issue_text):
        counts["bug_spec"] = counts.get("bug_spec", 0) + 1
        return real_extract_bug_spec(self, issue_text)

    monkeypatch.setattr(MockLLMClient, "generate_harness", counting_harness)
    monkeypatch.setattr(MockLLMClient, "extract_bug_spec", counting_bug_spec)
    _fake_native_oracle_internals(monkeypatch)

    buggy_repo = tmp_path / "prepared" / "shared" / "buggy"
    fixed_repo = tmp_path / "prepared" / "shared" / "fixed"
    _make_mock_repo(buggy_repo, fixed=False)
    _make_mock_repo(fixed_repo, fixed=True)
    monkeypatch.setattr(
        cli_module,
        "prepare_swebench_repos",
        lambda instance, workdir, cache_dir=None: PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            patch_applied=True,
        ),
    )

    per_run: list[tuple[int, int]] = []
    for run_index, native_flag in enumerate(([], ["--native-oracle", "pytest"])):
        counts.clear()
        instance_id = f"demo__repo-llm-{run_index}"
        instances_file = tmp_path / f"instances_llm_{run_index}.jsonl"
        instances_file.write_text(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "repo": "demo/repo",
                    "base_commit": "abc123",
                    "problem_statement": "divide(a, b) should raise ZeroDivisionError when b is zero",
                    "patch": "unused in mocked prepare",
                    "FAIL_TO_PASS": '["tests/test_mod.py::test_value"]',
                    "test_patch": "--- a/tests/test_mod.py\n+++ b/tests/test_mod.py\n@@ -0,0 +1 @@\n+x = 1\n",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with runner.isolated_filesystem(temp_dir=str(tmp_path)):
            result = runner.invoke(
                app,
                [
                    "run-swebench-one",
                    "--instances-file",
                    str(instances_file),
                    "--instance-id",
                    instance_id,
                    "--workdir",
                    "repos",
                    "--mock",
                    *native_flag,
                ],
            )
            assert result.exit_code == 0, result.stdout
            payload = json.loads(
                Path(f"outputs/{instance_id}/result.json").read_text(encoding="utf-8")
            )
            if native_flag:
                assert payload["native_oracle"]["status"] == "confirmed"
        per_run.append((counts["harness"], counts["bug_spec"]))

    # the native oracle adds zero LLM traffic
    assert per_run[0] == per_run[1]
    assert per_run[1][0] > 0 and per_run[1][1] > 0


def test_run_swebench_one_native_oracle_sentinel_never_reaches_prompts(tmp_path: Path, monkeypatch):
    sentinel_testpatch = "SENTINEL_TESTPATCH_9f3a"
    sentinel_target = "SENTINEL_TARGET_9f3a"
    sentinel_native_out = "SENTINEL_NATIVE_OUT_9f3a"
    sentinels = {sentinel_testpatch, sentinel_target, sentinel_native_out}
    instance = {
        "instance_id": "demo__repo-sentinel",
        "repo": "demo/repo",
        "base_commit": "abc123",
        "problem_statement": "divide(a, b) should raise ZeroDivisionError when b is zero",
        "patch": "unused in mocked prepare",
        "FAIL_TO_PASS": f'["tests/test_{sentinel_target}.py::test_bug"]',
        "test_patch": (
            f"--- a/tests/test_{sentinel_target}.py\n"
            f"+++ b/tests/test_{sentinel_target}.py\n"
            "@@ -0,0 +1 @@\n"
            f"+# {sentinel_testpatch}\n"
        ),
    }
    instances_file = tmp_path / "instances.jsonl"
    instances_file.write_text(json.dumps(instance) + "\n", encoding="utf-8")

    buggy_repo = tmp_path / "prepared" / "sentinel" / "buggy"
    fixed_repo = tmp_path / "prepared" / "sentinel" / "fixed"
    _make_mock_repo(buggy_repo, fixed=False)
    _make_mock_repo(fixed_repo, fixed=True)
    monkeypatch.setattr(
        cli_module,
        "prepare_swebench_repos",
        lambda instance, workdir, cache_dir=None: PreparedRepos(
            instance_id=instance["instance_id"],
            repo=instance["repo"],
            base_commit=instance["base_commit"],
            buggy_repo=buggy_repo,
            fixed_repo=fixed_repo,
            patch_applied=True,
        ),
    )

    captured = {}
    real_run_pipeline = cli_module.run_pipeline

    def spy_pipeline(*args, **kwargs):
        captured["result"] = real_run_pipeline(*args, **kwargs)
        return captured["result"]

    monkeypatch.setattr(cli_module, "run_pipeline", spy_pipeline)

    def fake_apply(repo_dir: Path, test_patch: str):
        # positive control: the oracle really did receive the test_patch
        assert sentinel_testpatch in test_patch
        return subprocess.CompletedProcess(["patch"], 0, "", "")

    def fake_run(cmd, *, cwd, timeout, input_text=None):
        role = "buggy" if Path(cwd).name == "buggy_eval" else "fixed"
        spec = (
            {"tests": 1, "failures": 1, "returncode": 1}
            if role == "buggy"
            else {"tests": 1, "returncode": 0}
        )
        xml_index = next(i for i, part in enumerate(cmd) if part.startswith("--junitxml="))
        xml_path = Path(cmd[xml_index].split("=", 1)[1])
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(
            '<?xml version="1.0"?><testsuites><testsuite name="pytest" '
            f'tests="{spec["tests"]}" failures="{spec.get("failures", 0)}" '
            'errors="0" skipped="0"></testsuite></testsuites>',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, spec["returncode"], f"{sentinel_native_out} {role}\n", "")

    monkeypatch.setattr(native_oracle, "_apply_test_patch", fake_apply)
    monkeypatch.setattr(native_oracle, "_run_command", fake_run)

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-sentinel",
                "--workdir",
                "repos",
                "--mock",
                "--native-oracle",
                "pytest",
            ],
        )
        assert result.exit_code == 0, result.stdout

        out_dir = Path("outputs/demo__repo-sentinel")
        payload = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
        assert payload["native_oracle"]["status"] == "confirmed"

        # the CLI attaches the oracle to a copy of the pipeline result, so the
        # original object keeps its prompts untouched — check them directly
        pipeline_result = captured["result"]
        prompt_texts = [
            pipeline_result.bug_spec_prompt,
            pipeline_result.initial_harness_prompt,
            pipeline_result.concise_context,
        ] + [attempt.prompt_text for attempt in pipeline_result.attempts]
        for text in prompt_texts:
            for sentinel in sentinels:
                assert sentinel not in text

        for prompt_file in (out_dir / "prompts").glob("*.md"):
            for sentinel in sentinels:
                assert sentinel not in prompt_file.read_text(encoding="utf-8")
        for sentinel in sentinels:
            assert sentinel not in (out_dir / "concise_context.md").read_text(encoding="utf-8")

        # positive control: native outputs are recorded in result.json (audit),
        # they just must never reach any prompt
        assert sentinel_native_out in json.dumps(payload)


def test_run_swebench_one_rejects_bad_native_oracle_mode(tmp_path: Path):
    instances_file = tmp_path / "instances.jsonl"
    instances_file.write_text(
        json.dumps(
            {
                "instance_id": "demo__repo-bad",
                "repo": "demo/repo",
                "base_commit": "abc123",
                "problem_statement": "x",
                "patch": "y",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-bad",
                "--workdir",
                "repos",
                "--mock",
                "--native-oracle",
                "django",
            ],
        )
        assert result.exit_code != 0
        # BadParameter usage errors are reported on stderr (mixed into output)
        assert "native_oracle must be one of" in result.output


def test_run_swebench_one_rejects_zero_native_timeout(tmp_path: Path):
    instances_file = tmp_path / "instances.jsonl"
    instances_file.write_text(
        json.dumps(
            {
                "instance_id": "demo__repo-timeout",
                "repo": "demo/repo",
                "base_commit": "abc123",
                "problem_statement": "x",
                "patch": "y",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            app,
            [
                "run-swebench-one",
                "--instances-file",
                str(instances_file),
                "--instance-id",
                "demo__repo-timeout",
                "--workdir",
                "repos",
                "--mock",
                "--native-test-timeout",
                "0",
            ],
        )
        assert result.exit_code != 0
