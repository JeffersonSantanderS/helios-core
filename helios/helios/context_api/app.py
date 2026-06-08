"""Helios Context API — deterministic, privacy-safe context contract.

Run with:
    python -m helios.context_api.app

Default bind: 127.0.0.1:8200 (local only, never exposed to LAN).

Provides two endpoints:
    GET /api/v1/context  — full sanitized context snapshot
    GET /api/v1/health   — lightweight health check

All responses are privacy-safe: no private data, no host paths,
no Santander-specific or corporate identifiers.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from helios.dashboard.data import load_json_safe, HELIOS_HOME

from . import __version__
from .sanitize import build_contract_context, sanitize_for_contract

log = logging.getLogger(__name__)

# ── App start time for uptime ──────────────────────────────────────────────
_APP_START_TIME = time.time()

app = FastAPI(
    title="Helios Context API",
    version=__version__,
    description=(
        "Deterministic, privacy-safe context contract API. "
        "No private data, no host paths, no corporate identifiers."
    ),
)


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/api/v1/context", response_class=JSONResponse)
async def get_context() -> dict[str, Any]:
    """Return the full sanitized context contract.

    The response is a deterministic, privacy-safe snapshot built from
    context_export.json and latest_status.json. Every value passes
    through the sanitization pipeline before leaving this endpoint.

    Contract stability: the key structure and value types are guaranteed
    to remain stable across releases. New keys may be added but existing
    keys will not be removed or retyped without a version bump.
    """
    payload: dict[str, Any]
    try:
        context_export = load_json_safe(HELIOS_HOME / "context_export.json")
        latest_status = load_json_safe(HELIOS_HOME / "latest_status.json")
        payload = build_contract_context(context_export, latest_status)
    except Exception as exc:
        log.exception("Context contract build failed: %s", exc)
        payload = {
            "error": "failed to build context contract",
            "health": "error",
        }

    # Add API metadata — no host paths
    payload["api_meta"] = {
        "api_version": __version__,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "sanitizer_version": sanitize_for_contract.__module__,
    }

    return payload


@app.get("/api/v1/health", response_class=JSONResponse)
async def get_health() -> dict[str, Any]:
    """Lightweight health check — privacy-safe, no personal data.

    Returns service status, version, uptime, and data availability
    without exposing any host paths or infrastructure details.
    """
    uptime_secs = time.time() - _APP_START_TIME

    # Check data source availability — no paths in response
    status = "ok"
    missing = []
    critical_sources = {
        "context_export": HELIOS_HOME / "context_export.json",
        "latest_status": HELIOS_HOME / "latest_status.json",
    }
    for name, path in critical_sources.items():
        if not path.exists():
            missing.append(name)

    if missing:
        status = "degraded"

    return {
        "status": status,
        "service": "helios-context-api",
        "version": __version__,
        "uptime_secs": round(uptime_secs, 1),
        "bind": "127.0.0.1:8200",
        "missing_sources": missing,
    }


# ── Server entrypoint ───────────────────────────────────────────────────────

def main() -> None:
    """Run the context API server locally."""
    import uvicorn

    uvicorn.run(
        "helios.context_api.app:app",
        host="127.0.0.1",
        port=8200,
        log_level="info",
    )


if __name__ == "__main__":
    main()