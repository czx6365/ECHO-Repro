from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import echo_repro.native_oracle as native_oracle
from echo_repro.models import (
    BugSpec,
    EnvironmentProfileResult,
    ExecutionResult,
    HarnessCandidate,
    PipelineResult,
    PreparedRepos,
    RetrievedContext,
    ValidationResult,
)
from echo_repro.native_oracle import is_pytest_target, parse_fail_to_pass, run_native_oracle

TARGETS = ["tests/test_mod.py::test_value"]
TEST_PATCH = (
    "--- a/tests/test_mod.py\n"
    "+++ b/tests/test_mod.py\n"
    "@@ -0,0 +1,4 @@\n"
    "+import mod\n"
    "+\n"
    "+def test_value():\n"
    "+    assert mod.VALUE == 1\n"
)


def _prepared(tmp_path: Path) -> PreparedRepos:
    buggy = tmp_path / "buggy"
    fixed = tmp_path / "fixed"
    for repo, value in ((buggy, "0"), (fixed, "1")):
        (repo / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "mod.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
        (repo / "tests" / "test_mod.py").write_text("", encoding="utf-8")
    return PreparedRepos(
        instance_id="demo__repo-n",
        repo="demo/repo",
        base_commit="abc123",
        buggy_repo=buggy,
        fixed_repo=fixed,
        patch_applied=True,
    )


def _instance(fail_to_pass=TARGETS, test_patch: str = TEST_PATCH) -> dict:
    return {
        "instance_id": "demo__repo-n",
        "repo": "demo/repo",
        "base_commit": "abc123",
        "problem_statement": "mod should do the right thing",
        "patch": "",
        "FAIL_TO_PASS": fail_to_pass,
        "test_patch": test_patch,
    }


def _pipeline_result(env_python: str | None = None) -> PipelineResult:
    return PipelineResult(
        bug_spec=BugSpec(
            title="t",
            summary="s",
            current_behavior="c",
            expected_behavior="e",
            failure_signature="f",
        ),
        retrieved_context=RetrievedContext(repo_path=Path(".")),
        concise_context="ctx",
        harness_candidate=HarnessCandidate(code="pass\n"),
        buggy_execution=ExecutionResult(repo_path=Path("."), command="x", returncode=0, stdout="", stderr=""),
        validation=ValidationResult(success=True, buggy_status="reproduced", summary="ok"),
        environment_profile=(
            EnvironmentProfileResult(ready=True, python_path=Path(env_python)) if env_python else None
        ),
    )


def _junit_xml(tests: int, failures: int, errors: int, skipped: int) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" tests="{tests}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}"></testsuite></testsuites>'
    )


class _FakeRunner:
    """Replaces native_oracle._run_command and _apply_test_patch with scripted
    outcomes; no real pytest/patch process ever runs."""

    def __init__(self, monkeypatch, buggy: dict, fixed: dict | None, *, real_patch: bool = False):
        self.patch_calls: list[Path] = []
        self.pytest_calls: list[tuple[str, list[str], Path]] = []
        self.patched_contents: list[tuple[str, str]] = []
        self.buggy = buggy
        self.fixed = fixed
        self.real_patch = real_patch
        monkeypatch.setattr(native_oracle, "_run_command", self._run)
        monkeypatch.setattr(native_oracle, "_apply_test_patch", self._apply_patch)

    def _apply_patch(self, repo_dir: Path, test_patch: str):
        self.patch_calls.append(repo_dir)
        if self.real_patch:
            return subprocess.run(
                ["patch", "-p1", "--batch", "--forward"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                input=test_patch,
                timeout=120,
                shell=False,
            )
        (repo_dir / "PATCHED.txt").write_text("applied\n", encoding="utf-8")
        return subprocess.CompletedProcess(["patch"], 0, "", "")

    def _run(self, cmd, *, cwd, timeout, input_text=None):
        role = "buggy" if Path(cwd).name == "buggy_eval" else "fixed"
        self.pytest_calls.append((role, list(cmd), Path(cwd)))
        spec = self.buggy if role == "buggy" else self.fixed
        assert spec is not None, f"unexpected pytest run for {role}"
        if self.real_patch:
            self.patched_contents.append(
                (role, (Path(cwd) / "tests" / "test_mod.py").read_text(encoding="utf-8"))
            )
        if spec.get("timeout"):
            raise subprocess.TimeoutExpired(cmd, timeout)
        xml_index = next(i for i, part in enumerate(cmd) if part.startswith("--junitxml="))
        xml_path = Path(cmd[xml_index].split("=", 1)[1])
        if not spec.get("no_xml"):
            xml_path.parent.mkdir(parents=True, exist_ok=True)
            if spec.get("corrupt_xml"):
                xml_path.write_text("not xml at all", encoding="utf-8")
            else:
                xml_path.write_text(
                    _junit_xml(
                        tests=spec.get("tests", 1),
                        failures=spec.get("failures", 0),
                        errors=spec.get("errors", 0),
                        skipped=spec.get("skipped", 0),
                    ),
                    encoding="utf-8",
                )
        return subprocess.CompletedProcess(
            cmd,
            spec.get("returncode", 0),
            spec.get("stdout", ""),
            spec.get("stderr", ""),
        )


# --- FAIL_TO_PASS parsing ---------------------------------------------------


def test_parse_fail_to_pass_json_string():
    assert parse_fail_to_pass('["a.py::test_x", "b.py::test_y"]') == ["a.py::test_x", "b.py::test_y"]


def test_parse_fail_to_pass_parsed_list():
    assert parse_fail_to_pass(["a.py::test_x"]) == ["a.py::test_x"]
    assert parse_fail_to_pass(["  a.py::test_x  "]) == ["a.py::test_x"]


def test_parse_fail_to_pass_empty_and_invalid():
    for value in (None, "", "   ", "[]", "not-json", "[", 42, {"a": 1}):
        assert parse_fail_to_pass(value) is None


def test_is_pytest_target_variants():
    assert is_pytest_target("tests/test_mod.py::test_value")
    assert is_pytest_target("tests/test_mod.py::TestClass::test_value")
    assert is_pytest_target("tests/test_mod.py")
    assert is_pytest_target("tests/test_mod.py::test_value[case-1]")
    # Django unittest labels (P1-B) must not be mistaken for pytest targets
    assert not is_pytest_target("test_name (test_utils.tests.SomeClass)")
    assert not is_pytest_target("test_utils.tests.SomeClass.test_name")
    assert not is_pytest_target("app.tests:TestCase.test_name")


# --- mode off / invalid mode ------------------------------------------------


def test_mode_off_returns_not_run(tmp_path: Path):
    result = run_native_oracle(
        instance={},
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="off",
    )
    assert result.mode == "off"
    assert result.status == "not_run"
    assert result.native_f2p is None


def test_unknown_mode_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        run_native_oracle(
            instance={},
            prepared=_prepared(tmp_path),
            pipeline_result=_pipeline_result(),
            mode="django",
        )


# --- unsupported targets ----------------------------------------------------


def test_invalid_or_empty_fail_to_pass_is_unsupported(monkeypatch, tmp_path: Path):
    def boom(*args, **kwargs):
        raise AssertionError("no subprocess may run")

    monkeypatch.setattr(native_oracle, "_run_command", boom)
    monkeypatch.setattr(native_oracle, "_apply_test_patch", boom)
    for fail_to_pass in (None, "", "[]", "[broken"):
        instance = _instance(fail_to_pass=fail_to_pass)
        result = run_native_oracle(
            instance=instance,
            prepared=_prepared(tmp_path),
            pipeline_result=_pipeline_result(),
            mode="pytest",
        )
        assert result.status == "unsupported"
        assert result.native_f2p is None


def test_django_labels_are_unsupported_without_subprocess(monkeypatch, tmp_path: Path):
    def boom(*args, **kwargs):
        raise AssertionError("no subprocess may run")

    monkeypatch.setattr(native_oracle, "_run_command", boom)
    monkeypatch.setattr(native_oracle, "_apply_test_patch", boom)
    for fail_to_pass in (
        '["test_name (test_utils.tests.SomeClass)"]',
        ["test_utils.tests.SomeClass.test_name"],
        ["tests/test_mod.py::test_x", "other (module.Class)"],
    ):
        result = run_native_oracle(
            instance=_instance(fail_to_pass=fail_to_pass),
            prepared=_prepared(tmp_path),
            pipeline_result=_pipeline_result(),
            mode="pytest",
        )
        assert result.status == "unsupported"
        assert result.native_f2p is None


# --- isolated copies + patch application ------------------------------------


def test_test_patch_applied_to_two_isolated_copies_with_real_patch(monkeypatch, tmp_path: Path):
    prepared = _prepared(tmp_path)
    fake = _FakeRunner(
        monkeypatch,
        buggy={"tests": 1, "failures": 1, "returncode": 1},
        fixed={"tests": 1, "returncode": 0},
        real_patch=True,
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=prepared,
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )

    assert result.status == "confirmed"
    assert result.native_f2p is True
    assert result.test_patch_applied_buggy is True
    assert result.test_patch_applied_fixed is True
    # both isolated copies live under the oracle's TemporaryDirectory
    assert len(fake.patch_calls) == 2
    assert fake.patch_calls[0] != fake.patch_calls[1]
    for copy_dir in fake.patch_calls:
        assert "echo_native_oracle_" in str(copy_dir)
        assert not str(copy_dir).startswith(str(prepared.buggy_repo))
        assert not str(copy_dir).startswith(str(prepared.fixed_repo))
    # the real patch command really applied the diff to both copies
    assert [role for role, _ in fake.patched_contents] == ["buggy", "fixed"]
    for _, content in fake.patched_contents:
        assert "assert mod.VALUE == 1" in content


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_prepared_repos_unchanged_after_native_run(monkeypatch, tmp_path: Path):
    prepared = _prepared(tmp_path)
    buggy_before = _snapshot(prepared.buggy_repo)
    fixed_before = _snapshot(prepared.fixed_repo)
    _FakeRunner(
        monkeypatch,
        buggy={"tests": 1, "failures": 1, "returncode": 1},
        fixed={"tests": 1, "returncode": 0},
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=prepared,
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "confirmed"
    assert _snapshot(prepared.buggy_repo) == buggy_before
    assert _snapshot(prepared.fixed_repo) == fixed_before


def test_test_patch_apply_failure_yields_setup_error(monkeypatch, tmp_path: Path):
    for failing_side in ("buggy", "fixed"):
        def fake_apply(repo_dir: Path, test_patch: str, side=failing_side):
            marker = Path(repo_dir) / "PATCHED.txt"
            marker.write_text("applied\n", encoding="utf-8")
            if (Path(repo_dir).name == "buggy_eval") == (side == "buggy"):
                return subprocess.CompletedProcess(["patch"], 1, "", f"rejected on {side}")
            return subprocess.CompletedProcess(["patch"], 0, "", "")

        def boom(*args, **kwargs):
            raise AssertionError("pytest must not run when test_patch fails")

        monkeypatch.setattr(native_oracle, "_apply_test_patch", fake_apply)
        monkeypatch.setattr(native_oracle, "_run_command", boom)
        result = run_native_oracle(
            instance=_instance(),
            prepared=_prepared(tmp_path),
            pipeline_result=_pipeline_result(),
            mode="pytest",
        )
        assert result.status == "setup_error"
        assert result.native_f2p is None
        # the fixed side only ever gets patched after the buggy side succeeded
        assert result.test_patch_applied_buggy == (failing_side != "buggy")
        assert result.test_patch_applied_fixed is False
        assert failing_side in result.reason


# --- buggy judgments --------------------------------------------------------


def test_buggy_fails_and_fixed_passes_confirms_f2p(monkeypatch, tmp_path: Path):
    fake = _FakeRunner(
        monkeypatch,
        buggy={"tests": 2, "failures": 2, "returncode": 1},
        fixed={"tests": 2, "returncode": 0},
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "confirmed"
    assert result.native_f2p is True
    assert result.buggy_execution is not None and result.buggy_execution.counts.tests == 2
    assert result.fixed_execution is not None and result.fixed_execution.counts.failures == 0
    assert [role for role, _, _ in fake.pytest_calls] == ["buggy", "fixed"]


def test_buggy_passes_yields_buggy_not_failed_and_fixed_not_run(monkeypatch, tmp_path: Path):
    fake = _FakeRunner(
        monkeypatch,
        buggy={"tests": 1, "returncode": 0},
        fixed=None,
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "buggy_not_failed"
    assert result.native_f2p is False
    assert result.fixed_execution is None
    assert len(fake.pytest_calls) == 1


def test_buggy_partial_failures_not_confirmed(monkeypatch, tmp_path: Path):
    _FakeRunner(
        monkeypatch,
        buggy={"tests": 2, "failures": 1, "returncode": 1},
        fixed=None,
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "buggy_not_failed"
    assert result.native_f2p is False


def test_buggy_import_error_is_collection_error_not_failure(monkeypatch, tmp_path: Path):
    fake = _FakeRunner(
        monkeypatch,
        buggy={
            "tests": 1,
            "errors": 1,
            "returncode": 1,
            "stderr": "ModuleNotFoundError: No module named 'numpy'",
        },
        fixed=None,
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "collection_error"
    assert result.native_f2p is None
    assert len(fake.pytest_calls) == 1


def test_buggy_skipped_tests_not_confirmed(monkeypatch, tmp_path: Path):
    _FakeRunner(
        monkeypatch,
        buggy={"tests": 2, "failures": 1, "skipped": 1, "returncode": 1},
        fixed=None,
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "buggy_not_failed"
    assert result.native_f2p is False


# --- fixed judgments --------------------------------------------------------


def test_fixed_failure_yields_fixed_not_passed(monkeypatch, tmp_path: Path):
    fake = _FakeRunner(
        monkeypatch,
        buggy={"tests": 1, "failures": 1, "returncode": 1},
        fixed={"tests": 1, "failures": 1, "returncode": 1},
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "fixed_not_passed"
    assert result.native_f2p is False
    assert len(fake.pytest_calls) == 2


def test_fixed_skipped_tests_not_confirmed(monkeypatch, tmp_path: Path):
    _FakeRunner(
        monkeypatch,
        buggy={"tests": 1, "failures": 1, "returncode": 1},
        fixed={"tests": 2, "skipped": 1, "returncode": 0},
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "fixed_not_passed"
    assert result.native_f2p is False


# --- timeouts ---------------------------------------------------------------


def test_buggy_timeout_yields_timeout_and_fixed_not_run(monkeypatch, tmp_path: Path):
    fake = _FakeRunner(monkeypatch, buggy={"timeout": True}, fixed=None)
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
        timeout_seconds=30,
    )
    assert result.status == "timeout"
    assert result.native_f2p is None
    assert result.buggy_execution is not None and result.buggy_execution.timed_out is True
    assert result.fixed_execution is None
    assert len(fake.pytest_calls) == 1


def test_fixed_timeout_yields_timeout(monkeypatch, tmp_path: Path):
    fake = _FakeRunner(
        monkeypatch,
        buggy={"tests": 1, "failures": 1, "returncode": 1},
        fixed={"timeout": True},
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "timeout"
    assert result.native_f2p is None
    assert result.fixed_execution is not None and result.fixed_execution.timed_out is True
    assert len(fake.pytest_calls) == 2


# --- JUnit XML problems -----------------------------------------------------


def test_missing_junit_xml_yields_collection_error(monkeypatch, tmp_path: Path):
    _FakeRunner(
        monkeypatch,
        buggy={"no_xml": True, "returncode": 1},
        fixed=None,
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "collection_error"
    assert result.native_f2p is None


def test_corrupt_junit_xml_yields_collection_error(monkeypatch, tmp_path: Path):
    _FakeRunner(
        monkeypatch,
        buggy={"corrupt_xml": True, "returncode": 1},
        fixed=None,
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "collection_error"
    assert result.native_f2p is None


def test_no_tests_collected_yields_collection_error(monkeypatch, tmp_path: Path):
    _FakeRunner(
        monkeypatch,
        buggy={"tests": 0, "returncode": 5},
        fixed=None,
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "collection_error"
    assert result.native_f2p is None


# --- interpreter selection --------------------------------------------------


def test_uses_environment_profile_python(monkeypatch, tmp_path: Path):
    fake_python = "/opt/envs/demo/bin/python"
    fake = _FakeRunner(
        monkeypatch,
        buggy={"tests": 1, "failures": 1, "returncode": 1},
        fixed={"tests": 1, "returncode": 0},
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(env_python=fake_python),
        mode="pytest",
    )
    assert result.status == "confirmed"
    assert all(cmd[0] == fake_python for _, cmd, _ in fake.pytest_calls)


def test_falls_back_to_current_interpreter_without_ready_profile(monkeypatch, tmp_path: Path):
    fake = _FakeRunner(
        monkeypatch,
        buggy={"tests": 1, "failures": 1, "returncode": 1},
        fixed={"tests": 1, "returncode": 0},
    )
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "confirmed"
    assert all(cmd[0] == sys.executable for _, cmd, _ in fake.pytest_calls)


# --- subprocess discipline --------------------------------------------------


def test_commands_are_lists_without_shell_and_targets_after_double_dash(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(cmd, *, cwd=None, capture_output=False, text=False, input=None, timeout=None, shell=False):
        calls.append({"cmd": cmd, "cwd": cwd, "shell": shell, "input": input, "timeout": timeout})
        if cmd[0] == "patch":
            return subprocess.CompletedProcess(cmd, 0, "", "")
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
            _junit_xml(
                tests=spec["tests"],
                failures=spec.get("failures", 0),
                errors=0,
                skipped=0,
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, spec["returncode"], "", "")

    monkeypatch.setattr(native_oracle.subprocess, "run", fake_run)
    result = run_native_oracle(
        instance=_instance(),
        prepared=_prepared(tmp_path),
        pipeline_result=_pipeline_result(),
        mode="pytest",
    )
    assert result.status == "confirmed"

    for call in calls:
        assert isinstance(call["cmd"], list)
        assert call["shell"] is False
    pytest_calls = [call for call in calls if call["cmd"][1:3] == ["-m", "pytest"]]
    assert len(pytest_calls) == 2
    for call in pytest_calls:
        cmd = call["cmd"]
        assert "--" in cmd
        assert cmd[cmd.index("--") + 1 :] == TARGETS
        assert any(part.startswith("--junitxml=") for part in cmd)
