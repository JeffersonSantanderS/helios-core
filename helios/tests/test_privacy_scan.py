"""Privacy scan: ensure no private identifiers remain in tracked source.

This test greps for patterns that should NEVER appear in committed code:
- Personal names/emails
- Private domains, hostnames, IPs
- Matrix room IDs and bot MXIDs
- SSH passwords, iCloud emails
- Personal device identifiers
- Internal network names

Allowlisted entries are for generic/test patterns only.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent / "helios"
SOURCE_DIR = REPO_ROOT / "helios"

# ── Patterns that must NEVER appear in tracked source ───────────────────────

BLOCKED_PATTERS: list[tuple[str, str]] = [
    # Private personal identifiers
    (r"jefferson\.santander@icloud\.com", "iCloud email address"),
    (r"Jeffer2son", "SSH password"),
    (r"jeffersons_iphone", "Personal device tracker ID"),
    (r"jefferson@santander\.network", "Personal email in User-Agent"),
    # Private domains/hostnames
    (r"matrix\.santander\.ovh", "Private Matrix homeserver"),
    (r"santander\.ovh", "Private domain"),
    (r"192\.168\.1\.\d{1,3}", "Private IP address"),
    (r"192\.168\.48\.\d{1,3}", "Private IP address"),
    # Matrix identifiers
    (r"!TTRUvypsEnozRUPWHn", "Private Matrix room ID"),
    (r"@sergio:matrix", "Private bot MXID"),
    (r"@jefferson:matrix", "Private user MXID"),
    # Branding that should be generic
    (r"Santander Network", "Private network branding"),
    (r"Sergio.*domain", "Private agent branding"),
    (r"Belongs to Sergio", "Private attribution"),
    (r"Kronos Export", "Internal system name"),
    (r"Khalena", "Personal name"),
    (r"sergioapplehealthsync", "Personal health prefix"),
    # Personal locations
    (r"\bCalgary\b", "Personal city name - use config-driven DEFAULT_CITY"),
    (r"\bAlberta\b", "Personal province name - use config-driven DEFAULT_PROVINCE"),
    (r"51\.0447", "Personal GPS coordinates (Calgary latitude)"),
    (r"114\.0719", "Personal GPS coordinates (Calgary longitude)"),
    # Additional personal identifiers
    (r"\bKhalena\b", "Personal name"),
    (r"\bKasaemsuk\b", "Personal name"),
    (r"\bSkyview\b", "Personal neighborhood"),
    (r"T3N\s*2J8", "Personal postal code"),
    (r"2215\s*60", "Personal address fragment"),
    (r"jefferson@\w+\.\w+", "Personal email in source"),

]

# ── Directories/files to skip ────────────────────────────────────────────────

SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "*.egg-info",
}

SKIP_FILES = {
    # The sanitize module deliberately contains these patterns in blocklists
    "context_api/sanitize.py",
    # Tests may reference patterns to verify redaction
    "test_context_api.py",
    "test_dashboard.py",
}


def _should_skip(path: Path) -> bool:
    """Return True if this path should be excluded from the scan."""
    rel = path.relative_to(REPO_ROOT).as_posix()
    for skip in SKIP_FILES:
        if skip in rel:
            return True
    return False


def _git_tracked_py_files() -> list[Path]:
    """Return all git-tracked .py files under helios/helios/."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", "helios/helios/"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=30,
        )
        if result.returncode != 0:
            pytest.skip("git ls-files failed — not in a git repo")
        files = []
        for line in result.stdout.strip().splitlines():
            p = REPO_ROOT / line
            if p.suffix == ".py" and not _should_skip(p):
                files.append(p)
        return files
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # Fallback: just walk the directory
        files = []
        for p in SOURCE_DIR.rglob("*.py"):
            if not _should_skip(p):
                files.append(p)
        return files


class TestPrivacyScan:
    """Scan source files for private identifiers that must not be committed."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.py_files = _git_tracked_py_files()

    def _grep_pattern(self, pattern: str) -> list[tuple[str, int, str]]:
        """Search for a regex pattern across all tracked Python files."""
        import re
        compiled = re.compile(pattern, re.IGNORECASE)
        hits = []
        for fpath in self.py_files:
            try:
                for i, line in enumerate(fpath.read_text(errors="replace").splitlines(), 1):
                    if compiled.search(line):
                        hits.append((str(fpath.relative_to(REPO_ROOT)), i, line.strip()))
            except (OSError, UnicodeDecodeError):
                continue
        return hits

    @pytest.mark.parametrize(
        "pattern,description",
        BLOCKED_PATTERS,
        ids=[p[1] for p in BLOCKED_PATTERS],
    )
    def test_blocked_pattern_not_in_source(self, pattern: str, description: str):
        hits = self._grep_pattern(pattern)
        assert hits == [], (
            f"Found {description!r} in tracked source:\n"
            + "\n".join(f"  {f}:{line}: {text}" for f, line, text in hits)
        )

    def test_config_yaml_not_tracked(self):
        """config.yaml should not be in git tracking (contains secrets)."""
        result = subprocess.run(
            ["git", "ls-files", "--", "helios/config/config.yaml"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=10,
        )
        if result.returncode != 0:
            pytest.skip("git not available")
        tracked = result.stdout.strip()
        assert tracked == "", (
            f"helios/config/config.yaml is still tracked by git. "
            f"Run: git rm --cached helios/config/config.yaml"
        )

    def test_no_hardcoded_passwords(self):
        """Check for obvious password patterns in source."""
        import re
        # Match password= or password: followed by a quoted string (not env var)
        pattern = r"""(?:password|passwd|pwd)\s*[=:]\s*["'][^"$\{]+["']"""
        compiled = re.compile(pattern, re.IGNORECASE)
        hits = []
        for fpath in self.py_files:
            try:
                for i, line in enumerate(fpath.read_text(errors="replace").splitlines(), 1):
                    if compiled.search(line):
                        hits.append((str(fpath.relative_to(REPO_ROOT)), i, line.strip()))
            except (OSError, UnicodeDecodeError):
                continue
        assert hits == [], (
            f"Found hardcoded password patterns:\n"
            + "\n".join(f"  {f}:{line}: {text}" for f, line, text in hits)
        )