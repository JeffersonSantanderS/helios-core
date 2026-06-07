# GitHub Copilot instructions — Helios

Use `AGENTS.md` as the canonical project guidance.

When suggesting code:
- Preserve existing script-first architecture.
- Add or update tests for behavior changes.
- Do not introduce secrets, local state, databases, logs, caches, or virtualenv files.
- Prefer small Python functions with explicit inputs and deterministic behavior.
