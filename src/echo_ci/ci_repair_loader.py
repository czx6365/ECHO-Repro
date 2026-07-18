from __future__ import annotations

import csv
import json
import random
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from echo_ci.schema_utils import (
    canonical_preview,
    coerce_text,
    field,
    normalize_failure_type,
    safe_instance_id,
    type_name,
)

DEFAULT_HF_NAME = "ci-benchmark-user/ci-repair-bench"


def load_hf_dataset(hf_name: str = DEFAULT_HF_NAME, split: str | None = None) -> Any:
    from datasets import load_dataset

    if split:
        return load_dataset(hf_name, split=split)
    return load_dataset(hf_name)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    buffer = ""
    for line in Path(path).read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue
        buffer = line if not buffer else buffer + "\n" + line
        try:
            rows.append(dict(json.loads(buffer)))
            buffer = ""
        except json.JSONDecodeError:
            continue
    if buffer:
        rows.append(dict(json.loads(buffer)))
    return rows


def write_jsonl(rows: Iterable[Mapping[str, Any]], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")


def load_local_dataset(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return load_jsonl(path)
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [dict(row) for row in data]
        if isinstance(data, dict):
            for key in ("data", "instances", "rows"):
                value = data.get(key)
                if isinstance(value, list):
                    return [dict(row) for row in value]
            return [dict(data)]
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    raise ValueError(f"Unsupported dataset file extension: {path}")


def split_names(dataset: Any) -> list[str]:
    if isinstance(dataset, dict):
        return list(dataset.keys())
    if hasattr(dataset, "keys") and not isinstance(dataset, list):
        try:
            return list(dataset.keys())
        except Exception:
            pass
    return ["local"]


def iter_dataset_rows(dataset: Any, split: str | None = None) -> Iterable[dict[str, Any]]:
    if isinstance(dataset, list):
        yield from (dict(row) for row in dataset)
        return
    if split is not None and hasattr(dataset, "keys") and split in dataset.keys():
        rows = dataset[split]
        yield from (dict(row) for row in rows)
        return
    if split is not None and hasattr(dataset, "column_names"):
        rows = dataset
        yield from (dict(row) for row in rows)
        return
    if hasattr(dataset, "items"):
        for _, rows in dataset.items():
            yield from (dict(row) for row in rows)
        return
    yield from (dict(row) for row in dataset)


def load_instances(
    *,
    dataset_path: Path | None = None,
    hf_name: str = DEFAULT_HF_NAME,
    split: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    dataset = load_local_dataset(dataset_path) if dataset_path else load_hf_dataset(hf_name, split=split)
    rows = list(iter_dataset_rows(dataset, split=split))
    if limit is not None:
        rows = rows[:limit]
    return rows


def inspect_dataset(
    *,
    dataset_path: Path | None = None,
    hf_name: str = DEFAULT_HF_NAME,
    split: str | None = None,
    max_examples: int = 3,
) -> dict[str, Any]:
    dataset = load_local_dataset(dataset_path) if dataset_path else load_hf_dataset(hf_name, split=split)
    names = split_names(dataset)
    report: dict[str, Any] = {"source": str(dataset_path or hf_name), "splits": {}}
    for split_name in names:
        rows_iter = iter_dataset_rows(dataset, split=split_name if split_name != "local" else split)
        rows = []
        for index, row in enumerate(rows_iter):
            rows.append(row)
            if index + 1 >= max_examples:
                break
        columns: dict[str, str] = {}
        for row in rows:
            for key, value in row.items():
                columns.setdefault(str(key), type_name(value))
        report["splits"][split_name] = {
            "columns": columns,
            "examples": [
                {
                    "raw_keys": list(row.keys()),
                    "canonical_preview": canonical_preview(row),
                }
                for row in rows
            ],
        }
    return report


def sample_subset(
    instances: list[dict[str, Any]],
    *,
    limit: int,
    seed: int,
    failure_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rows = list(instances)
    if failure_types:
        wanted = {item.strip().lower() for item in failure_types if item.strip()}
        filtered = [
            row
            for row in rows
            if normalize_failure_type(field(row, "failure_type", default=None)) in wanted
        ]
        if filtered:
            rows = filtered
    rng.shuffle(rows)
    return rows[:limit]


def stratified_failure_bucket(instance: Mapping[str, Any]) -> str:
    """Map benchmark labels into the extension strata used by the paper."""

    label = coerce_text(field(instance, "failure_type", default="")).lower()
    if any(token in label for token in ("configuration", "config", "workflow", "build", "action")):
        return "build_workflow_config"
    if any(token in label for token in ("dependency", "install", "package", "module")):
        return "dependency_install"
    if any(token in label for token in ("test", "assertion")):
        return "test"
    if any(token in label for token in ("lint", "mypy", "ruff", "flake8", "pylint", "type check")):
        return "lint"
    if any(token in label for token in ("format", "style")):
        return "format"
    return "other"


def sample_stratified_extension(
    instances: list[dict[str, Any]],
    *,
    exclude_ids: set[str],
    quotas: Mapping[str, int],
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Select a deterministic, disjoint extension satisfying exact quotas."""

    rng = random.Random(seed)
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in quotas}
    for instance in instances:
        instance_id = safe_instance_id(instance)
        if instance_id in exclude_ids:
            continue
        bucket = stratified_failure_bucket(instance)
        if bucket in buckets:
            buckets[bucket].append(instance)

    selected: list[dict[str, Any]] = []
    manifest: dict[str, list[str]] = {}
    for bucket, quota in quotas.items():
        candidates = list(buckets.get(bucket, []))
        candidates.sort(key=safe_instance_id)
        rng.shuffle(candidates)
        if len(candidates) < int(quota):
            raise ValueError(
                f"Insufficient `{bucket}` candidates: requested {quota}, available {len(candidates)}"
            )
        chosen = candidates[: int(quota)]
        selected.extend(chosen)
        manifest[bucket] = [safe_instance_id(instance) for instance in chosen]
    return selected, manifest
