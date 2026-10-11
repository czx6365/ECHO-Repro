from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from echo_ci.ci_repair_loader import (
    DEFAULT_HF_NAME,
    load_instances,
    sample_stratified_extension,
    write_jsonl,
)
from echo_ci.schema_utils import safe_instance_id


DEFAULT_QUOTAS = {
    "test": 6,
    "dependency_install": 6,
    "build_workflow_config": 4,
    "lint": 2,
    "format": 2,
}


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extend a frozen CI-Repair pilot with a deterministic stratified sample."
    )
    parser.add_argument("--base", type=Path, default=Path("data/ci_repair_subset_30.jsonl"))
    parser.add_argument("--dataset_path", type=Path, default=None)
    parser.add_argument("--hf_name", default=DEFAULT_HF_NAME)
    parser.add_argument("--split", default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--extension_output", type=Path, default=Path("data/ci_repair_extension_20.jsonl"))
    parser.add_argument("--combined_output", type=Path, default=Path("data/ci_repair_subset_50.jsonl"))
    parser.add_argument("--manifest_output", type=Path, default=Path("data/ci_repair_subset_50_manifest.json"))
    args = parser.parse_args()

    base = load_instances(dataset_path=args.base)
    full = load_instances(dataset_path=args.dataset_path, hf_name=args.hf_name, split=args.split)
    base_ids = {safe_instance_id(instance) for instance in base}
    extension, strata = sample_stratified_extension(
        full,
        exclude_ids=base_ids,
        quotas=DEFAULT_QUOTAS,
        seed=args.seed,
    )
    combined = [*base, *extension]
    write_jsonl(extension, args.extension_output)
    write_jsonl(combined, args.combined_output)
    manifest = {
        "seed": args.seed,
        "source": str(args.dataset_path or args.hf_name),
        "base_path": str(args.base),
        "base_count": len(base),
        "extension_count": len(extension),
        "combined_count": len(combined),
        "quotas": DEFAULT_QUOTAS,
        "strata": strata,
        "base_ids": [safe_instance_id(instance) for instance in base],
        "extension_ids": [safe_instance_id(instance) for instance in extension],
        "base_sha256": sha256(args.base),
    }
    args.manifest_output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["extension_sha256"] = sha256(args.extension_output)
    manifest["combined_sha256"] = sha256(args.combined_output)
    args.manifest_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
