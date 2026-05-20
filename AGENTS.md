# AGENTS.md

Guidance for future Codex or contributor tasks in this repository:

- Keep modules small and focused.
- Always add or update tests for behavior changes.
- Run `pytest` before finishing work.
- Do not remove or break mock mode. The project must remain runnable offline.
- Keep the feedback loop deterministic in mock mode so tests stay stable.
- Network-dependent tests must be mocked or replaced with local fixtures.
- Keep sandbox warnings around generated code and execution.
- Avoid heavyweight dependencies unless they clearly unlock needed capability.
- Prefer extending retrieval and prompting through clean seams rather than rewriting the pipeline.
- Preserve the invariant that generated harnesses are written only inside the target repo.
