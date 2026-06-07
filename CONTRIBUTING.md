# Contributing to Helios

Thank you for your interest in Helios! This document covers everything you need to know to contribute effectively.

---

## Code of Conduct

Be respectful. Be constructive. We're all here to make Helios better. Harassment, personal attacks, and trolling are not welcome.

---

## How to Contribute

### Bug Reports

1. Search [existing issues](https://github.com/jefferson-north/helios-v6/issues) to avoid duplicates.
2. Open a new issue with:
   - **What happened** — steps to reproduce
   - **What you expected** — the correct behavior
   - **Environment** — OS, Python version, Helios version
   - **Logs** — relevant daemon output or traceback (sanitized of personal data)

### Feature Requests

Open an issue with the prefix `[Feature]` and describe the use case, not just the solution. Explain what you want Helios to do and why.

### Pull Requests

1. **Fork** the repository and create a branch from `main`.
2. **Make your changes** with clear, small commits.
3. **Add or update tests** for any behavioral change. Bug fixes should include a regression test.
4. **Run the test suite** and ensure it passes:

   ```bash
   cd helios
   python -m pytest -q
   ```

5. **Open a pull request** with a clear description of what changed and why.

---

## Design Principles

Helios follows a few core conventions. Please keep these in mind:

| Principle | What it means |
|-----------|---------------|
| **Deterministic-first** | Scripts, rules, and SQLite power the core. LLM calls are opt-in polish/fallback, never the primary control path. |
| **Electric-motor / gas-engine** | Deterministic code is the reliable motor. LLMs are the gas engine — powerful but only used when the motor isn't enough. |
| **Privacy by design** | Never commit personal data, credentials, session cookies, or generated state. Runtime data lives outside the repo under `~/.hermes/`. |
| **Stable exports** | JSON contracts should be written atomically (temp-file + rename) and versioned with `schema_version`. |
| **Freshness over silence** | Stale data must be surfaced honestly, never silently treated as fresh. |
| **Small, reviewable commits** | Use [conventional commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`, `chore:`. |

---

## Adding a New Module

1. Create `helios/helios/modules/your_module.py` implementing the module interface.
2. Register it in `helios/helios/scheduler.py` (or the appropriate registry).
3. Add tests in `helios/tests/test_your_module.py`.
4. Add a state file or data directory pattern under `~/.hermes/helios/data/`.
5. Update `.gitignore` if the module introduces new local-file patterns.
6. Document the module in `README.md` and the relevant `docs/` file.
7. Add any new environment variables to `.env.example`.

---

## Adding a New Collector

1. Create the collector script in `helios/collectors/` or `scripts/`.
2. Each collector should produce:
   - A **state file** — a single JSON with current status (overwritten each cycle).
   - An optional **history file** — a JSONL file appended with events for trend analysis.
   - **Freshness metadata** — timestamp so the daemon knows if data is stale.
3. Add a systemd unit + timer in `deploy/systemd/` if the collector runs on a schedule.
4. Wire the ingestion path in `helios/helios/data_ingestion.py`.
5. Add tests for the ingestion path.

---

## Code Style

- Python 3.10+; prefer readability over cleverness.
- Follow PEP 8 with 4-space indentation.
- Use type hints on public functions.
- Keep functions small and composable.
- Avoid hidden side effects at import time.

---

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(module): add sleep correlation to insight engine
fix(briefing): stale weather data now correctly surfaced
docs: update README module table
test(ingestion): add coverage for Spotify history parsing
chore: pin pytest-timeout in dev extras
```

---

## Testing

```bash
cd helios
python -m pytest -q                        # full suite
python -m pytest -q tests/test_weather.py  # single module
```

All new code should have corresponding tests. Bug fixes should include a regression test that fails before the fix and passes after.

---

## Review Process

- Maintainers will review PRs as soon as possible.
- Be responsive to feedback — push fixes to the same branch.
- PRs that pass CI and have clear documentation will merge faster.

---

## Questions?

Open an issue with the `[Question]` prefix, or start a discussion in GitHub Discussions.

Thank you for contributing to Helios! 🌞