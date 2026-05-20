from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

DEFAULT_OUTPUT_PATH = Path("data/swebench_lite.jsonl")


def fetch_swebench_lite(split: str = "test"):
    from datasets import load_dataset

    return load_dataset("SWE-bench/SWE-bench_Lite", split=split)


def save_instances_to_jsonl(instances: Iterable[dict], output_path: Path) -> list[dict]:
    rows = [dict(instance) for instance in instances]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return rows


def preview_instance_ids(instances: list[dict], limit: int = 5) -> list[str]:
    return [str(instance.get("instance_id", "")) for instance in instances[:limit]]


def main() -> None:
    instances = fetch_swebench_lite(split="test")
    rows = save_instances_to_jsonl(instances, DEFAULT_OUTPUT_PATH)
    print(f"Saved {len(rows)} instances to {DEFAULT_OUTPUT_PATH}")
    print(f"Total instances: {len(rows)}")
    print("First 5 instance_id values:")
    for instance_id in preview_instance_ids(rows, limit=5):
        print(instance_id)


if __name__ == "__main__":
    main()
