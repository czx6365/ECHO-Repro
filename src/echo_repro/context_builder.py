from __future__ import annotations

from echo_repro.models import BugSpec, RetrievedContext


def _format_snippets(snippets: dict[str, str]) -> str:
    if not snippets:
        return "(none)"
    blocks = []
    for path, text in snippets.items():
        blocks.append(f"File: {path}\n{text.strip()}")
    return "\n\n".join(blocks)


def build_concise_context(issue_text: str, bug_spec: BugSpec, retrieved_context: RetrievedContext) -> str:
    return f"""Bug Summary
{bug_spec.summary}

Current Behavior
{bug_spec.current_behavior}

Expected Behavior
{bug_spec.expected_behavior}

Failure Signature
{bug_spec.failure_signature}

Suspicious Source Context
{_format_snippets(retrieved_context.source_snippets)}

Related Test / Fixture Context
{_format_snippets(retrieved_context.test_snippets)}

Environment Context
{_format_snippets(retrieved_context.env_snippets)}

Original Issue Text
{issue_text.strip()}

Task Instruction
Generate a complete Python reproduction harness named reproduce.py.
It must print exactly one of:
- Issue reproduced
- Issue resolved
- Other issues
Use real imports and real function calls when possible.
Do not fake failure with a direct assertion created only to force reproduction.
"""

