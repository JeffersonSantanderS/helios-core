"""Helios v7 — Test delivery ledger schema (delivery_attempts table)."""
import tempfile
import os

import pytest

from helios.state import HeliosDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db() -> tuple[HeliosDB, str]:
    """Create a temp-file-backed HeliosDB and return (db, path)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = HeliosDB(db_path=path)
    return db, path


def _cleanup(db: HeliosDB, path: str) -> None:
    db.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# test_delivery_ledger_table_exists
# ---------------------------------------------------------------------------

def test_delivery_ledger_table_exists():
    """The delivery_attempts table should exist with all expected columns."""
    db, path = _fresh_db()
    try:
        conn = db._conn()
        # PRAGMA table_info returns columns: cid, name, type, notnull, dflt_value, pk
        rows = conn.execute(
            "PRAGMA table_info(delivery_attempts)"
        ).fetchall()

        column_names = {row[1] for row in rows}

        expected_columns = {
            "id",
            "event_fingerprint",
            "event_type",
            "event_category",
            "event_source",
            "event_priority",
            "route",
            "channel_name",
            "success",
            "response_detail",
            "error_detail",
            "matrix_event_id",
            "ts",
        }

        assert expected_columns == column_names, (
            f"Missing columns: {expected_columns - column_names}, "
            f"Extra columns: {column_names - expected_columns}"
        )
    finally:
        _cleanup(db, path)


# ---------------------------------------------------------------------------
# test_delivery_ledger_insert_and_query
# ---------------------------------------------------------------------------

def test_delivery_ledger_insert_and_query():
    """Insert a delivery attempt row and verify it can be queried back."""
    db, path = _fresh_db()
    try:
        conn = db._conn()
        conn.execute(
            """INSERT INTO delivery_attempts
               (event_fingerprint, event_type, event_category, event_source,
                event_priority, route, channel_name, success, response_detail,
                error_detail, matrix_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "fp-abc123",
                "alert",
                "weather",
                "weather_bot",
                3,
                "matrix:room1",
                "matrix",
                1,
                "sent ok",
                None,
                "$event_id_1",
            ),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM delivery_attempts WHERE event_fingerprint = ?",
            ("fp-abc123",),
        ).fetchone()

        assert row is not None, "Expected one row but got None"
        assert row["event_fingerprint"] == "fp-abc123"
        assert row["event_type"] == "alert"
        assert row["event_category"] == "weather"
        assert row["event_source"] == "weather_bot"
        assert row["event_priority"] == 3
        assert row["route"] == "matrix:room1"
        assert row["channel_name"] == "matrix"
        assert row["success"] == 1
        assert row["response_detail"] == "sent ok"
        assert row["error_detail"] is None
        assert row["matrix_event_id"] == "$event_id_1"
        assert row["ts"] is not None
    finally:
        _cleanup(db, path)


# ---------------------------------------------------------------------------
# test_delivery_ledger_fingerprint_index
# ---------------------------------------------------------------------------

def test_delivery_ledger_fingerprint_index():
    """The idx_delivery_fingerprint index should exist on the delivery_attempts table."""
    db, path = _fresh_db()
    try:
        conn = db._conn()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='delivery_attempts'"
        ).fetchall()

        index_names = {row[0] for row in indexes}
        assert "idx_delivery_fingerprint" in index_names, (
            f"idx_delivery_fingerprint not found; indexes: {index_names}"
        )
    finally:
        _cleanup(db, path)