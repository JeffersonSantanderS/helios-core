"""Regression guard: Google Workspace OAuth is retired for Helios.

Helios must not depend on ~/.hermes/google_token.json or direct Google
Workspace REST endpoints. Calendar access belongs behind Home Assistant and
Gmail access belongs outside Helios through Himalaya.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (
    REPO_ROOT / "helios" / "helios",
    REPO_ROOT / "helios" / "config",
)
FORBIDDEN_PATTERNS = (
    "google_token.json",
    "google_client_secret.json",
    "gmail.googleapis.com",
    "www.googleapis.com/calendar/v3",
    "googleapiclient",
)


def _iter_project_files():
    for root in SCANNED_ROOTS:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
                continue
            if path.suffix not in {".py", ".yaml", ".yml", ".toml", ".json", ".md"}:
                continue
            yield path


def test_helios_runtime_has_no_google_workspace_oauth_dependency():
    offenders: list[str] = []
    for path in _iter_project_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.lower() in text.lower():
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}: {pattern}")

    assert offenders == []
