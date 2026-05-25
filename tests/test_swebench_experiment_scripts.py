import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_script_module(script_name: str):
    script_path = Path("scripts") / script_name
    spec = spec_from_file_location(script_name.removesuffix(".py"), script_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_create_swebench_sample_is_deterministic_and_keeps_required_id():
    module = _load_script_module("create_swebench_sample.py")
    instances = [
        {"instance_id": "django__django-1", "repo": "django/django"},
        {"instance_id": "astropy__astropy-12907", "repo": "astropy/astropy"},
        {"instance_id": "django__django-2", "repo": "django/django"},
        {"instance_id": "sympy__sympy-1", "repo": "sympy/sympy"},
    ]

    sample = module.select_diverse_instances(instances, sample_size=3)

    assert [row["instance_id"] for row in sample] == [
        "astropy__astropy-12907",
        "django__django-1",
        "sympy__sympy-1",
    ]


def test_summarize_swebench_experiment_handles_missing_and_existing_results(tmp_path: Path):
    module = _load_script_module("summarize_swebench_experiment.py")
    instances = [
        {"instance_id": "demo__repo-1", "repo": "demo/repo"},
        {"instance_id": "demo__repo-2", "repo": "demo/repo"},
    ]
    result_dir = tmp_path / "outputs" / "demo__repo-1"
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "instance_metadata": {
                    "repo": "demo/repo",
                    "repo_validated": True,
                    "patch_applied": True,
                },
                "environment_repairs": [{"success": True}],
                "attempts_summary": [
                    {"llm_metadata": {"total_tokens": 10}},
                    {"llm_metadata": {"total_tokens": 15}},
                ],
                "final_result": {
                    "buggy_status": "reproduced",
                    "fixed_status": "resolved",
                    "failure_category": None,
                },
            }
        ),
        encoding="utf-8",
    )

    rows = [module.summarize_instance(instance, tmp_path / "outputs") for instance in instances]

    assert rows[0]["prepared?"] == "yes"
    assert rows[0]["env ready?"] == "yes"
    assert rows[0]["reproduced?"] == "yes"
    assert rows[0]["fixed passed?"] == "yes"
    assert rows[0]["cost"] == "25"
    assert rows[0]["attempts"] == "2"
    assert rows[1]["prepared?"] == "pending"
    assert rows[1]["failure category"] == "not_run"


def test_summarize_swebench_experiment_marks_legacy_results_as_unknown(tmp_path: Path):
    module = _load_script_module("summarize_swebench_experiment.py")
    result_dir = tmp_path / "outputs" / "legacy__repo-1"
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "instance_metadata": {
                    "repo": "legacy/repo",
                    "patch_applied": True,
                },
                "attempts_summary": [],
                "final_result": {
                    "buggy_status": "import_error",
                    "fixed_status": None,
                    "failure_category": "import_error",
                },
            }
        ),
        encoding="utf-8",
    )

    row = module.summarize_instance(
        {"instance_id": "legacy__repo-1", "repo": "legacy/repo"},
        tmp_path / "outputs",
    )

    assert row["prepared?"] == "unknown"
    assert row["env ready?"] == "no"


def test_summarize_swebench_experiment_leaves_env_pending_for_repo_error(tmp_path: Path):
    module = _load_script_module("summarize_swebench_experiment.py")
    result_dir = tmp_path / "outputs" / "demo__repo-error"
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(
        json.dumps(
            {
                "instance_metadata": {
                    "repo": "demo/repo",
                    "repo_validated": False,
                    "patch_applied": False,
                },
                "attempts_summary": [],
                "final_result": {
                    "buggy_status": "repo_error",
                    "fixed_status": None,
                    "failure_category": "repo_error",
                },
            }
        ),
        encoding="utf-8",
    )

    row = module.summarize_instance(
        {"instance_id": "demo__repo-error", "repo": "demo/repo"},
        tmp_path / "outputs",
    )

    assert row["prepared?"] == "no"
    assert row["env ready?"] == "pending"
