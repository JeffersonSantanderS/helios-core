"""Helios Dashboard — read-only local visibility layer.

Run with:
    python -m helios.dashboard.app

Default bind: 127.0.0.1:8199 (local only, never exposed to LAN).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .data import build_dashboard_snapshot, HELIOS_HOME, DATA_DIR
from .privacy import privacy_panel

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# ── App start time for uptime ──────────────────────────────────────────────────
_APP_START_TIME = time.time()

# ── Data source definitions ────────────────────────────────────────────────────
def _get_data_sources() -> dict[str, Path]:
    """Resolve data source paths dynamically so tests can patch HELIOS_HOME/DATA_DIR."""
    return {
        "latest_status": HELIOS_HOME / "latest_status.json",
        "context_export": HELIOS_HOME / "context_export.json",
        "alerts_recent": HELIOS_HOME / "alerts_recent.json",
        "channel_log": DATA_DIR / "channel_log.jsonl",
        "module_health": HELIOS_HOME / "data" / "module_health.json",
        "priority_engine": DATA_DIR / "priority_engine" / "latest.json",
    }


app = FastAPI(
    title="Helios Dashboard",
    version="0.3.0",
    description="Read-only local dashboard for Helios engine status",
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/status", response_class=JSONResponse)
async def api_status() -> dict:
    """Return the sanitized dashboard snapshot as JSON.

    Includes dashboard metadata: version, generated time, data source
    availability, and sanitizer version.
    """
    try:
        snapshot = build_dashboard_snapshot()
    except Exception as exc:
        log.exception("Dashboard snapshot failed: %s", exc)
        snapshot = {"error": str(exc), "health": "error"}

    # Dashboard metadata
    from . import __version__
    snapshot["dashboard_meta"] = {
        "dashboard_version": __version__,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "helios_home": str(HELIOS_HOME),
        "data_sources_present": [],
        "missing_sources": [],
        "sanitizer_version": "1.0",
    }
    for name, path in _get_data_sources().items():
        key = "data_sources_present" if path.exists() else "missing_sources"
        snapshot["dashboard_meta"][key].append(name)

    return snapshot


@app.get("/health", response_class=JSONResponse)
async def health() -> dict:
    """Health endpoint for monitoring. Privacy-safe — no secrets or personal data."""
    uptime_secs = time.time() - _APP_START_TIME
    # Determine health status from data availability
    status = "ok"
    critical_missing = []
    for name in ("latest_status", "context_export"):
        path = _get_data_sources().get(name)
        if path and not path.exists():
            critical_missing.append(name)

    if critical_missing:
        status = "degraded"

    return {
        "status": status,
        "service": "helios-dashboard",
        "version": "0.3.0",
        "uptime_secs": round(uptime_secs, 1),
        "bind": "127.0.0.1:8199",
        "mode": "read-only",
        "missing_sources": critical_missing,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Serve the dashboard HTML page."""
    index_path = STATIC_DIR / "dashboard.html"
    if index_path.exists():
        return index_path.read_text()
    return "<html><body><h1>Helios Dashboard</h1><p>Dashboard HTML not found.</p></body></html>"


def main() -> None:
    """Run the dashboard server locally."""
    import uvicorn
    uvicorn.run(
        "helios.dashboard.app:app",
        host="127.0.0.1",
        port=8199,
        log_level="info",
    )


if __name__ == "__main__":
    main()