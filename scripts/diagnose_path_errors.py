from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from echo_ci.metrics import load_results


PATH_PATTERNS = re.compile(
    r"(can't open file|cannot open file|no such file|file not found|filenotfounderror|not a git repository|cd: )",
    re.IGNORECASE,
)


def _phase_rows(payload: dict) -> list[tuple[str, dict]]:
    execution = payload.get("execution") or payload
    rows = []
    for phase in ("failing", "fixed"):
        value = execution.get(phase)
        if isinstance(value, dict):
            rows.append((phase, value))
    return rows


def _signal(result: dict) -> str:
    text = "\n".join(str(result.get(key) or "") for key in ("stderr", "stdout", "error", "checkout_error"))
    for line in text.splitlines():
        if PATH_PATTERNS.search(line):
            return line.strip()[:240]
    return text.strip().splitlines()[0][:240] if text.strip() else ""


def _likely_cause(result: dict, generated_dir: Path) -> str:
    preflight = result.get("preflight") or {}
    signal = _signal(result).lower()
    text = "\n".join(str(result.get(key) or "") for key in ("stderr", "stdout", "error", "checkout_error")).lower()
    run_content = str(preflight.get("run_sh_content") or "")
    generated_reproduce = generated_dir / "reproduce.py"
    generated_run = generated_dir / "run.sh"

    if not generated_reproduce.exists() or not generated_run.exists():
        return "missing_generated_artifact"
    if preflight.get("echo_repro_dir_exists") is False:
        return "missing_echo_repro_dir"
    if preflight.get("reproduce_py_exists") is False:
        return "missing_reproduce_py"
    if preflight.get("run_sh_exists") is False:
        return "missing_run_sh"
    if "can't open file" in signal and "reproduce.py" in signal and preflight.get("reproduce_py_exists"):
        return "wrong_run_sh_path"
    if "not a git repository" in signal or "cd: " in signal:
        return "wrong_working_directory"
    if re.search(r"/(?:work|outputs|failing_repo|fixed_repo|ci_repair)", run_content):
        return "hardcoded_failing_path"
    dependency_markers = (
        "no module named",
        "command not found",
        "python: no such file",
        "poetry: command not found",
        "pre-commit: command not found",
        ".venv/bin/activate",
        "venv/bin/activate",
    )
    if any(marker in text for marker in dependency_markers):
        return "dependency_or_environment_missing"
    missing_binary = re.search(r"no such file or directory: '([^']+)'", text)
    if missing_binary and "/" not in missing_binary.group(1) and "." not in missing_binary.group(1):
        return "dependency_or_environment_missing"
    if re.search(r"(no such file|filenotfounderror|file not found)", signal) and ".echo_repro" not in signal:
        return "missing_project_target_file"
    return "unknown_path_error"


def markdown(rows: list[dict[str, str]]) -> str:
    columns = [
        "instance",
        "setting",
        "phase",
        "generated_reproduce_exists",
        "generated_run_exists",
        "fixed_echo_repro_exists",
        "fixed_reproduce_exists",
        "fixed_run_exists",
        "stderr_key_signal",
        "likely_cause",
    ]
    lines = [
        "# Path Error Diagnosis",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        values = [str(row.get(column, "")).replace("\n", " ")[:220] for column in columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose path errors in CI-Repair MVP outputs.")
    parser.add_argument("--result_dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows: list[dict[str, str]] = []
    for payload in load_results(args.result_dir):
        classification = str(payload.get("classification") or (payload.get("execution") or {}).get("classification") or "")
        result_path = Path(payload.get("_path", ""))
        if not result_path:
            continue
        generated_dir = result_path.parent / "generated"
        include_payload = classification == "fixed_harness_path_error" or classification.startswith("fixed_")
        for phase, result in _phase_rows(payload):
            signal = _signal(result)
            if not include_payload and not PATH_PATTERNS.search(signal):
                continue
            preflight = result.get("preflight") or {}
            if phase != "fixed" and classification.startswith("fixed_"):
                continue
            rows.append(
                {
                    "instance": str(payload.get("instance_id", "")),
                    "setting": str(payload.get("setting", result_path.parent.name)),
                    "phase": phase,
                    "generated_reproduce_exists": str((generated_dir / "reproduce.py").exists()).lower(),
                    "generated_run_exists": str((generated_dir / "run.sh").exists()).lower(),
                    "fixed_echo_repro_exists": str(preflight.get("echo_repro_dir_exists", "")).lower(),
                    "fixed_reproduce_exists": str(preflight.get("reproduce_py_exists", "")).lower(),
                    "fixed_run_exists": str(preflight.get("run_sh_exists", "")).lower(),
                    "stderr_key_signal": signal,
                    "likely_cause": _likely_cause(result, generated_dir),
                }
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown(rows), encoding="utf-8")
    print(json.dumps({"diagnosed": len(rows), "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
