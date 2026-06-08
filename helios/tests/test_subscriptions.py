"""Tests for helios.modules.subscriptions (SAN-121)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from helios.modules.subscriptions import (
    ALERT_DAYS_BEFORE,
    SubscriptionsModule,
    _classify_category,
    _compute_next_due_date,
    _extract_amount_from_text,
)


# ── Schema DDL for test DBs ──────────────────────────────────────────────────

SCHEMA_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service         TEXT    NOT NULL,
    provider        TEXT    NOT NULL DEFAULT 'unknown',
    amount          REAL    NOT NULL DEFAULT 0.0,
    currency        TEXT    NOT NULL DEFAULT 'CAD',
    cycle           TEXT    NOT NULL DEFAULT 'monthly'
                    CHECK (cycle IN ('monthly', 'yearly', 'weekly', 'one-time', 'unknown')),
    last_payment    TEXT,
    next_renewal    TEXT,
    source_email    TEXT,
    source_msgid    TEXT,
    confidence      REAL    NOT NULL DEFAULT 0.5,
    is_active       INTEGER NOT NULL DEFAULT 1,
    alert_sent      INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    category        TEXT    NOT NULL DEFAULT 'other',
    CONSTRAINT uq_subscription_service_cycle UNIQUE (service, cycle)
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_next_renewal
    ON subscriptions (next_renewal, is_active, alert_sent);
CREATE INDEX IF NOT EXISTS idx_subscriptions_active
    ON subscriptions (is_active, cycle);
CREATE INDEX IF NOT EXISTS idx_subscriptions_service
    ON subscriptions (service);

CREATE TABLE IF NOT EXISTS email_scan_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT    NOT NULL UNIQUE,
    sender          TEXT,
    subject         TEXT,
    scan_date       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    result          TEXT    NOT NULL DEFAULT 'skipped',
    subscription_id INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT fk_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
);

CREATE INDEX IF NOT EXISTS idx_email_scan_msgid ON email_scan_log (message_id);

CREATE TABLE IF NOT EXISTS context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    module TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT,
    UNIQUE(module, key, source)
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    description TEXT
);
"""


@pytest.fixture
def fresh_db(tmp_path: Path) -> str:
    """Provide a temporary SQLite DB with subscriptions and related tables ready."""
    db_path = str(tmp_path / "test_helios.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_DDL)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def module(fresh_db: str) -> SubscriptionsModule:
    """Return a SubscriptionsModule wired to a temp DB."""
    return SubscriptionsModule(db_path=fresh_db, config={})


# ── test_subscriptions_module_info ────────────────────────────────────────────

def test_subscriptions_module_info(module: SubscriptionsModule):
    info = module.module_info()
    assert info["name"] == "subscriptions"
    assert info["version"] == "1.0.0"
    assert "subscription" in info["description"].lower()
    assert info["health"]["status"] == "healthy"
    assert "gmail_himalaya" in info["collectors"]
    assert info["encrypted_state"] is True


# ── test_subscriptions_create_subscription ────────────────────────────────────

def test_subscriptions_create_subscription(fresh_db: str):
    """Manually creating a subscription should store it in the DB."""
    mod = SubscriptionsModule(db_path=fresh_db, config={})
    sub_id = mod.add_subscription({
        "service": "Netflix",
        "amount": 20.99,
        "currency": "CAD",
        "cycle": "monthly",
        "category": "streaming",
    })
    assert sub_id > 0

    conn = sqlite3.connect(fresh_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["service"] == "Netflix"
    assert row["amount"] == 20.99
    assert row["cycle"] == "monthly"
    assert row["is_active"] == 1
    assert row["currency"] == "CAD"


# ── test_subscriptions_detect_from_email_signal ───────────────────────────────

def test_subscriptions_detect_from_email_signal(fresh_db: str):
    """Module tick should detect subscription signals from gmail context rows."""
    # Insert a gmail context signal
    conn = sqlite3.connect(fresh_db)
    conn.row_factory = sqlite3.Row
    signal_value = json.dumps({
        "category": "subscription",
        "sender_label": "Netflix",
        "from_domain": "netflix.com",
        "amount": 20.99,
        "keywords": ["subscription", "monthly"],
        "summary": "Subscription email signal detected.",
        "importance": 0.65,
        "message_id_hash": "sha256:abc123test",
    })
    conn.execute(
        "INSERT INTO context (source, module, key, value) VALUES (?, ?, ?, ?)",
        ("himalaya", "gmail_himalaya", "signal_netflix", signal_value),
    )
    conn.commit()
    conn.close()

    mod = SubscriptionsModule(db_path=fresh_db, config={})
    result = mod.tick()

    assert result["source"] == "subscriptions"
    assert result["new_detected"] >= 1

    # Verify the subscription was stored
    conn = sqlite3.connect(fresh_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM subscriptions WHERE service = 'Netflix'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["amount"] == 20.99
    assert row["cycle"] == "monthly"


# ── test_subscriptions_upcoming_renewal_alert ──────────────────────────────────

def test_subscriptions_upcoming_renewal_alert(fresh_db: str):
    """Subscriptions with next_renewal within 3 days should trigger an alert."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    two_days_ahead = (
        datetime.now(timezone.utc) + timedelta(days=2)
    ).strftime("%Y-%m-%d")

    mod = SubscriptionsModule(db_path=fresh_db, config={})
    # Insert a subscription with renewal in 2 days
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        """INSERT INTO subscriptions
           (service, provider, amount, currency, cycle, next_renewal, is_active, alert_sent)
           VALUES (?, ?, ?, ?, ?, ?, 1, 0)""",
        ("Spotify", "spotify.com", 10.99, "CAD", "monthly", two_days_ahead),
    )
    conn.commit()
    conn.close()

    result = mod.tick()
    assert "renewal_alerts" in result
    assert result["_renewal_alert_count"] >= 1
    alert = result["renewal_alerts"][0]
    assert alert["service"] == "Spotify"
    assert alert["days_until"] <= 2


# ── test_subscriptions_billing_cycle_calculation ──────────────────────────────

class TestBillingCycleCalculation:
    def test_monthly_cycle(self):
        result = _compute_next_due_date("2026-06-01", "monthly")
        # Monthly adds ~30 days
        assert result is not None
        dt = datetime.strptime(result, "%Y-%m-%d")
        assert dt.month in (6, 7)  # June → July (30 days later)

    def test_yearly_cycle(self):
        result = _compute_next_due_date("2026-01-15", "yearly")
        assert result is not None
        assert result == "2027-01-10" or result.startswith("2027")  # 365 days later

    def test_weekly_cycle(self):
        result = _compute_next_due_date("2026-06-01", "weekly")
        assert result is not None
        # 7 days later
        dt = datetime.strptime(result, "%Y-%m-%d")
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        assert (dt.replace(tzinfo=timezone.utc) - base).days == 7

    def test_one_time_cycle_returns_none(self):
        result = _compute_next_due_date("2026-06-01", "one-time")
        assert result is None

    def test_unknown_cycle_returns_none(self):
        result = _compute_next_due_date("2026-06-01", "unknown")
        assert result is None

    def test_invalid_date_returns_none(self):
        result = _compute_next_due_date("not-a-date", "monthly")
        assert result is None

    def test_none_date_returns_none(self):
        result = _compute_next_due_date(None, "monthly")
        assert result is None


# ── test_subscriptions_manual_entry ──────────────────────────────────────────

def test_subscriptions_manual_entry(fresh_db: str):
    """Manual add_subscription should create a record marked as manual source."""
    mod = SubscriptionsModule(db_path=fresh_db, config={})
    sub_id = mod.add_subscription({
        "service": "iCloud+",
        "amount": 3.99,
        "currency": "CAD",
        "cycle": "monthly",
        "category": "cloud",
    })
    assert sub_id > 0

    conn = sqlite3.connect(fresh_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
    ).fetchone()
    conn.close()

    assert row["service"] == "iCloud+"
    assert row["amount"] == 3.99
    assert row["source_email"] == "manual"
    assert row["confidence"] == 1.0
    assert row["is_active"] == 1


# ── test_subscriptions_duplicate_detection ────────────────────────────────────

def test_subscriptions_duplicate_detection(fresh_db: str):
    """Inserting the same (service, cycle) should update, not duplicate."""
    mod = SubscriptionsModule(db_path=fresh_db, config={})

    sub_id_1 = mod.add_subscription({
        "service": "Netflix",
        "amount": 20.99,
        "cycle": "monthly",
    })
    sub_id_2 = mod.add_subscription({
        "service": "Netflix",
        "amount": 22.99,  # price increased
        "cycle": "monthly",
    })

    # Should be the same id (upsert, not new insert)
    assert sub_id_1 == sub_id_2

    conn = sqlite3.connect(fresh_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE service = 'Netflix' AND cycle = 'monthly'"
    ).fetchall()
    conn.close()

    # Only one row — no duplicates
    assert len(rows) == 1
    assert rows[0]["amount"] == 22.99  # updated to new price


# ── test_subscriptions_health_status ──────────────────────────────────────────

def test_subscriptions_health_status(fresh_db: str):
    """health() should report active subscription count and next renewal."""
    mod = SubscriptionsModule(db_path=fresh_db, config={})

    # Initially, no subscriptions
    health = mod.health()
    assert health["status"] == "healthy"
    assert health["active_subscriptions"] == 0
    assert health["next_renewal"] is None

    # Add a subscription with a future renewal date
    tomorrow = (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    conn = sqlite3.connect(fresh_db)
    conn.execute(
        """INSERT INTO subscriptions
           (service, provider, amount, currency, cycle, next_renewal, is_active)
           VALUES (?, ?, ?, ?, ?, ?, 1)""",
        ("Hulu", "hulu.com", 15.99, "CAD", "monthly", tomorrow),
    )
    conn.commit()
    conn.close()

    health = mod.health()
    assert health["active_subscriptions"] == 1
    assert health["next_renewal"] is not None
    assert health["next_renewal"]["service"] == "Hulu"
    assert health["next_renewal"]["amount"] == 15.99


# ── test_subscriptions_encrypted_state ─────────────────────────────────────────

def test_subscriptions_encrypted_state():
    """SubscriptionsModule should have encrypted_state = True."""
    assert SubscriptionsModule.encrypted_state is True

    # Also verify it's in the module_info output
    mod = SubscriptionsModule(db_path=None, config={})
    info = mod.module_info()
    assert info["encrypted_state"] is True


# ── Helper function tests ──────────────────────────────────────────────────

class TestClassifyCategory:
    def test_netflix_is_streaming(self):
        assert _classify_category("Netflix") == "streaming"

    def test_spotify_is_streaming(self):
        assert _classify_category("Spotify") == "streaming"

    def test_icloud_is_cloud(self):
        assert _classify_category("iCloud") == "cloud"

    def test_utility_company(self):
        assert _classify_category("Enmax Electric") == "utility"

    def test_unknown_is_other(self):
        assert _classify_category("RandomService") == "other"


class TestExtractAmount:
    def test_dollar_amount(self):
        assert _extract_amount_from_text("Your bill is $20.99") == 20.99

    def test_cad_amount(self):
        assert _extract_amount_from_text("Amount: CAD 50.00") == 50.0

    def test_large_amount_with_commas(self):
        assert _extract_amount_from_text("Total: $1,234.56") == 1234.56

    def test_no_amount(self):
        assert _extract_amount_from_text("No amount here") is None

    def test_integer_amount(self):
        assert _extract_amount_from_text("Fee: $10") == 10.0


# ── Edge cases ──────────────────────────────────────────────────────────────

def test_subscriptions_no_db_path():
    """Module should handle missing db_path gracefully."""
    mod = SubscriptionsModule(db_path=None, config={})
    result = mod.tick()
    assert result["source"] == "subscriptions"
    assert result.get("_warning") == "No db_path configured"

    health = mod.health()
    assert health["status"] == "degraded"

    # Manual add should return -1
    sub_id = mod.add_subscription({"service": "Test", "amount": 10.0})
    assert sub_id == -1


def test_subscriptions_alert_sent_flag(fresh_db: str):
    """After an alert is sent, alert_sent should be set to 1 so we don't re-alert."""
    two_days_ahead = (
        datetime.now(timezone.utc) + timedelta(days=2)
    ).strftime("%Y-%m-%d")

    mod = SubscriptionsModule(db_path=fresh_db, config={})
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        """INSERT INTO subscriptions
           (service, provider, amount, currency, cycle, next_renewal, is_active, alert_sent)
           VALUES (?, ?, ?, ?, ?, ?, 1, 0)""",
        ("Disney+", "disney.com", 11.99, "CAD", "monthly", two_days_ahead),
    )
    conn.commit()
    conn.close()

    # First tick — should detect the alert
    result1 = mod.tick()
    assert result1.get("_renewal_alert_count", 0) >= 1

    # Verify alert_sent was set
    conn = sqlite3.connect(fresh_db)
    row = conn.execute(
        "SELECT alert_sent FROM subscriptions WHERE service = 'Disney+'"
    ).fetchone()
    conn.close()
    assert row[0] == 1

    # Second tick — should NOT re-alert
    result2 = mod.tick()
    assert result2.get("_renewal_alert_count", 0) == 0


def test_subscriptions_inactive_not_alerted(fresh_db: str):
    """Inactive subscriptions should not trigger renewal alerts."""
    two_days_ahead = (
        datetime.now(timezone.utc) + timedelta(days=2)
    ).strftime("%Y-%m-%d")

    conn = sqlite3.connect(fresh_db)
    conn.execute(
        """INSERT INTO subscriptions
           (service, provider, amount, currency, cycle, next_renewal, is_active, alert_sent)
           VALUES (?, ?, ?, ?, ?, ?, 0, 0)""",
        ("Cancelled Service", "cancel.com", 5.99, "CAD", "monthly", two_days_ahead),
    )
    conn.commit()
    conn.close()

    mod = SubscriptionsModule(db_path=fresh_db, config={})
    result = mod.tick()
    assert result.get("_renewal_alert_count", 0) == 0


def test_subscriptions_custom_alert_days(fresh_db: str):
    """Configurable alert_days_before should work."""
    five_days_ahead = (
        datetime.now(timezone.utc) + timedelta(days=5)
    ).strftime("%Y-%m-%d")

    conn = sqlite3.connect(fresh_db)
    conn.execute(
        """INSERT INTO subscriptions
           (service, provider, amount, currency, cycle, next_renewal, is_active, alert_sent)
           VALUES (?, ?, ?, ?, ?, ?, 1, 0)""",
        ("Early Alert Service", "early.com", 7.99, "CAD", "monthly", five_days_ahead),
    )
    conn.commit()
    conn.close()

    # With default 3 days, 5 days away should NOT alert
    mod = SubscriptionsModule(db_path=fresh_db, config={})
    result = mod.tick()
    assert result.get("_renewal_alert_count", 0) == 0

    # With 7-day threshold, 5 days away SHOULD alert
    mod7 = SubscriptionsModule(db_path=fresh_db, config={"alert_days_before": 7})
    result7 = mod7.tick()
    assert result7.get("_renewal_alert_count", 0) >= 1