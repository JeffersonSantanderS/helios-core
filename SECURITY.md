# Security Policy

## Supported Versions

Helios is an active personal-intelligence project. Security updates are applied to the `main` branch.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them by:

1. **Email** — Send details to the maintainer privately.
2. **GitHub Security Advisory** — Use [GitHub's private vulnerability reporting](https://github.com/jefferson-north/helios-v6/security/advisories/new) if available.

Please include:

- A description of the vulnerability
- Steps to reproduce
- The potential impact
- Any suggested mitigations (optional but appreciated)

## What constitutes a security issue

- **Credential exposure** — Hardcoded API keys, tokens, passwords, or session cookies in source code.
- **Data leakage** — Personal data (health, location, contacts) being written to logs, exports, or paths that could be committed to git.
- **Injection vulnerabilities** — Any path where user-controlled input could execute arbitrary code.
- **Authentication bypass** — Ways to access protected endpoints or data without proper credentials.
- **Dependency vulnerabilities** — Known CVEs in dependencies that affect Helios runtime.

## What is NOT a security issue

- Misconfiguration of user-provided API keys or tokens
- Personal data that a user intentionally exports or shares from their own instance
- Feature requests or bugs that don't expose data or access

## Response timeline

- **Acknowledgment** — Within 48 hours of receiving a report.
- **Triage** — Within 5 business days.
- **Fix** — Depends on severity; critical issues will be addressed as soon as possible.

## Security best practices for contributors

- **Never commit secrets** — Use environment variables (see `.env.example`) or `~/.hermes/` local paths.
- **Never commit personal data** — Health, location, contacts, and mood data must stay in local runtime directories.
- **Validate inputs** — Collector scripts and data ingestion should validate JSON structure before processing.
- **Atomic writes** — Use temp-file + rename for all stable exports to prevent partial-read corruption.
- **Principle of least privilege** — Collector scripts and systemd services should run with minimal permissions.

## Dependency security

Helios has minimal dependencies. The core requires only `pyyaml`. Optional dependencies (`spotipy`, `fastapi`, `watchdog`, `pyicloud`) should be pinned to known-good versions.

To audit dependencies:

```bash
pip audit
```

Thank you for helping keep Helios secure.