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


def build_dependency_repair_prompt(
    repo_slug: str,
    missing_module: str,
    stderr_tail: str,
    python_version: str,
    declaration_summary: str,
    feedback: str = "",
    installed_packages: str = "",
) -> str:
    retry = (
        f"""
Previous install attempt failed. Adjust the packages or versions and reply again.

Previous install failure output:
{feedback}
"""
        if feedback
        else ""
    )
    installed = (
        f"""

Packages currently installed in the target virtualenv:
{installed_packages}
"""
        if installed_packages
        else ""
    )
    return f"""You are diagnosing a missing Python dependency for a bug reproduction pipeline.

Return only a JSON object with these keys:
- packages: array of objects, each with "name" (string) and "version_spec" (string, e.g. "==2.7.0" or "" for latest)
- install_commands: array of strings, each a complete install command such as "python -m pip install package==1.2.3"
- reason: string, one or two sentences explaining the choice

Requirements:
- Do not include markdown fences.
- Prefer the exact version the repository declares when visible.
- Use the fewest install commands needed, at most 3.
- Only propose commands of the form "pip install <packages...>" or "python -m pip install <packages...>", or exactly "pip install ." / "pip install -e ." to install the repository itself.
- Do not use --user, --target, --root, --prefix, or --break-system-packages flags.
- The repository may be old; do not pick the newest major version if its metadata pins an older one.
- Version choices must be compatible with the Python interpreter shown below.
- If the missing module is pkg_resources, pin setuptools<81 explicitly: setuptools 81+ removed pkg_resources, so an unpinned install of the latest setuptools will not fix the import.
- If the repository's own source directory shadows the pip-installed package (e.g. the missing module is '<pkg>.__check_build' or a '<pkg>.<submodule>' and <pkg> is already installed), propose exactly 'pip install -e .' to build and install the repository itself.
- Check the installed-packages list below: if the relevant package is already installed there, the installed version is probably wrong - propose a different version instead of reinstalling the same one.

Repository: {repo_slug}
Python interpreter: {python_version}
Missing module: {missing_module}

Last stderr from the failing execution:
{stderr_tail}

Repository dependency declarations that mention this module:
{declaration_summary}
{installed}{retry}"""
