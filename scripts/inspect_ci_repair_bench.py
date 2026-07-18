from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from echo_ci.ci_repair_loader import DEFAULT_HF_NAME, inspect_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect CI-Repair-Bench schema.")
    parser.add_argument("--dataset_path", type=Path, default=None, help="Local JSON/JSONL/CSV dataset path.")
    parser.add_argument("--hf_name", default=DEFAULT_HF_NAME, help="HuggingFace dataset name.")
    parser.add_argument("--split", default=None, help="Optional dataset split.")
    parser.add_argument("--max_examples", type=int, default=3)
    args = parser.parse_args()

    report = inspect_dataset(
        dataset_path=args.dataset_path,
        hf_name=args.hf_name,
        split=args.split,
        max_examples=args.max_examples,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

