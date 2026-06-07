#!/usr/bin/env python3
"""Gmail → Helios summary-signal collector via Himalaya.

Privacy contract:
- No direct Gmail API / Google OAuth usage.
- Uses Himalaya IMAP metadata as agent-side access outside Helios.
- Does not write raw email bodies.
- Does not persist raw full subjects; writes category summaries only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
SCHEMA_VERSION = 1
SOURCE = "himalaya"
COLLECTOR = "gmail_himalaya_collector"
ALLOWLIST = {
    "bill",
    "receipt",
    "subscription",
    "renewal",
    "delivery",
    "appointment",
    "reservation",
    "travel",
    "account_security",
    "government",
    "banking",
    "insurance",
    "work",
    "family_plan",
    "urgent_notice",
}
IGNORE_KEYWORDS = {
    "unsubscribe",
    "newsletter",
    "sale",
    "discount",
    "% off",
    "promo",
    "promotion",
    "deal",
    "deals",
    "coupon",
    "reward",
    "rewards",
    "points",
    "friend gets",
    "refer",
    "social notification",
    "new follower",
}
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "bill": ["bill", "invoice", "payment due", "amount due", "due date", "statement", "balance due"],
    "receipt": ["receipt", "order receipt", "payment received", "transaction", "purchase confirmation"],
    "subscription": ["subscription", "membership", "plan", "monthly plan"],
    "renewal": ["renewal", "renews", "renewing", "expires soon", "expiration"],
    "delivery": ["shipped", "tracking", "delivered", "out for delivery", "package", "delivery"],
    "appointment": ["appointment", "booking", "scheduled", "rescheduled", "reminder"],
    "reservation": ["reservation", "reserved", "booking confirmation", "confirmed booking"],
    "travel": ["flight", "boarding", "itinerary", "hotel", "airbnb", "trip", "travel"],
    "account_security": ["security", "login", "password", "verification", "verify", "suspicious", "sign-in", "signin", "2fa", "two-factor"],
    "government": ["cra", "government", "service canada", "canada revenue", "tax", "notice of assessment"],
    "banking": ["bank", "credit card", "debit", "e-transfer", "etransfer", "deposit", "withdrawal"],
    "insurance": ["insurance", "policy", "claim", "premium"],
    "work": ["schedule", "shift", "job", "work order", "paystub", "pay statement"],
    "family_plan": ["family", "household", "shared plan"],
    "urgent_notice": ["urgent", "final notice", "past due", "overdue", "immediate action", "action required"],
}
IMPORTANCE = {
    "urgent_notice": 0.9,
    "account_security": 0.85,
    "bill": 0.7,
    "renewal": 0.7,
    "subscription": 0.65,
    "appointment": 0.75,
    "reservation": 0.75,
    "travel": 0.75,
    "delivery": 0.55,
    "receipt": 0.35,
    "government": 0.8,
    "banking": 0.75,
    "insurance": 0.75,
    "work": 0.7,
    "family_plan": 0.65,
}
FOLDERS = ["INBOX", "[Gmail]/All Mail"]
FORBIDDEN_PERSISTED_FIELDS = {
    "body", "raw_body", "subject", "raw_subject", "snippet", "content",
    "raw", "from", "sender", "raw_ref",
}
SAFE_PERSISTED_FIELDS = {
    "schema_version", "ts", "email_date", "message_id_hash", "thread_id_hash",
    "from_domain", "sender_label", "category", "summary", "action_required",
    "due_date", "amount", "importance", "confidence", "keywords", "body_stored",
}
SAFE_SUMMARIES = {
    "bill": "Billing email signal detected.",
    "renewal": "Renewal email signal detected.",
    "subscription": "Subscription email signal detected.",
    "delivery": "Delivery email signal detected.",
    "appointment": "Appointment email signal detected.",
    "reservation": "Reservation email signal detected.",
    "travel": "Travel email signal detected.",
    "account_security": "Account security email signal detected.",
    "government": "Government email signal detected.",
    "banking": "Banking email signal detected.",
    "insurance": "Insurance email signal detected.",
    "work": "Work email signal detected.",
    "family_plan": "Family plan email signal detected.",
    "urgent_notice": "Urgent email notice detected.",
    "receipt": "Receipt email signal detected.",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _domain(addr: str | None) -> str:
    if not addr or "@" not in addr:
        return "unknown"
    return addr.rsplit("@", 1)[1].strip().lower()


def _sender_label(from_obj: Any, domain: str) -> str:
    """Return a non-personal sender label derived only from the sender domain."""
    if domain == "unknown":
        return "Unknown"
    base = domain.split(".")[0].title()
    label = re.sub(r"[^A-Za-z0-9 ._&'-]", "", base).strip()
    return label[:48] or "Unknown"


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def _is_ignored(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in IGNORE_KEYWORDS)


def _classify(subject: str, sender_domain: str) -> tuple[str | None, list[str], float]:
    text = f"{subject} {sender_domain}".lower()
    if _is_ignored(text):
        return None, [], 0.0

    hits: list[tuple[str, list[str]]] = []
    for category, keywords in CATEGORY_KEYWORDS.items():
        found = [kw for kw in keywords if kw in text]
        if found:
            hits.append((category, found))

    if not hits:
        return None, [], 0.0

    priority = [
        "urgent_notice", "account_security", "government", "banking", "insurance",
        "bill", "renewal", "subscription", "appointment", "travel", "reservation",
        "delivery", "work", "family_plan", "receipt",
    ]
    found_categories = {cat for cat, _ in hits}
    category = next(cat for cat in priority if cat in found_categories)
    keywords = sorted({kw for cat, kws in hits if cat == category for kw in kws})[:8]
    confidence = min(0.95, 0.55 + 0.12 * len(keywords))
    return category, keywords, confidence


def _extract_amount(subject: str) -> float | None:
    match = re.search(r"(?:\$|CAD\s*)(\d{1,5}(?:,\d{3})*(?:\.\d{2})?)", subject, re.I)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_due_date(subject: str, email_dt: datetime | None) -> str | None:
    lower = subject.lower()
    if "today" in lower:
        return (email_dt or _utc_now()).date().isoformat()
    if "tomorrow" in lower:
        return ((email_dt or _utc_now()) + timedelta(days=1)).date().isoformat()
    match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", subject)
    if match:
        y, m, d = match.groups()
        try:
            return datetime(int(y), int(m), int(d), tzinfo=timezone.utc).date().isoformat()
        except ValueError:
            return None
    return None


def _summary_for(category: str, keywords: list[str], due_date: str | None, amount: float | None) -> str:
    if category == "delivery":
        if any("out for delivery" in k for k in keywords):
            return "Package is out for delivery today."
        if any("delivered" in k for k in keywords):
            return "Delivery confirmation detected."
        return "Delivery or tracking update detected."
    if category in {"bill", "renewal", "subscription"}:
        parts = ["Billing or renewal notice detected"]
        if due_date:
            parts.append(f"due {due_date}")
        if amount is not None:
            parts.append(f"amount ${amount:.2f}")
        return "; ".join(parts) + "."
    if category == "receipt":
        return "Receipt or purchase confirmation detected."
    if category in {"appointment", "reservation", "travel"}:
        return "Schedule, booking, or travel confirmation detected."
    if category == "account_security":
        return "Account security notice detected."
    if category == "urgent_notice":
        return "Urgent notice detected."
    if category == "government":
        return "Government notice detected."
    if category == "banking":
        return "Banking notice detected."
    if category == "insurance":
        return "Insurance notice detected."
    if category == "work":
        return "Work-related email signal detected."
    if category == "family_plan":
        return "Family or household plan email signal detected."
    return "Email life signal detected."


def _score_importance(category: str, due_date: str | None) -> float:
    base = IMPORTANCE.get(category, 0.5)
    if category == "bill" and due_date:
        try:
            due = datetime.fromisoformat(due_date).date()
            days = (due - _utc_now().date()).days
            if days <= 7:
                base = max(base, 0.8)
        except ValueError:
            pass
    return round(min(base, 0.95), 2)


def _himalaya_envelopes(folder: str, max_results: int, since_date: str) -> list[dict[str, Any]]:
    base = [
        "himalaya", "envelope", "list", "--quiet", "--folder", folder,
        "--page-size", str(max_results), "--output", "json",
    ]
    queries = [
        ["after", since_date, "order", "by", "date", "desc"],
        ["order", "by", "date", "desc"],
        [],
    ]
    for query in queries:
        try:
            result = subprocess.run(
                base + query,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception:
            continue
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    return []


def collect(lookback_days: int = 7, max_results: int = 50) -> list[dict[str, Any]]:
    now = _utc_now()
    cutoff = now - timedelta(days=lookback_days)
    since_date = cutoff.date().isoformat()
    candidates: dict[str, dict[str, Any]] = {}

    per_folder = max(1, max_results)
    for folder in FOLDERS:
        for env in _himalaya_envelopes(folder, per_folder, since_date):
            subject = str(env.get("subject") or "")
            email_dt = _parse_dt(env.get("date"))
            if email_dt and email_dt < cutoff:
                continue
            from_obj = env.get("from") or {}
            addr = from_obj.get("addr") if isinstance(from_obj, dict) else ""
            from_domain = _domain(addr)
            category, keywords, confidence = _classify(subject, from_domain)
            if not category or category not in ALLOWLIST:
                continue
            amount = _extract_amount(subject)
            due_date = _extract_due_date(subject, email_dt)
            base_id = "|".join([
                folder,
                str(env.get("id") or ""),
                str(env.get("date") or ""),
                from_domain,
                subject,
            ])
            message_id_hash = _hash(base_id)
            thread_id_hash = _hash("thread|" + base_id)
            if message_id_hash in candidates:
                continue
            signal = {
                "schema_version": SCHEMA_VERSION,
                "ts": now.isoformat().replace("+00:00", "Z"),
                "email_date": (email_dt or now).isoformat().replace("+00:00", "Z"),
                "message_id_hash": message_id_hash,
                "thread_id_hash": thread_id_hash,
                "from_domain": from_domain,
                "sender_label": _sender_label(from_obj, from_domain),
                "category": category,
                "summary": SAFE_SUMMARIES.get(category, "Email life signal detected."),
                "action_required": category in {"bill", "renewal", "subscription", "account_security", "urgent_notice", "government", "banking", "insurance"},
                "due_date": due_date,
                "amount": amount,
                "importance": _score_importance(category, due_date),
                "confidence": round(confidence, 2),
                "keywords": keywords,
                "body_stored": False,
            }
            candidates[message_id_hash] = signal
            if len(candidates) >= max_results:
                break
        if len(candidates) >= max_results:
            break

    return sorted(candidates.values(), key=lambda r: r.get("email_date", ""), reverse=True)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _safe_persisted_record(record: dict[str, Any]) -> dict[str, Any]:
    safe = {k: v for k, v in record.items() if k in SAFE_PERSISTED_FIELDS}
    category = str(safe.get("category") or "")
    domain = str(safe.get("from_domain") or "unknown")
    safe["sender_label"] = _sender_label({}, domain)
    safe["summary"] = SAFE_SUMMARIES.get(category, "Email life signal detected.")
    allowed_keywords = set(CATEGORY_KEYWORDS.get(category, []))
    safe["keywords"] = [
        kw for kw in safe.get("keywords", [])
        if isinstance(kw, str) and kw in allowed_keywords
    ]
    safe["body_stored"] = False
    return safe


def _append_jsonl_dedup(path: Path, records: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    sanitized_records: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                existing_record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(existing_record, dict):
                continue
            safe_existing = _safe_persisted_record(existing_record)
            mid = str(safe_existing.get("message_id_hash") or "")
            if not mid or mid in existing:
                continue
            existing.add(mid)
            sanitized_records.append(safe_existing)
    written = 0
    for record in records:
        safe = _safe_persisted_record(record)
        mid = str(safe.get("message_id_hash") or "")
        if not mid or mid in existing:
            continue
        sanitized_records.append(safe)
        existing.add(mid)
        written += 1
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as tmp:
        for safe in sanitized_records:
            tmp.write(json.dumps(safe, sort_keys=True) + "\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)
    return written


def write_outputs(records: list[dict[str, Any]], lookback_days: int) -> tuple[Path, Path, int]:
    now = _utc_now()
    today = now.date().isoformat()
    categories = sorted({str(r.get("category")) for r in records if r.get("category")})
    state = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "collector": COLLECTOR,
        "ts": now.isoformat().replace("+00:00", "Z"),
        "lookback_days": lookback_days,
        "last_success_at": now.isoformat().replace("+00:00", "Z"),
        "signals_today": sum(1 for r in records if str(r.get("email_date", ""))[:10] == today),
        "categories_seen": categories,
        "raw_body_storage": False,
    }
    state_path = DATA_DIR / "gmail_state.json"
    jsonl_path = DATA_DIR / f"gmail_signals_{today}.jsonl"
    _write_json_atomic(state_path, state)
    written = _append_jsonl_dedup(jsonl_path, records)
    return state_path, jsonl_path, written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect summary-only Gmail life signals via Himalaya.")
    parser.add_argument("--dry-run", action="store_true", help="Collect and print counts only; write nothing.")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--max-results", type=int, default=50)
    parser.add_argument("--write", action="store_true", help="Write gmail_state.json and gmail_signals_YYYY-MM-DD.jsonl.")
    args = parser.parse_args(argv)

    lookback_days = max(1, args.lookback_days)
    max_results = max(1, args.max_results)
    records = collect(lookback_days=lookback_days, max_results=max_results)
    counts = Counter(str(r.get("category")) for r in records)

    print(json.dumps({
        "collector": COLLECTOR,
        "source": SOURCE,
        "lookback_days": lookback_days,
        "max_results": max_results,
        "write": bool(args.write and not args.dry_run),
        "signals": len(records),
        "categories": dict(sorted(counts.items())),
        "raw_body_storage": False,
    }, sort_keys=True))

    if args.write and not args.dry_run:
        state_path, jsonl_path, written = write_outputs(records, lookback_days)
        print(json.dumps({
            "state_path": str(state_path),
            "jsonl_path": str(jsonl_path),
            "records_appended": written,
            "raw_body_storage": False,
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
