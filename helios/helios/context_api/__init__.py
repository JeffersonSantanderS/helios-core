"""Helios Context API — deterministic, privacy-safe context contract.

This module exposes a read-only FastAPI service that provides a stable,
sanitized JSON contract for consumers (agents, dashboards, integrations).
It reuses the privacy and data layers from helios.dashboard and applies
an additional sanitization pass to guarantee no private data or
infrastructure-specific paths leak into responses.
"""

__version__ = "1.0.0"