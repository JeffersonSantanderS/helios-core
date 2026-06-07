import importlib.util
import json
import sqlite3
from pathlib import Path

from helios import data_ingestion


_COLLECTOR_PATH = Path(__file__).resolve().parents[1] / "collectors" / "gmail_himalaya_collector.py"
_COLLECTOR_SPEC = importlib.util.spec_from_file_location("gmail_himalaya_collector", _COLLECTOR_PATH)
assert _COLLECTOR_SPEC and _COLLECTOR_SPEC.loader
gmail_collector = importlib.util.module_from_spec(_COLLECTOR_SPEC)
_COLLECTOR_SPEC.loader.exec_module(gmail_collector)


METRIC_DDL = """
CREATE TABLE metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    date_key TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'ingestion',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CONSTRAINT uq_metric_date UNIQUE (metric, date_key)
);
"""

TIMELINE_DDL = """
CREATE TABLE timeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_module TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    summary TEXT NOT NULL,
    metadata TEXT,
    date_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _make_db(tmp_path: Path) -> str:
    db_path = tmp_path / "helios.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(METRIC_DDL + TIMELINE_DDL)
    conn.commit()
    conn.close()
    return str(db_path)


def _write_signal(data_dir: Path, date_key: str, record: dict) -> None:
    path = data_dir / f"gmail_signals_{date_key}.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _hash(ch: str) -> str:
    return "sha256:" + ch * 64


def test_gmail_signal_jsonl_ingests_metrics_and_timeline_event(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(data_ingestion, "DATA_DIR", data_dir)
    db_path = _make_db(tmp_path)
    today = "2026-05-23"

    _write_signal(data_dir, today, {
        "schema_version": 1,
        "ts": "2026-05-23T12:00:00Z",
        "email_date": "2026-05-23T11:58:00Z",
        "message_id_hash": _hash("a"),
        "thread_id_hash": _hash("b"),
        "from_domain": "enmax.com",
        "sender_label": "ENMAX",
        "category": "bill",
        "summary": "Utility bill due soon.",
        "action_required": True,
        "due_date": "2026-06-04",
        "amount": 145.23,
        "importance": 0.8,
        "confidence": 0.91,
        "keywords": ["bill", "due"],
        "body_stored": False,
    })

    inserted = data_ingestion._ingest_gmail_signals(db_path, today)

    assert inserted == 1
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    metrics = {r["metric"]: r["value"] for r in conn.execute(
        "SELECT metric, value FROM metric_snapshots WHERE metric LIKE 'gmail.%'"
    )}
    assert metrics["gmail.signals_daily"] == 1
    assert metrics["gmail.action_required_daily"] == 1
    assert metrics["gmail.bills_daily"] == 1

    event = conn.execute(
        "SELECT event_type, source_module, importance, summary, metadata FROM timeline_events"
    ).fetchone()
    assert event["event_type"] == "email_finance_signal"
    assert event["source_module"] == "gmail"
    assert event["summary"] == "Billing email signal detected."
    metadata = json.loads(event["metadata"])
    assert metadata == {
        "category": "bill",
        "from_domain": "enmax.com",
        "message_id_hash": _hash("a"),
        "action_required": True,
        "due_date": "2026-06-04",
        "amount": 145.23,
        "confidence": 0.91,
        "body_stored": False,
    }
    assert "raw_body" not in metadata
    assert "body" not in metadata
    conn.close()


def test_gmail_ingestion_deduplicates_message_ids(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(data_ingestion, "DATA_DIR", data_dir)
    db_path = _make_db(tmp_path)
    today = "2026-05-23"
    record = {
        "schema_version": 1,
        "ts": "2026-05-23T12:00:00Z",
        "email_date": "2026-05-23T11:58:00Z",
        "message_id_hash": _hash("c"),
        "thread_id_hash": _hash("d"),
        "from_domain": "amazon.ca",
        "sender_label": "Amazon",
        "category": "delivery",
        "summary": "Package is out for delivery today.",
        "action_required": False,
        "due_date": None,
        "amount": None,
        "importance": 0.55,
        "confidence": 0.88,
        "keywords": ["delivery", "tracking"],
        "body_stored": False,
    }
    path = data_dir / f"gmail_signals_{today}.jsonl"
    path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8")

    first = data_ingestion._ingest_gmail_signals(db_path, today)
    second = data_ingestion._ingest_gmail_signals(db_path, today)

    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM timeline_events WHERE source_module = 'gmail'"
    ).fetchone()[0]
    metrics = {r[0]: r[1] for r in conn.execute(
        "SELECT metric, value FROM metric_snapshots WHERE metric LIKE 'gmail.%'"
    )}
    all_db_text = "\n".join(str(row) for row in conn.execute(
        "SELECT summary, metadata FROM timeline_events"
    ).fetchall())
    conn.close()

    assert first == 1
    assert second == 0
    assert count == 1
    assert metrics["gmail.signals_daily"] == 1
    assert metrics["gmail.deliveries_daily"] == 1
    assert "Package is out for delivery today." not in all_db_text


def test_gmail_ingestion_skips_low_confidence_and_raw_body_storage(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(data_ingestion, "DATA_DIR", data_dir)
    db_path = _make_db(tmp_path)
    today = "2026-05-23"
    record = {
        "schema_version": 1,
        "ts": "2026-05-23T12:00:00Z",
        "email_date": "2026-05-23T11:58:00Z",
        "message_id_hash": _hash("e"),
        "thread_id_hash": _hash("f"),
        "from_domain": "example.com",
        "sender_label": "Example",
        "category": "account_security",
        "summary": "Suspicious login detected.",
        "action_required": True,
        "due_date": None,
        "amount": None,
        "importance": 0.85,
        "confidence": 0.49,
        "keywords": ["security"],
        "raw_ref": "44",
        "body_stored": False,
    }
    _write_signal(data_dir, today, record)

    inserted = data_ingestion._ingest_gmail_signals(db_path, today)

    conn = sqlite3.connect(db_path)
    timeline_count = conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0]
    metric_count = conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0]
    conn.close()

    assert inserted == 0
    assert timeline_count == 0
    assert metric_count == 0


def test_collector_does_not_persist_raw_refs_subjects_or_display_names(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail_collector, "DATA_DIR", tmp_path)
    records = [{
        "schema_version": 1,
        "ts": "2026-05-23T12:00:00Z",
        "email_date": "2026-05-23T11:58:00Z",
        "message_id_hash": _hash("a"),
        "thread_id_hash": _hash("b"),
        "from_domain": "personal.example",
        "sender_label": gmail_collector._sender_label({"name": "Private Person"}, "personal.example"),
        "category": "appointment",
        "summary": "Schedule, booking, or travel confirmation detected.",
        "action_required": False,
        "due_date": None,
        "amount": None,
        "importance": 0.75,
        "confidence": 0.88,
        "keywords": ["appointment"],
        "raw_ref": "INBOX:12345",
        "subject": "Private medical appointment with exact address",
        "raw_subject": "Private medical appointment with exact address",
        "body": "PRIVATE BODY",
        "raw_body": "PRIVATE RAW BODY",
        "body_stored": False,
    }]

    _, jsonl_path, written = gmail_collector.write_outputs(records, lookback_days=7)

    persisted = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert written == 1
    assert persisted[0]["sender_label"] == "Personal"
    assert "raw_ref" not in persisted[0]
    assert "subject" not in persisted[0]
    assert "raw_subject" not in persisted[0]
    assert "body" not in persisted[0]
    assert "raw_body" not in persisted[0]
    assert "PRIVATE" not in jsonl_path.read_text(encoding="utf-8")


def test_gmail_ingestion_rejects_forbidden_raw_fields_and_uses_safe_summary(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(data_ingestion, "DATA_DIR", data_dir)
    db_path = _make_db(tmp_path)
    today = "2026-05-23"
    record = {
        "schema_version": 1,
        "ts": "2026-05-23T12:00:00Z",
        "email_date": "2026-05-23T11:58:00Z",
        "message_id_hash": _hash("0"),
        "thread_id_hash": _hash("1"),
        "from_domain": "example.com",
        "sender_label": "Example",
        "category": "account_security",
        "summary": "PRIVATE RAW SUBJECT AND BODY TEXT",
        "action_required": True,
        "due_date": None,
        "amount": None,
        "importance": 0.85,
        "confidence": 0.91,
        "keywords": ["security"],
        "raw_body": "PRIVATE RAW BODY",
        "body_stored": False,
    }
    _write_signal(data_dir, today, record)

    inserted = data_ingestion._ingest_gmail_signals(db_path, today)

    conn = sqlite3.connect(db_path)
    assert inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0] == 0
    conn.close()

    safe = dict(record)
    safe.pop("raw_body")
    safe["message_id_hash"] = _hash("2")
    safe["thread_id_hash"] = _hash("3")
    _write_signal(data_dir, today, safe)

    inserted = data_ingestion._ingest_gmail_signals(db_path, today)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT summary, metadata FROM timeline_events").fetchone()
    conn.close()

    assert inserted == 1
    assert row[0] == "Account security email signal detected."
    assert "PRIVATE" not in row[0]
    assert "PRIVATE" not in row[1]
