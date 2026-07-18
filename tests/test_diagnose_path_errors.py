import json
import subprocess
from pathlib import Path


def test_diagnose_path_errors_writes_markdown(tmp_path: Path):
    result_dir = tmp_path / "outputs"
    case_dir = result_dir / "demo" / "S1_issue_only"
    generated = case_dir / "generated"
    generated.mkdir(parents=True)
    (generated / "reproduce.py").write_text("print('x')\n", encoding="utf-8")
    (generated / "run.sh").write_text("python3 .echo_repro/reproduce.py\n", encoding="utf-8")
    (generated / "oracle.json").write_text("{}", encoding="utf-8")
    payload = {
        "instance_id": "demo",
        "setting": "S1_issue_only",
        "classification": "fixed_harness_path_error",
        "execution": {
            "classification": "fixed_harness_path_error",
            "fixed": {
                "stderr": "python3: can't open file '.echo_repro/reproduce.py': [Errno 2] No such file or directory",
                "preflight": {
                    "echo_repro_dir_exists": True,
                    "run_sh_exists": True,
                    "reproduce_py_exists": True,
                    "run_sh_content": "python3 .echo_repro/reproduce.py",
                },
            },
        },
    }
    (case_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "diagnosis.md"

    result = subprocess.run(
        [
            ".venv/bin/python",
            "scripts/diagnose_path_errors.py",
            "--result_dir",
            str(result_dir),
            "--output",
            str(output),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0
    text = output.read_text(encoding="utf-8")
    assert "Path Error Diagnosis" in text
    assert "wrong_run_sh_path" in text

