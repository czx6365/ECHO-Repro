from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable

DEFAULT_INPUT_PATH = Path("data/swebench_lite.jsonl")
DEFAULT_OUTPUT_PATH = Path("data/swebench_lite_small.jsonl")
DEFAULT_REQUIRED_IDS = ("astropy__astropy-12907",)


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_jsonl(rows: Iterable[dict], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def select_diverse_instances(
    instances: list[dict],
    sample_size: int,
    required_ids: Iterable[str] = DEFAULT_REQUIRED_IDS,
) -> list[dict]:
    by_id = {str(instance.get("instance_id", "")): instance for instance in instances}
    selected: list[dict] = []
    selected_ids: set[str] = set()

    for instance_id in required_ids:
        if instance_id in by_id and instance_id not in selected_ids:
            selected.append(by_id[instance_id])
            selected_ids.add(instance_id)
        if len(selected) >= sample_size:
            return selected

    grouped: dict[str, list[dict]] = defaultdict(list)
    for instance in instances:
        grouped[str(instance.get("repo", ""))].append(instance)

    for repo_instances in grouped.values():
        repo_instances.sort(key=lambda instance: str(instance.get("instance_id", "")))

    repo_order = sorted(grouped, key=lambda repo: (-len(grouped[repo]), repo))
    while len(selected) < sample_size:
        added_this_round = False
        for repo in repo_order:
            while grouped[repo] and str(grouped[repo][0].get("instance_id", "")) in selected_ids:
                grouped[repo].pop(0)
            if not grouped[repo]:
                continue
            instance = grouped[repo].pop(0)
            selected.append(instance)
            selected_ids.add(str(instance.get("instance_id", "")))
            added_this_round = True
            if len(selected) >= sample_size:
                break
        if not added_this_round:
            break

    return selected


def parse_required_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a deterministic small SWE-bench Lite sample.")
    parser.add_argument("--instances-file", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output-file", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--required-ids", default=",".join(DEFAULT_REQUIRED_IDS))
    args = parser.parse_args()

    instances = load_jsonl(args.instances_file)
    sample = select_diverse_instances(
        instances,
        sample_size=args.sample_size,
        required_ids=parse_required_ids(args.required_ids),
    )
    write_jsonl(sample, args.output_file)

    print(f"Loaded {len(instances)} instances from {args.instances_file}")
    print(f"Selected {len(sample)} instances into {args.output_file}")
    if len(sample) < args.sample_size:
        print(f"Warning: requested {args.sample_size} instances but only {len(sample)} were available.")
    for instance in sample:
        print(f"{instance.get('instance_id')}\\t{instance.get('repo')}")


if __name__ == "__main__":
    main()
