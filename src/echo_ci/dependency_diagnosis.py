from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any


DECLARATION_NAMES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements-tests.txt",
}


def extract_missing_module(text: str) -> str:
    patterns = (
        r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]",
        r"ImportError:\s+No module named ['\"]([^'\"]+)['\"]",
        r"cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, str(text or ""), re.IGNORECASE)
        if match:
            if len(match.groups()) > 1 and match.group(2):
                return match.group(2).split(".")[0]
            return match.group(1).split(".")[0]
    return ""


def _target_paths(target_preflight: dict[str, Any] | None) -> list[str]:
    paths: list[str] = []
    for record in (target_preflight or {}).get("targets") or []:
        for key in ("fixed_resolved_path", "failing_resolved_path", "target"):
            value = str(record.get(key) or "")
            if value and value not in paths:
                paths.append(value)
    return paths


def _imports_module(path: Path, module: str) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == module for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and str(node.module or "").split(".")[0] == module:
            return True
    return False


def _find_importers(repo_path: Path, module: str, targets: list[str]) -> list[str]:
    importers: list[str] = []
    for target in targets:
        path = repo_path / target
        if path.is_file() and path.suffix in {".py", ".pyi"} and _imports_module(path, module):
            relative = path.relative_to(repo_path).as_posix()
            if relative not in importers:
                importers.append(relative)
    return importers


def _declaration_files(repo_path: Path, targets: list[str] | None = None) -> list[Path]:
    files: list[Path] = []
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", ".echo_venv", ".echo_repro", "node_modules"} for part in path.parts):
            continue
        name = path.name.lower()
        if name in DECLARATION_NAMES or name.startswith("requirements") and name.endswith(".txt"):
            files.append(path)
    target_parts = [Path(target).parts for target in (targets or [])]

    def common_prefix_length(left: tuple[str, ...], right: tuple[str, ...]) -> int:
        length = 0
        for left_part, right_part in zip(left, right):
            if left_part != right_part:
                break
            length += 1
        return length

    def relevance(path: Path) -> tuple[int, str]:
        relative_parts = path.relative_to(repo_path).parts
        shared = max(
            (common_prefix_length(relative_parts, parts) for parts in target_parts),
            default=0,
        )
        return (-shared, path.as_posix())

    return sorted(files, key=relevance)


def _find_declarations(repo_path: Path, module: str, targets: list[str]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    needle = module.lower().replace("_", "-")
    for path in _declaration_files(repo_path, targets):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        section = ""
        for line_number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped.strip("[]")
            comparable = stripped.lower().replace("_", "-")
            if needle not in comparable:
                continue
            if path.name == "pyproject.toml" and not any(
                marker in section.lower()
                for marker in ("dependenc", "optional", "extras")
            ):
                # Mentions in mypy plugins, Ruff conventions, and similar
                # tool configuration are not package declarations.
                continue
            if path.name == "pyproject.toml" and "optional" in section.lower():
                source = "project_optional_extras"
            elif path.name.startswith("requirements"):
                source = "requirements_file"
            else:
                source = "project_declaration"
            declarations.append(
                {
                    "path": path.relative_to(repo_path).as_posix(),
                    "line": line_number,
                    "entry": stripped[:500],
                    "section": section,
                    "source": source,
                }
            )
    return declarations[:20]


def _package_from_declarations(module: str, declarations: list[dict[str, Any]]) -> str:
    needle = module.lower().replace("_", "-")
    for declaration in declarations:
        entry = str(declaration.get("entry") or "")
        quoted = re.findall(r"['\"]([^'\"]+)['\"]", entry)
        for candidate in quoted:
            package = re.split(r"[<>=!~;\[]", candidate, maxsplit=1)[0].strip()
            if needle in package.lower().replace("_", "-"):
                return package
        plain = re.split(r"[<>=!~;\s]", entry.lstrip("- "), maxsplit=1)[0].strip("'\"")
        if needle in plain.lower().replace("_", "-"):
            return plain
    return module.replace("_", "-")


def diagnose_missing_dependency(
    *,
    repo_path: Path,
    output_text: str,
    target_preflight: dict[str, Any] | None = None,
    workflow_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repo_path = Path(repo_path)
    module = extract_missing_module(output_text)
    if not module:
        return {}
    targets = _target_paths(target_preflight)
    imported_by = _find_importers(repo_path, module, targets)
    declarations = _find_declarations(repo_path, module, targets)
    workflow_setup = [
        str(command)
        for command in (workflow_signal or {}).get("setup_commands") or []
    ]
    sources: list[str] = []
    if imported_by:
        sources.append("target_test_imports")
    for declaration in declarations:
        source = str(declaration.get("source") or "")
        if source and source not in sources:
            sources.append(source)
    if workflow_setup:
        sources.append("workflow_install_step")
    if not sources:
        sources.append("transitive_runtime_import")
    package = _package_from_declarations(module, declarations)
    return {
        "missing_module": module,
        "imported_by": imported_by,
        "declared_in": declarations,
        "workflow_setup": workflow_setup,
        "attributed_sources": sources,
        "recommended_minimal_install": f"python -m pip install {package}",
        "diagnosis_only": True,
    }
