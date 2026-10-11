#!/usr/bin/env python
"""Compute the era-rerun instance list fresh from outputs/*/result.json.

Includes every instance whose final_result is an environment/dependency
failure or a non-F2P partial (resolved/reproduced). Excludes ids listed in
--exclude-file (e.g. the pass-6 remainder that already ran with the fix).
Prints a comma-separated list on stdout and the count on stderr.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_result(outputs_dir: Path, instance_id: str) -> dict | None:
    path = outputs_dir / instance_id / "result.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs", type=Path, default=ROOT / "outputs")
    parser.add_argument("--exclude-file", type=Path, default=None)
    args = parser.parse_args()

    exclude = set()
    if args.exclude_file and args.exclude_file.exists():
        exclude = {line.strip() for line in args.exclude_file.read_text().split() if line.strip()}

    rerun: list[str] = []
    for result_path in sorted(args.outputs.glob("*/result.json")):
        instance_id = result_path.parent.name
        if not instance_id or instance_id.startswith("_"):
            continue
        if instance_id in exclude:
            continue
        result = load_result(args.outputs, instance_id)
        if result is None:
            continue
        final = result.get("final_result", {})
        category = final.get("failure_category")
        is_f2p = bool(final.get("is_f2p"))
        if category in {"environment_error", "dependency_error", "import_error"}:
            rerun.append(instance_id)
        elif final.get("buggy_status") == "reproduced" or final.get("fixed_status") == "resolved":
            if not is_f2p:
                rerun.append(instance_id)

    print(",".join(rerun))
    print(f"era rerun list: {len(rerun)} ids (excluded {len(exclude)})", file=__import__("sys").stderr)


if __name__ == "__main__":
    main()
