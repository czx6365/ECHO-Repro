from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

from echo_ci import log_parser
from echo_ci.schema_utils import FIELD_CANDIDATES, coerce_text, field, normalize_failure_type
from echo_ci.workflow_parser import extract_workflow_context, extract_workflow_snippet


@dataclass
class FailureSpec:
    instance_id: str
    repo: str
    failing_commit: str
    fixed_commit: str
    failed_stage: str = ""
    failed_job: str = ""
    failed_step: str = ""
    failed_command: str = ""
    error_signature: str = ""
    observed_behavior: str = ""
    expected_behavior: str = ""
    suspected_artifacts: list[str] = dataclass_field(default_factory=list)
    dependency_context: list[str] = dataclass_field(default_factory=list)
    environment_context: list[str] = dataclass_field(default_factory=list)
    workflow_context: list[str] = dataclass_field(default_factory=list)
    oracle_type: str = "unknown"
    failure_type: str = "unknown"
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write_json(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _oracle_type(failure_type: str, signature: str, command: str) -> str:
    text = f"{failure_type}\n{signature}\n{command}".lower()
    if "assert" in text or "pytest" in text:
        return "assertion" if "assert" in text else "test"
    if "flake8" in text or "ruff" in text or "mypy" in text or "pylint" in text:
        return "lint"
    if "black" in text or "isort" in text:
        return "format"
    if "modulenotfounderror" in text or "importerror" in text:
        return "dependency"
    if "build" in text or "setup.py" in text:
        return "build"
    if "exception" in text or "traceback" in text:
        return "exception"
    return failure_type if failure_type in {"test", "lint", "format", "dependency", "build"} else "unknown"


def _confidence(spec: FailureSpec) -> float:
    score = 0.15
    for value, weight in (
        (spec.repo, 0.1),
        (spec.failing_commit, 0.1),
        (spec.fixed_commit, 0.1),
        (spec.failed_command, 0.2),
        (spec.error_signature, 0.25),
        (spec.workflow_context, 0.05),
        (spec.suspected_artifacts, 0.05),
    ):
        if value:
            score += weight
    return min(1.0, round(score, 2))


def extract_failure_spec_rule_based(instance: dict[str, Any]) -> FailureSpec:
    ci_log = log_parser.normalize_log(field(instance, "ci_log", default=""))
    workflow = coerce_text(field(instance, "workflow_content", default=""))
    failed_command = log_parser.extract_failed_command(ci_log)
    error_signature = log_parser.extract_error_signature(ci_log)
    dataset_failure_type = normalize_failure_type(field(instance, "failure_type", default=None))
    failure_type = log_parser.infer_failure_type(ci_log, failed_command, default=dataset_failure_type)
    if failure_type == "unknown" and dataset_failure_type != "unknown":
        failure_type = dataset_failure_type

    issue_text = coerce_text(field(instance, "issue", default=""))
    workflow_context = extract_workflow_context(workflow, failed_command=failed_command)
    artifacts = log_parser.extract_suspected_artifacts(
        ci_log,
        workflow,
        coerce_text(field(instance, "patch", default="")),
        failed_command,
    )

    spec = FailureSpec(
        instance_id=str(field(instance, "instance_id", default="")),
        repo=str(field(instance, "repo", default="")),
        failing_commit=str(field(instance, "failing_commit", default="")),
        fixed_commit=str(field(instance, "fixed_commit", default="")),
        failed_stage="ci",
        failed_job="",
        failed_step=log_parser.extract_failed_step(ci_log),
        failed_command=failed_command,
        error_signature=error_signature,
        observed_behavior=error_signature or issue_text[:500],
        expected_behavior="The same harness should pass on the fixed/success commit.",
        suspected_artifacts=artifacts,
        dependency_context=log_parser.extract_dependency_context(ci_log),
        environment_context=log_parser.extract_environment_context(ci_log),
        workflow_context=workflow_context,
        oracle_type=_oracle_type(failure_type, error_signature, failed_command),
        failure_type=failure_type,
        confidence=0.0,
    )
    spec.confidence = _confidence(spec)
    return spec


def extract_failure_spec(
    instance: dict[str, Any],
    *,
    use_llm: bool = False,
    llm_client: Any | None = None,
) -> FailureSpec:
    rule_based = extract_failure_spec_rule_based(instance)
    if not use_llm or llm_client is None:
        return rule_based

    ci_log = log_parser.extract_ci_log_snippet(
        field(instance, "ci_log", default=""),
        rule_based.failed_command,
    )
    workflow = extract_workflow_snippet(
        coerce_text(field(instance, "workflow_content", default="")),
        failed_command=rule_based.failed_command,
    )
    prompt = {
        "instruction": "Return only a JSON object matching the FailureSpec schema.",
        "schema_fields": list(FIELD_CANDIDATES.keys()),
        "rule_based_failure_spec": rule_based.to_dict(),
        "ci_log_snippet": ci_log,
        "workflow_snippet": workflow,
    }
    try:
        raw = llm_client.generate_json(json.dumps(prompt, ensure_ascii=False, indent=2))
        merged = rule_based.to_dict()
        for key in merged:
            if key in raw and raw[key] not in (None, ""):
                merged[key] = raw[key]
        spec = FailureSpec(**merged)
        spec.confidence = max(rule_based.confidence, float(spec.confidence or 0.0))
        return spec
    except Exception:
        return rule_based
