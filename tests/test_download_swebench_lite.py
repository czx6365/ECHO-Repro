from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from echo_repro.swebench_adapter import load_instances


def _load_script_module():
    script_path = Path("scripts/download_swebench_lite.py")
    spec = spec_from_file_location("download_swebench_lite", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_save_instances_to_jsonl_round_trips_fake_fixture(tmp_path: Path):
    fixture_path = Path("tests/fixtures/fake_swebench_lite.jsonl")
    fixture_instances = load_instances(fixture_path)
    module = _load_script_module()

    output_path = tmp_path / "swebench_lite.jsonl"
    saved_rows = module.save_instances_to_jsonl(fixture_instances, output_path)
    round_tripped = load_instances(output_path)

    assert len(saved_rows) == 2
    assert len(round_tripped) == 2
    assert round_tripped[0]["instance_id"] == "django__django-10001"
    assert module.preview_instance_ids(round_tripped) == [
        "django__django-10001",
        "pallets__flask-10002",
    ]
