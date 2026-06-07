"""Helios module: System Health - Monitors collectors, disk, memory, and overall health."""
from __future__ import annotations
import logging
import subprocess
import json
from pathlib import Path
from typing import Any

from .base import BaseMod

log = logging.getLogger("helios.modules.system_health")

# Collector services Helios depends on (v5: services we actually care about)
COLLECTOR_SERVICES = [
    "health-api.service",
]


class SystemHealthModule(BaseMod):
    """Monitors system health: collectors, disk, memory."""

    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "system",
        "version": "1.0.0",
        "description": "Monitors collector services, disk usage, and system health",
        "author": "system",
        "collectors": [],
        "dependencies": [],
        "priority": 0,  # run first
    }

    def __init__(self, db_path=None, config=None):
        super().__init__(db_path=db_path, config=config)

    def tick(self) -> dict[str, Any]:
        """Check all collector services and system health."""
        result: dict[str, Any] = {}
        crashed_collectors: list[str] = []

        for service in COLLECTOR_SERVICES:
            name = service.replace(".service", "").replace("helios-", "")
            status = self._check_service(service)
            result[f"collector_{name}_active"] = status["active"]
            result[f"collector_{name}_state"] = status["state"]

            if not status["active"]:
                crashed_collectors.append(name)
                log.warning("Collector %s is DOWN (state=%s)", name, status["state"])

        # Set collector_down to comma-separated list of crashed collectors, or empty string
        result["collector_down"] = ",".join(crashed_collectors) if crashed_collectors else ""
        result["collector_count_total"] = len(COLLECTOR_SERVICES)
        result["collector_count_active"] = len(COLLECTOR_SERVICES) - len(crashed_collectors)

        # Check disk usage
        disk = self._check_disk()
        result.update(disk)

        # Check memory
        mem = self._check_memory()
        result.update(mem)

        return result

    def health(self) -> dict[str, Any]:
        return {"status": "healthy", "name": self.name}

    def _check_service(self, service_name: str) -> dict[str, Any]:
        """Check if a user systemd service is active."""
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", service_name],
                capture_output=True, text=True, timeout=5
            )
            state = result.stdout.strip()
            return {
                "active": state == "active",
                "state": state,
            }
        except Exception as exc:
            log.debug("Failed to check %s: %s", service_name, exc)
            return {"active": False, "state": f"error: {exc}"}

    def _check_disk(self) -> dict[str, Any]:
        """Check disk usage on main partitions."""
        try:
            result = subprocess.run(
                ["df", "-h", "/", "/home"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split("\n")[1:]
            disk_info = {}
            for line in lines:
                parts = line.split()
                if len(parts) >= 6:
                    mount = parts[5]
                    pct = parts[4].replace("%", "")
                    try:
                        disk_info[f"disk_{mount.replace('/', 'root').lstrip('_')}_pct"] = int(pct)
                    except ValueError:
                        pass
            return disk_info
        except Exception:
            return {}

    def _check_memory(self) -> dict[str, Any]:
        """Check memory usage."""
        try:
            result = subprocess.run(
                ["free", "-m"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split("\n")
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 3:
                    return {
                        "memory_total_mb": int(parts[1]),
                        "memory_used_mb": int(parts[2]),
                        "memory_free_mb": int(parts[3]),
                    }
        except Exception:
            pass
        return {}
