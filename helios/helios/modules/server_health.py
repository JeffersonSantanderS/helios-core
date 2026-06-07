"""Helios v6 — Server Health Module.

Pings configured home servers every tick and reports reachability.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from .base import BaseMod

log = logging.getLogger("helios.modules.server_health")

# Server inventory — loaded from env/config, falls back to data file
def _load_servers() -> dict[str, str]:
    """Load server inventory from env var or data file."""
    servers_json = os.environ.get("HELIOS_SERVERS", "")
    if servers_json:
        try:
            return json.loads(servers_json)
        except (json.JSONDecodeError, TypeError):
            log.warning("Invalid HELIOS_SERVERS env var, expected JSON dict")
    # Try data file
    servers_file = Path.home() / ".hermes" / "helios" / "data" / "servers.json"
    if servers_file.exists():
        try:
            return json.loads(servers_file.read_text())
        except (json.JSONDecodeError, TypeError):
            pass
    return {}

SERVERS = _load_servers()

CPU_THRESHOLD = 90  # %


class ServerHealthModule(BaseMod):
    MODULE_MANIFEST = {
        "name": "server",
        "version": "6.0.0",
        "priority": 1,
        "description": "Monitors home server health via ping + CPU",
    }

    def _run_tick(self, db) -> dict[str, Any]:
        results: dict[str, Any] = {}
        high_cpu_server = None

        for name, ip in SERVERS.items():
            reachable = self._ping(ip)
            key_prefix = f"{'ops' if name == 'ops' else name}"
            results[f"{key_prefix}_reachable"] = reachable

            if reachable:
                cpu = self._check_cpu(ip)
                if cpu is not None:
                    results[f"{key_prefix}_cpu"] = cpu
                    if cpu > CPU_THRESHOLD:
                        high_cpu_server = name
                        log.warning("Server %s CPU at %.0f%%", name, cpu)

        if high_cpu_server:
            results["high_cpu"] = high_cpu_server

        for key, val in results.items():
            if key not in ("high_cpu",):
                db.set_context("v6_migration", "server", key, val)

        return results

    def tick(self) -> dict[str, Any]:
        """Abstract implementation — delegates to _run_tick with no DB."""
        return self._run_tick(None)

    def _ping(self, ip: str) -> bool:
        """Return True if server responds to ping."""
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "3", ip],
                capture_output=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def _check_cpu(self, ip: str) -> float | None:
        """SSH into server and check CPU usage. Returns None on failure.

        Uses key-based SSH authentication only (no passwords).
        Requires HELIOS_SSH_KEY env var pointing to a private key,
        or relies on ssh-agent/default SSH config.

        Runs ``nproc`` remotely to get core count, then computes CPU % from
        the 1-minute load average.  Falls back to reading ``/proc/cpuinfo``
        lines when ``nproc`` is unavailable.
        """
        ssh_user = os.environ.get("HELIOS_SSH_USER", os.environ.get("USER", ""))
        ssh_key = os.environ.get("HELIOS_SSH_KEY", "")
        ssh_args = [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=3",
        ]
        if ssh_key:
            ssh_args.extend(["-i", ssh_key])
        ssh_args.append(f"{ssh_user}@{ip}" if ssh_user else ip)

        try:
            core_cmd = "nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 4"
            cmd = (
                f'cores=$({core_cmd}); '
                'awk -v c="$cores" \'{printf "%.1f", $1 * 100 / c}\' /proc/loadavg'
            )
            result = subprocess.run(
                ssh_args + [cmd],
                capture_output=True, text=True, timeout=8
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception:
            pass
        return None
