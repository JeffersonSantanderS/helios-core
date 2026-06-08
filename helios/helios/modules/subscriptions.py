"""Helios v6 — Subscription / bill tracking module (SAN-121).

Scans Gmail signals (from the himalaya collector) for recurring bills and
subscription renewals, tracks them in a SQLite table, and sends renewal
alerts via the channel system when a subscription is about to renew.

Supports:
  - Automatic detection from email signals (keywords + amount extraction)
  - Manual entry via context API or direct DB insert
  - Billing cycle calculation (monthly / yearly / weekly)
  - Duplicate detection by (service, cycle) uniqueness constraint
  - 3-day-ahead renewal alerts
  - Encrypted state at rest (financial data is sensitive)
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .base import BaseMod

logger = logging.getLogger("helios.modules.subscriptions")

# ── Constants ────────────────────────────────────────────────────────────────

SOURCE_AUTO = "email"
SOURCE_MANUAL = "manual"

# Category classification heuristics
SUBSCRIPTION_CATEGORIES: dict[str, list[str]] = {
    "streaming": [
        "netflix", "spotify", "disney", "hulu", "hbo", "max",
        "apple.tv", "paramount", "crunchyroll", "youtube.premium",
        "youtube music", "tidal", "deezer",
    ],
    "cloud": [
        "icloud", "google one", "dropbox", "onedrive", "box",
        "apple one", "apple music",
    ],
    "utility": [
        "electric", "gas", "water", "utility", "hydro", "energy",
        "telus", "rogers", "bell", "shaw", "internet", "phone",
    ],
    "insurance": [
        "insurance", "policy", "premium",
    ],
    "software": [
        "github", "jetbrains", "adobe", "creative cloud",
        "microsoft 365", "notion", "slack", "zoom",
    ],
}

# Subject keywords that indicate a subscription/billing email
SUBJECT_KEYWORDS = [
    "renewal", "invoice", "payment", "subscription", "billing",
    "receipt", "renew", "charge", "order confirmation", "upcoming",
]

# Amount extraction pattern: $XX.XX or $X,XXX.XX or CAD XX.XX
_AMOUNT_PATTERN = re.compile(
    r"(?:\$|CAD\s*)(\d{1,5}(?:,\d{3})*(?:\.\d{2})?)",
    re.IGNORECASE,
)

# Days before next_due_date to trigger a renewal alert
ALERT_DAYS_BEFORE = 3


# ── Helper functions ────────────────────────────────────────────────────────

def _classify_category(service_name: str) -> str:
    """Classify a service name into a subscription category.

    Uses a scoring approach: for each category, count how many keywords
    match. The category with the most keyword hits wins. Ties are
    broken by a priority order (utility > insurance > streaming).
    """
    lower = service_name.lower()
    best_category = "other"
    best_score = 0
    # Priority order for tie-breaking (more important categories first)
    priority_order = ["utility", "insurance", "software", "cloud", "streaming"]

    for category, keywords in SUBSCRIPTION_CATEGORIES.items():
        score = sum(1 for kw in keywords if kw in lower)
        if score > best_score:
            best_score = score
            best_category = category
        elif score == best_score and score > 0:
            # Tie-breaking: prefer higher priority category
            if category in priority_order and (best_category not in priority_order or priority_order.index(category) < priority_order.index(best_category)):
                best_category = category
    return best_category


def _extract_amount_from_text(text: str) -> Optional[float]:
    """Extract a monetary amount from free text (e.g., email subject or body)."""
    match = _AMOUNT_PATTERN.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _compute_next_due_date(last_payment: str, cycle: str) -> Optional[str]:
    """Compute the next renewal date from last_payment and billing cycle.

    Args:
        last_payment: ISO date string (YYYY-MM-DD).
        cycle: One of 'monthly', 'yearly', 'weekly', 'one-time', 'unknown'.

    Returns:
        ISO date string (YYYY-MM-DD) or None if cannot compute.
    """
    try:
        dt = datetime.strptime(last_payment, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    delta_map = {
        "weekly": timedelta(weeks=1),
        "monthly": timedelta(days=30),   # approximate; fine for reminders
        "yearly": timedelta(days=365),
    }
    delta = delta_map.get(cycle)
    if delta is None:
        # one-time or unknown — can't compute next due
        return None

    next_dt = dt + delta
    return next_dt.strftime("%Y-%m-%d")


# ── Module ──────────────────────────────────────────────────────────────────

class SubscriptionsModule(BaseMod):
    """Tracks subscriptions and recurring bills from email signals and manual entry.

    On each tick:
      1. Scans new gmail_signals in the DB for subscription-related emails.
      2. Inserts or updates subscriptions in the ``subscriptions`` table.
      3. Checks for upcoming renewals (within ALERT_DAYS_BEFORE days).
      4. Returns context with active subscriptions and any alerts.
    """

    encrypted_state = True  # financial data

    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "subscriptions",
        "version": "1.0.0",
        "description": (
            "Tracks recurring bills and subscription renewals from email "
            "signals and manual entry, with renewal alerts"
        ),
        "author": "system",
        "collectors": ["gmail_himalaya"],
        "dependencies": [],
        "priority": 7,
    }

    def __init__(
        self,
        db_path: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(db_path=db_path, config=config)
        self._alert_days = int(self.config.get("alert_days_before", ALERT_DAYS_BEFORE))

    # ── DB access ───────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        assert self.db_path, "db_path required for SubscriptionsModule"
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── Email signal scanning ────────────────────────────────────────────

    def _scan_gmail_signals(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Scan the gmail_signals JSONL data for subscription-related signals.

        Looks at the gmail_signal context entries for category in
        (bill, renewal, subscription, receipt) that haven't been processed yet
        (i.e., not in email_scan_log).
        """
        # We look for context entries with module='gmail_himalaya' that
        # contain subscription-relevant categories.  Each entry has a value
        # dict with category, amount, sender_label, etc.
        try:
            # Get recent gmail context entries (last 30 days)
            since = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat()
            rows = conn.execute(
                """SELECT id, module, key, value, ts FROM context
                   WHERE module = 'gmail_himalaya'
                     AND ts >= ?
                   ORDER BY ts DESC""",
                (since,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

        signals = []
        for row in rows:
            import json
            try:
                value = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
            except (json.JSONDecodeError, TypeError):
                continue

            if not isinstance(value, dict):
                continue

            category = str(value.get("category", "")).lower()
            if category not in ("bill", "renewal", "subscription", "receipt"):
                continue

            # Check if already processed via email_scan_log
            msg_id = value.get("message_id_hash", "")
            if msg_id:
                existing = conn.execute(
                    "SELECT id FROM email_scan_log WHERE message_id = ?",
                    (msg_id,),
                ).fetchone()
                if existing:
                    continue

            signals.append({
                "context_id": row["id"],
                "message_id_hash": msg_id,
                "category": category,
                "sender_label": value.get("sender_label", "Unknown"),
                "from_domain": value.get("from_domain", "unknown"),
                "amount": value.get("amount"),
                "due_date": value.get("due_date"),
                "keywords": value.get("keywords", []),
                "summary": value.get("summary", ""),
                "ts": row["ts"],
                "importance": value.get("importance", 0.5),
            })

        return signals

    def _signal_to_subscription(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Convert a gmail signal dict to a subscription record dict."""
        # Derive service name from sender_label or domain
        sender_label = signal.get("sender_label", "Unknown")
        from_domain = signal.get("from_domain", "")

        # Try to infer a readable service name
        service = sender_label
        if service in ("Unknown", "") and from_domain:
            # Use domain as service name, clean it up
            parts = from_domain.split(".")
            service = parts[0].title() if parts else from_domain

        # Classify category
        sub_category = _classify_category(service)

        # Determine billing cycle from keywords or default to monthly
        cycle = "monthly"
        keywords = signal.get("keywords", [])
        keyword_text = " ".join(keywords).lower() if keywords else ""
        if any(kw in keyword_text for kw in ("yearly", "annual", "year")):
            cycle = "yearly"
        elif any(kw in keyword_text for kw in ("weekly", "week")):
            cycle = "weekly"

        # Amount
        amount = signal.get("amount")
        if amount is None:
            amount = 0.0
        else:
            try:
                amount = float(amount)
            except (ValueError, TypeError):
                amount = 0.0

        # Due date → last_payment, then compute next_renewal
        due_date = signal.get("due_date")
        last_payment = due_date or signal.get("ts", "")[:10] if signal.get("ts") else None

        # Compute next_renewal from last_payment + cycle
        next_renewal = None
        if last_payment:
            next_renewal = _compute_next_due_date(last_payment, cycle)

        return {
            "service": service,
            "provider": from_domain or "unknown",
            "amount": amount,
            "currency": "CAD",
            "cycle": cycle,
            "last_payment": last_payment,
            "next_renewal": next_renewal,
            "source_email": from_domain,
            "source_msgid": signal.get("message_id_hash", ""),
            "confidence": signal.get("importance", 0.5),
            "category": sub_category,
        }

    def _upsert_subscription(
        self,
        conn: sqlite3.Connection,
        sub: dict[str, Any],
    ) -> int:
        """Insert or update a subscription. Returns the subscription id."""
        # Check for existing subscription by (service, cycle)
        existing = conn.execute(
            "SELECT id, next_renewal, alert_sent FROM subscriptions WHERE service = ? AND cycle = ?",
            (sub["service"], sub["cycle"]),
        ).fetchone()

        if existing:
            # Update existing subscription
            sub_id = existing["id"]
            # Reset alert_sent if next_renewal changed
            new_next_renewal = sub.get("next_renewal")
            old_next_renewal = existing["next_renewal"]
            alert_sent = existing["alert_sent"]

            if new_next_renewal != old_next_renewal:
                alert_sent = 0  # Reset alert when renewal date changes

            conn.execute(
                """UPDATE subscriptions SET
                   amount = ?, currency = ?, provider = ?,
                   last_payment = ?, next_renewal = ?,
                   confidence = ?, is_active = 1, alert_sent = ?,
                   source_email = ?, source_msgid = ?,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE id = ?""",
                (
                    sub["amount"],
                    sub["currency"],
                    sub.get("provider", "unknown"),
                    sub.get("last_payment"),
                    new_next_renewal,
                    sub.get("confidence", 0.5),
                    alert_sent,
                    sub.get("source_email", ""),
                    sub.get("source_msgid", ""),
                    sub_id,
                ),
            )
            return sub_id
        else:
            # Insert new subscription
            cursor = conn.execute(
                """INSERT INTO subscriptions
                   (service, provider, amount, currency, cycle,
                    last_payment, next_renewal, source_email, source_msgid,
                    confidence, is_active, alert_sent, notes, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
                (
                    sub["service"],
                    sub.get("provider", "unknown"),
                    sub["amount"],
                    sub["currency"],
                    sub["cycle"],
                    sub.get("last_payment"),
                    sub.get("next_renewal"),
                    sub.get("source_email", ""),
                    sub.get("source_msgid", ""),
                    sub.get("confidence", 0.5),
                    sub.get("notes", ""),
                ),
            )
            return cursor.lastrowid or 0

    def _mark_signal_processed(
        self,
        conn: sqlite3.Connection,
        signal: dict[str, Any],
        subscription_id: int,
    ) -> None:
        """Mark an email signal as processed in email_scan_log."""
        conn.execute(
            """INSERT OR IGNORE INTO email_scan_log
               (message_id, sender, subject, result, subscription_id)
               VALUES (?, ?, ?, 'detected', ?)""",
            (
                signal.get("message_id_hash", ""),
                signal.get("from_domain", ""),
                signal.get("summary", "")[:200],
                subscription_id,
            ),
        )

    # ── Renewal alert check ──────────────────────────────────────────────

    def _check_renewal_alerts(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Check for subscriptions due within alert_days_before days.

        Returns a list of alert dicts for subscriptions that need notification.
        """
        today = datetime.now(timezone.utc).date()
        alert_cutoff = (today + timedelta(days=self._alert_days)).strftime("%Y-%m-%d")

        rows = conn.execute(
            """SELECT * FROM subscriptions
               WHERE is_active = 1
                 AND next_renewal IS NOT NULL
                 AND next_renewal <= ?
                 AND alert_sent = 0
               ORDER BY next_renewal""",
            (alert_cutoff,),
        ).fetchall()

        alerts = []
        for row in rows:
            row_dict = dict(row)
            renewal_date = row_dict["next_renewal"]
            try:
                renewal_dt = datetime.strptime(renewal_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                days_until = (renewal_dt.date() - today).days
            except (ValueError, TypeError):
                days_until = 0

            alerts.append({
                "id": row_dict["id"],
                "service": row_dict["service"],
                "amount": row_dict["amount"],
                "currency": row_dict["currency"],
                "cycle": row_dict["cycle"],
                "next_renewal": renewal_date,
                "days_until": days_until,
                "category": row_dict.get("notes", "") or "subscription",
            })

        return alerts

    # ── Manual entry ────────────────────────────────────────────────────

    def add_subscription(self, sub: dict[str, Any]) -> int:
        """Manually add a subscription. Returns the subscription id.

        Args:
            sub: Dict with keys: service (required), amount, currency, cycle,
                 next_due_date (optional ISO date), category, provider.

        Returns:
            The id of the inserted/updated subscription.
        """
        if not self.db_path:
            logger.warning("subscriptions: no db_path, cannot add manual subscription")
            return -1

        service = sub.get("service", "Unknown")
        amount = float(sub.get("amount", 0.0))
        currency = sub.get("currency", "CAD")
        cycle = sub.get("cycle", "monthly")
        if cycle not in ("monthly", "yearly", "weekly", "one-time", "unknown"):
            cycle = "monthly"
        category = sub.get("category") or _classify_category(service)
        next_due_date = sub.get("next_due_date")
        provider = sub.get("provider", "manual")

        # Compute next_renewal from next_due_date or today
        last_payment = next_due_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        next_renewal = _compute_next_due_date(last_payment, cycle) if cycle not in ("one-time",) else next_due_date

        conn = self._get_conn()
        try:
            sub_id = self._upsert_subscription(conn, {
                "service": service,
                "provider": provider,
                "amount": amount,
                "currency": currency,
                "cycle": cycle,
                "last_payment": last_payment,
                "next_renewal": next_renewal or next_due_date,
                "source_email": "manual",
                "source_msgid": "",
                "confidence": 1.0,
                "notes": category,
            })
            conn.commit()
            logger.info("subscriptions: manually added %s (id=%d)", service, sub_id)
            return sub_id
        finally:
            conn.close()

    # ── Health ────────────────────────────────────────────────────────────

    def module_info(self) -> dict[str, Any]:
        """Return module metadata including encrypted_state flag."""
        info = super().module_info()
        info["encrypted_state"] = self.encrypted_state
        return info

    def health(self) -> dict[str, Any]:
        """Report number of active subscriptions and next upcoming renewal."""
        h = {"status": "healthy", "name": self.name, "encrypted_state": self.encrypted_state}

        if not self.db_path:
            h["status"] = "degraded"
            h["error"] = "No db_path configured"
            h["active_subscriptions"] = 0
            return h

        try:
            conn = self._get_conn()
            try:
                active = conn.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE is_active = 1"
                ).fetchone()[0]
                next_row = conn.execute(
                    """SELECT service, next_renewal, amount, currency, cycle
                       FROM subscriptions
                       WHERE is_active = 1 AND next_renewal IS NOT NULL
                       ORDER BY next_renewal LIMIT 1"""
                ).fetchone()

                h["active_subscriptions"] = active
                if next_row:
                    h["next_renewal"] = {
                        "service": next_row["service"],
                        "date": next_row["next_renewal"],
                        "amount": next_row["amount"],
                        "currency": next_row["currency"],
                        "cycle": next_row["cycle"],
                    }
                else:
                    h["next_renewal"] = None
            finally:
                conn.close()
        except Exception as exc:
            h["status"] = "degraded"
            h["error"] = str(exc)
            h["active_subscriptions"] = 0

        return h

    # ── Main tick ─────────────────────────────────────────────────────────

    def tick(self) -> dict[str, Any]:
        """Run one subscription scanning pass.

        1. Scan gmail signals for subscription-related emails.
        2. Upsert detected subscriptions.
        3. Check for upcoming renewals (within alert window).
        4. Return context with current state and any alerts.
        """
        result: dict[str, Any] = {"source": "subscriptions"}

        if not self.db_path:
            result["_warning"] = "No db_path configured"
            result["active_subscriptions"] = 0
            return result

        alerts: list[dict[str, Any]] = []
        new_detected = 0
        active_count = 0

        try:
            conn = self._get_conn()
            try:
                # ── 1. Scan gmail signals ────────────────────────────────
                try:
                    signals = self._scan_gmail_signals(conn)
                except Exception as exc:
                    logger.warning("subscriptions: gmail signal scan failed: %s", exc)
                    signals = []

                # ── 2. Upsert detected subscriptions ──────────────────────
                for signal in signals:
                    try:
                        sub = self._signal_to_subscription(signal)
                        sub_id = self._upsert_subscription(conn, sub)
                        self._mark_signal_processed(conn, signal, sub_id)
                        new_detected += 1
                        logger.info(
                            "subscriptions: detected %s ($%.2f/%s)",
                            sub["service"], sub["amount"], sub["cycle"],
                        )
                    except Exception as exc:
                        logger.warning("subscriptions: failed to process signal: %s", exc)

                # ── 3. Check for upcoming renewal alerts ──────────────────
                try:
                    alerts = self._check_renewal_alerts(conn)
                except Exception as exc:
                    logger.warning("subscriptions: renewal alert check failed: %s", exc)

                # Mark alerts as sent
                for alert in alerts:
                    conn.execute(
                        "UPDATE subscriptions SET alert_sent = 1 WHERE id = ?",
                        (alert["id"],),
                    )

                # ── 4. Get active count ──────────────────────────────────
                active_count = conn.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE is_active = 1"
                ).fetchone()[0]

                conn.commit()
            finally:
                conn.close()

        except Exception as exc:
            logger.exception("subscriptions: tick failed: %s", exc)
            result["_error"] = str(exc)
            return result

        result["active_subscriptions"] = active_count
        result["new_detected"] = new_detected

        if alerts:
            result["renewal_alerts"] = alerts
            result["_renewal_alert_count"] = len(alerts)
            for alert in alerts:
                logger.info(
                    "subscriptions: renewal alert — %s ($%.2f) renews in %d days (%s)",
                    alert["service"],
                    alert["amount"],
                    alert["days_until"],
                    alert["next_renewal"],
                )

        return result