from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from echo_ci.ci_repair_loader import DEFAULT_HF_NAME, load_instances, sample_subset, write_jsonl


def parse_failure_types(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a deterministic CI-Repair-Bench subset.")
    parser.add_argument("--dataset_path", type=Path, default=None, help="Local JSON/JSONL/CSV dataset path.")
    parser.add_argument("--hf_name", default=DEFAULT_HF_NAME, help="HuggingFace dataset name.")
    parser.add_argument("--split", default=None, help="Optional dataset split.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--failure_types",
        default="test,lint,dependency,build,format,workflow",
        help="Comma-separated failure types. If schema has no matching type, falls back to random sample.",
    )
    parser.add_argument("--output", type=Path, default=Path("data/ci_repair_subset_50.jsonl"))
    args = parser.parse_args()

    instances = load_instances(dataset_path=args.dataset_path, hf_name=args.hf_name, split=args.split)
    subset = sample_subset(
        instances,
        limit=args.limit,
        seed=args.seed,
        failure_types=parse_failure_types(args.failure_types),
    )
    write_jsonl(subset, args.output)
    print(f"Loaded {len(instances)} instances")
    print(f"Wrote {len(subset)} instances to {args.output}")


if __name__ == "__main__":
    main()

