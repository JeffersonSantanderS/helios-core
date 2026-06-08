"""Helios v6 — Fernet symmetric encryption for sensitive module state at rest.

Encrypts module state files so PII (GPS, health metrics, contacts, mood)
cannot be read without the key.  Key is stored in
    $HELIOS_BASE/.crypto_key   (default: ~/.hermes/helios/.crypto_key)

Encrypted files use a .enc extension so the original plaintext can be
deleted after successful migration.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.crypto")

# ── Key management ────────────────────────────────────────────────────


def _key_path() -> Path:
    """Return the path to the Fernet key file, respecting HELIOS_BASE."""
    base = os.environ.get(
        "HELIOS_BASE",
        os.path.join(os.path.expanduser("~"), ".hermes", "helios"),
    )
    return Path(base) / ".crypto_key"


def generate_key() -> bytes:
    """Generate a new Fernet key and persist it with restrictive permissions.

    If a key file already exists it is returned as-is (no overwrite).
    Returns the raw key bytes.
    """
    from cryptography.fernet import Fernet

    key_path = _key_path()
    if key_path.exists():
        try:
            key = key_path.read_bytes().strip()
            # Validate it's a proper Fernet key
            Fernet(key)
            return key
        except Exception:
            log.warning("Existing key file corrupted, regenerating")

    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    # Enforce 0600 — owner read/write only
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    log.info("Generated new Fernet key at %s", key_path)
    return key


def _load_key() -> Optional[bytes]:
    """Load the Fernet key from disk, or None if missing/corrupted."""
    key_path = _key_path()
    if not key_path.exists():
        log.warning("Fernet key file not found at %s", key_path)
        return None
    try:
        key = key_path.read_bytes().strip()
        from cryptography.fernet import Fernet
        Fernet(key)  # validate
        return key
    except Exception as exc:
        log.warning("Corrupted Fernet key at %s: %s", key_path, exc)
        return None


def _get_or_create_key() -> bytes:
    """Return a usable key, auto-generating on first use if needed."""
    key = _load_key()
    if key is not None:
        return key
    log.info("No valid key found — auto-generating")
    return generate_key()


# ── Encrypt / decrypt ────────────────────────────────────────────────


def encrypt_data(data: dict[str, Any]) -> bytes:
    """Serialize a dict to JSON and encrypt with Fernet.

    Auto-generates a key on first use.
    """
    from cryptography.fernet import Fernet

    key = _get_or_create_key()
    f = Fernet(key)
    payload = json.dumps(data, default=str).encode("utf-8")
    return f.encrypt(payload)


def decrypt_data(token: bytes) -> dict[str, Any]:
    """Decrypt a Fernet token and deserialize JSON back to a dict.

    Raises ``cryptography.fernet.InvalidToken`` on corruption.
    """
    from cryptography.fernet import Fernet

    key = _load_key()
    if key is None:
        raise RuntimeError("Cannot decrypt: no valid Fernet key available")
    f = Fernet(key)
    plaintext = f.decrypt(token)
    return json.loads(plaintext.decode("utf-8"))


# ── File-level helpers ────────────────────────────────────────────────


def _state_dir() -> Path:
    """Return the data directory for module state files."""
    base = os.environ.get(
        "HELIOS_BASE",
        os.path.join(os.path.expanduser("~"), ".hermes", "helios"),
    )
    return Path(base) / "data"


def encrypt_file(src: Path, dst: Path) -> None:
    """Read a plaintext JSON file, encrypt it, and write to dst."""
    data = json.loads(src.read_text(encoding="utf-8"))
    token = encrypt_data(data)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(token)


def decrypt_file(path: Path) -> dict[str, Any]:
    """Read an encrypted state file and return the decrypted dict."""
    token = path.read_bytes()
    return decrypt_data(token)


def is_encrypted_file(path: Path) -> bool:
    """Check if a path points to an encrypted (.enc) state file."""
    return path.suffix == ".enc"