"""Tests for SAN-125: Encrypted state at rest for Helios v6 sensitive modules.

Covers:
  - Fernet key generation and file permissions
  - Encrypt/decrypt roundtrip
  - Plaintext → encrypted migration
  - Auto-generation of missing keys
  - Non-encrypted modules remain unchanged
  - Corrupted encrypted file handling
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_key_path(tmp_path, monkeypatch):
    """Redirect HELIOS_BASE to a temporary directory for every test."""
    helios_base = tmp_path / "helios"
    helios_base.mkdir()
    monkeypatch.setenv("HELIOS_BASE", str(helios_base))
    # Ensure we pick up the env var change
    monkeypatch.setattr(
        "helios.crypto._key_path",
        lambda: helios_base / ".crypto_key",
    )
    monkeypatch.setattr(
        "helios.modules.base.BaseMod._state_dir",
        staticmethod(lambda: helios_base / "data"),
    )
    yield helios_base


@pytest.fixture()
def key_path(_isolate_key_path):
    return _isolate_key_path / ".crypto_key"


@pytest.fixture()
def data_dir(_isolate_key_path):
    d = _isolate_key_path / "data"
    d.mkdir(exist_ok=True)
    return d


# ── Key generation tests ────────────────────────────────────────────────


class TestGenerateKey:
    def test_generate_key_creates_file(self, key_path):
        from helios.crypto import generate_key

        key = generate_key()
        assert key_path.exists(), "Key file should be created"
        assert len(key) > 0, "Key should not be empty"

    def test_generate_key_returns_valid_fernet_key(self):
        from helios.crypto import generate_key
        from cryptography.fernet import Fernet

        key = generate_key()
        # Must not raise
        f = Fernet(key)
        # Round-trip check
        token = f.encrypt(b"test")
        assert f.decrypt(token) == b"test"

    def test_key_file_permissions(self, key_path):
        from helios.crypto import generate_key

        key = generate_key()
        mode = key_path.stat().st_mode
        # Owner-read + owner-write only = 0o100600
        assert mode & 0o777 == 0o600, (
            f"Key file should be 0600, got {oct(mode & 0o777)}"
        )

    def test_generate_key_idempotent(self, key_path):
        from helios.crypto import generate_key

        key1 = generate_key()
        key2 = generate_key()
        # Same key returned on subsequent calls (not regenerated)
        assert key1 == key2


class TestMissingKey:
    def test_missing_key_auto_generates(self, key_path):
        assert not key_path.exists()
        from helios.crypto import generate_key

        key = generate_key()
        assert key_path.exists()
        assert len(key) > 0


# ── Encrypt / Decrypt roundtrip ──────────────────────────────────────────


class TestEncryptDecryptRoundtrip:
    def test_encrypt_decrypt_roundtrip(self):
        from helios.crypto import encrypt_data, decrypt_data

        original = {"lat": 51.0447, "lon": -114.0719, "zone": "home"}
        token = encrypt_data(original)
        assert isinstance(token, bytes)

        result = decrypt_data(token)
        assert result == original

    def test_encrypt_large_dict(self):
        from helios.crypto import encrypt_data, decrypt_data

        data = {f"key_{i}": f"value_{i}" * 100 for i in range(50)}
        token = encrypt_data(data)
        result = decrypt_data(token)
        assert result == data

    def test_encrypt_nested_structures(self):
        from helios.crypto import encrypt_data, decrypt_data

        data = {
            "contacts": [
                {"name": "Alice", "phone": "+1-555-0100"},
                {"name": "Bob", "phone": "+1-555-0200"},
            ],
            "metadata": {"count": 2, "source": "icloud"},
        }
        token = encrypt_data(data)
        result = decrypt_data(token)
        assert result == data


# ── Corrupted encrypted file ────────────────────────────────────────────


class TestCorruptedEncryptedFile:
    def test_corrupted_encrypted_file_raises(self, key_path, data_dir):
        from helios.crypto import generate_key

        generate_key()  # ensure key exists

        enc_path = data_dir / "test_state.json.enc"
        enc_path.write_bytes(b"this is not valid fernet data at all")

        from helios.modules.base import BaseMod

        class TestMod(BaseMod):
            encrypted_state = True
            MODULE_MANIFEST = {**BaseMod.MODULE_MANIFEST, "name": "test"}

            def tick(self):
                return {}

        mod = TestMod()
        result = mod._load_state_encrypted("test_state.json")
        # Should return empty dict (logged warning) instead of crashing
        assert result == {}


# ── Plaintext migration ────────────────────────────────────────────────


class TestPlaintextMigration:
    def test_load_plaintext_migrates_to_encrypted(self, key_path, data_dir):
        from helios.crypto import generate_key

        generate_key()

        # Write a plaintext state file
        plain_path = data_dir / "mood_state.json"
        original = {"score": 7, "last_checkin": "2026-06-07", "trend": "stable"}
        plain_path.write_text(json.dumps(original), encoding="utf-8")
        assert plain_path.exists()

        from helios.modules.base import BaseMod

        class MoodMod(BaseMod):
            encrypted_state = True
            MODULE_MANIFEST = {**BaseMod.MODULE_MANIFEST, "name": "mood"}

            def tick(self):
                return {}

        mod = MoodMod()
        result = mod._load_state_encrypted("mood_state.json")
        assert result == original
        # Plaintext file should have been deleted after migration
        assert not plain_path.exists(), "Plaintext should be deleted after migration"
        # Encrypted file should now exist
        assert (data_dir / "mood_state.json.enc").exists()

    def test_migration_preserves_data(self, key_path, data_dir):
        from helios.crypto import generate_key

        generate_key()

        plain_path = data_dir / "contacts_state.json"
        contacts = {
            "contacts": [
                {"name": "Alice", "phone": "+1-555-0100"},
                {"name": "Bob", "phone": "+1-555-0200"},
            ],
            "count": 2,
        }
        plain_path.write_text(json.dumps(contacts), encoding="utf-8")

        from helios.modules.base import BaseMod

        class ContactsMod(BaseMod):
            encrypted_state = True
            MODULE_MANIFEST = {**BaseMod.MODULE_MANIFEST, "name": "contacts"}

            def tick(self):
                return {}

        mod = ContactsMod()
        result = mod._load_state_encrypted("contacts_state.json")
        assert result["count"] == 2
        assert len(result["contacts"]) == 2

        # Encrypted file should be readable with crypto primitives
        from helios.crypto import decrypt_data

        enc_path = data_dir / "contacts_state.json.enc"
        decrypted = decrypt_data(enc_path.read_bytes())
        assert decrypted == contacts


# ── Non-encrypted modules ───────────────────────────────────────────────


class TestNonEncryptedModule:
    def test_non_encrypted_module_unchanged(self, key_path, data_dir):
        from helios.crypto import generate_key

        generate_key()

        plain_path = data_dir / "weather_state.json"
        original = {"temp": 22, "condition": "sunny"}
        plain_path.write_text(json.dumps(original), encoding="utf-8")

        from helios.modules.base import BaseMod

        class WeatherMod(BaseMod):
            encrypted_state = False  # default
            MODULE_MANIFEST = {**BaseMod.MODULE_MANIFEST, "name": "weather"}

            def tick(self):
                return {}

        mod = WeatherMod()

        # Save should write plaintext (not encrypted)
        saved_path = mod._save_state_encrypted("weather_state.json", original)
        assert saved_path.suffix != ".enc"
        assert saved_path == data_dir / "weather_state.json"

        # Load should read plaintext directly
        result = mod._load_state_encrypted("weather_state.json")
        assert result == original

        # No .enc file should exist
        assert not (data_dir / "weather_state.json.enc").exists()

    def test_default_encrypted_state_is_false(self):
        from helios.modules.base import BaseMod

        class PlainMod(BaseMod):
            MODULE_MANIFEST = {**BaseMod.MODULE_MANIFEST, "name": "plain"}

            def tick(self):
                return {}

        mod = PlainMod()
        assert mod.encrypted_state is False


# ── Module-level encrypted_state markers ─────────────────────────────────


class TestModuleMarkers:
    """Verify the four sensitive modules have encrypted_state = True."""

    def test_location_module_encrypted(self):
        from helios.modules.location import LocationModule

        assert LocationModule.encrypted_state is True

    def test_health_module_encrypted(self):
        from helios.modules.health import HealthModule

        assert HealthModule.encrypted_state is True

    def test_contacts_module_encrypted(self):
        from helios.modules.contacts import ContactsModule

        assert ContactsModule.encrypted_state is True

    def test_mood_module_encrypted(self):
        from helios.modules.mood import MoodModule

        assert MoodModule.encrypted_state is True


# ── Save + load roundtrip with encryption ───────────────────────────────


class TestSaveLoadRoundtrip:
    def test_save_then_load_encrypted(self, key_path, data_dir):
        from helios.crypto import generate_key
        from helios.modules.base import BaseMod

        generate_key()

        class SensitiveMod(BaseMod):
            encrypted_state = True
            MODULE_MANIFEST = {**BaseMod.MODULE_MANIFEST, "name": "sensitive"}

            def tick(self):
                return {}

        mod = SensitiveMod()
        original = {"lat": 51.0447, "lon": -114.0719, "zone": "home"}

        saved = mod._save_state_encrypted("location_state.json", original)
        assert saved.suffix == ".enc"
        assert saved.exists()

        loaded = mod._load_state_encrypted("location_state.json")
        assert loaded == original

    def test_save_then_load_plaintext(self, key_path, data_dir):
        from helios.modules.base import BaseMod

        class PlainMod(BaseMod):
            encrypted_state = False
            MODULE_MANIFEST = {**BaseMod.MODULE_MANIFEST, "name": "plain"}

            def tick(self):
                return {}

        mod = PlainMod()
        original = {"temp": 22, "condition": "sunny"}

        saved = mod._save_state_encrypted("weather_state.json", original)
        assert saved.suffix == ".json"

        loaded = mod._load_state_encrypted("weather_state.json")
        assert loaded == original


# ── Key file corruption handling ────────────────────────────────────────


class TestKeyCorruption:
    def test_missing_key_returns_empty_state(self, key_path, data_dir):
        """If the key file is missing and an encrypted file exists, return empty dict."""
        # Create an encrypted file with a temporary key
        from helios.crypto import generate_key, encrypt_data

        key = generate_key()

        enc_path = data_dir / "test_state.json.enc"
        token = encrypt_data({"secret": "data"})
        enc_path.write_bytes(token)

        # Now delete the key — decrypt should return empty dict (not crash)
        key_path.unlink()

        from helios.modules.base import BaseMod

        class TestMod(BaseMod):
            encrypted_state = True
            MODULE_MANIFEST = {**BaseMod.MODULE_MANIFEST, "name": "test"}

            def tick(self):
                return {}

        mod = TestMod()
        result = mod._load_state_encrypted("test_state.json")
        assert result == {}