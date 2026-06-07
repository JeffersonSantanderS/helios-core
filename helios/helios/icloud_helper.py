"""Helios v6 — Shared iCloud helper.

All modules use this singleton to get a pre-authenticated PyiCloudService
with cookie_directory. No module needs to handle auth itself anymore.
"""

from __future__ import annotations

import logging
import os
from typing import Any

COOKIE_DIR = os.path.expanduser("~/.hermes/helios-v6/.icloud_session")
APPLE_ID = os.environ.get("ICLOUD_APPLE_ID", "")

logger = logging.getLogger("helios.icloud_helper")

_icloud_service: Any = None


def get_service() -> Any:
    """Return a shared PyiCloudService, creating it once with cookie_directory."""
    global _icloud_service

    if _icloud_service is not None:
        try:
            # Quick check if session is still valid
            if _icloud_service.is_trusted_session:
                return _icloud_service
        except Exception:
            pass

    try:
        from pyicloud import PyiCloudService

        if os.path.isdir(COOKIE_DIR):
            _icloud_service = PyiCloudService(
                APPLE_ID, "dummy", cookie_directory=COOKIE_DIR
            )
        else:
            _icloud_service = PyiCloudService(APPLE_ID, "dummy")

        if _icloud_service.requires_2fa:
            logger.warning(
                "iCloud session expired — re-run icloud_login.py"
            )
            return None

        logger.debug("iCloud service initialized (trusted=%s)", _icloud_service.is_trusted_session)
        return _icloud_service

    except ImportError:
        logger.debug("pyicloud not installed")
        return None
    except Exception as exc:
        logger.warning("iCloud init error: %s", exc)
        return None


def invalidate() -> None:
    """Force re-creation on next call."""
    global _icloud_service
    _icloud_service = None
