from __future__ import annotations


ORACLE_CONTRACT = """Oracle contract:
- The script must decide from an observed project behavior, not from version strings or patch presence.
- Check the narrow failure signature from the issue. Prefer a local condition over exact full-output equality.
- Use two explicit branches:
  - print Issue reproduced only when the buggy behavior is directly observed.
  - print Issue resolved only when the expected fixed behavior is directly observed.
- Print Other issues only when the observation is unrelated or ambiguous.
- Do not silently swallow import errors or unexpected exceptions. For unexpected exceptions, print the traceback to stderr with traceback.print_exc(), then print Other issues.
- Print exactly one final stdout line from the allowed set and no other stdout."""


def build_bug_spec_extraction_prompt(issue_text: str) -> str:
    return f"""You are extracting a structured bug specification for a bug reproduction system.

Return only a JSON object with these keys:
- title: string
- summary: string
- current_behavior: string
- expected_behavior: string
- failure_signature: string
- reproduction_hint: string
- keywords: array of short strings
- suspect_symbols: array of short strings

Requirements:
- Be concise and faithful to the issue text.
- Infer likely keywords and suspect symbols when possible.
- Do not include markdown fences.
- If a field is unclear, still return a best-effort string or empty array.

Issue text:
{issue_text}
"""


def build_harness_generation_prompt(concise_context: str) -> str:
    return f"""Write a complete Python file named reproduce.py for bug reproduction.

Requirements:
- Return only Python code, no markdown fences.
- The script must be complete and executable.
- Use real project-local imports and calls whenever possible.
- Prefer the repository's local test or fixture style when relevant.
- Keep the script minimal and focused.
- Include robust exception handling.
- Do not create an artificial failure by directly raising AssertionError only to fake reproduction.
- Print exactly one final stdout line from this closed set:
  - Issue reproduced
  - Issue resolved
  - Other issues
- Avoid extra prints.
- Import traceback if you catch unexpected exceptions.

{ORACLE_CONTRACT}

Goal:
- On the buggy repo, the script should print Issue reproduced when the issue is genuinely observed.
- On the fixed repo, the script should print Issue resolved when the issue is genuinely fixed.

Context:
{concise_context}
"""


def build_harness_repair_prompt(concise_context: str, current_code: str, feedback: str) -> str:
    return f"""Repair the following Python bug reproduction harness.

Requirements:
- Return only a complete Python file, no markdown fences.
- Preserve the intent of reproducing the target issue.
- Keep the script minimal.
- Use real project-local imports and calls whenever possible.
- Include robust exception handling.
- Print exactly one final stdout line from this closed set:
  - Issue reproduced
  - Issue resolved
  - Other issues
- Do not fake failure with AssertionError-only logic.
- Import traceback if you catch unexpected exceptions.

{ORACLE_CONTRACT}

Context:
{concise_context}

Current harness:
{current_code}

Execution feedback:
{feedback}
"""


def build_harness_strengthen_prompt(concise_context: str, current_code: str, feedback: str) -> str:
    return f"""Revise the following Python bug reproduction harness to strengthen its oracle.

Requirements:
- Return only a complete Python file, no markdown fences.
- Keep the harness minimal and executable.
- Use real project-local imports and calls whenever possible.
- Strengthen the logic that distinguishes buggy behavior from fixed behavior.
- Prefer checking the smallest issue-specific symptom over matching an entire matrix, repr, warning block, or full exception text.
- Include robust exception handling.
- Print exactly one final stdout line from this closed set:
  - Issue reproduced
  - Issue resolved
  - Other issues
- Do not add artificial AssertionError-only failure logic.
- Import traceback if you catch unexpected exceptions.

{ORACLE_CONTRACT}

Context:
{concise_context}

Current harness:
{current_code}

Execution feedback:
{feedback}
"""
