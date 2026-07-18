from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from echo_ci.metrics import summarize_result_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize CI-Repair MVP result.json files.")
    parser.add_argument("--result_dir", type=Path, default=Path("outputs/ci_repair_mvp"))
    parser.add_argument("--output_dir", type=Path, default=None)
    args = parser.parse_args()

    summary = summarize_result_dir(args.result_dir, output_dir=args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Wrote summary to {args.output_dir or args.result_dir}")


if __name__ == "__main__":
    main()

