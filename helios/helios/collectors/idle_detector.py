"""Helios v5 — Idle Detector.

Standalone service that polls Windows idle time and writes to focus_state.json.
Runs alongside active_window_tracker but provides a simpler, lower-overhead
signal that doesn't require powershell to parse window titles.

Used by the Helios focus/sleep correlation engine.
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
IDLE_STATE = DATA_DIR / "idle_state.json"
POLL_INTERVAL = 60  # seconds

POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
MAX_VALID_IDLE_SEC = 28_800  # 8 hours — reject GetLastInputInfo garbage from Session 0

_last_idle_sec: int = 0
_last_activity_ts: float = time.time()


def _read_net_io() -> dict:
    """Read /proc/net/dev counters into a dict."""
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()
        result = {}
        for line in lines[2:]:
            parts = line.strip().split()
            if len(parts) >= 9:
                iface = parts[0].rstrip(":")
                result[iface] = {"rx": int(parts[1]), "tx": int(parts[9])}
        return result
    except Exception:
        return {}


def _net_has_activity(prev: dict, curr: dict) -> bool:
    """Return True if any non-loopback interface moved > 1KB."""
    for iface, stats in curr.items():
        if iface in prev and not iface.startswith("lo") and iface != "docker0":
            delta = max(0, stats["rx"] - prev[iface]["rx"])
            delta += max(0, stats["tx"] - prev[iface]["tx"])
            if delta > 1024:
                return True
    return False


def _read_logind_idle() -> bool | None:
    """Return True/False from logind IdleHint, or None if unavailable."""
    try:
        result = subprocess.run(
            ["loginctl", "show-session", "--property=IdleHint"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip().lower().endswith("yes")
    except Exception:
        pass
    return None


def get_idle_seconds() -> int | None:
    """Get system idle time in seconds via powershell, with WSL2 validation."""
    global _last_idle_sec, _last_activity_ts

    ps_script = """
Add-Type @"
using System;
using System.Runtime.InteropServices;
public struct LASTINPUTINFO {
    public uint cbSize;
    public uint dwTime;
}
public class IdleDetector {
    [DllImport("user32.dll")]
    public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
}
"@
$lii = New-Object LASTINPUTINFO
$lii.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf($lii)
$ok = [IdleDetector]::GetLastInputInfo([ref]$lii)
if (-not $ok) { Write-Output "FAIL"; exit 0 }
$idleMs = [Environment]::TickCount - $lii.dwTime
if ($lii.dwTime -eq 0 -or $idleMs -lt 0 -or $idleMs -gt 28800000) { Write-Output "GARBAGE"; exit 0 }
[Math]::Floor($idleMs / 1000)
"""
    try:
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
            cwd="/tmp"
        )
        val = result.stdout.strip()
        if val.isdigit():
            idle_sec = int(val)
            if 0 <= idle_sec <= MAX_VALID_IDLE_SEC:
                _last_idle_sec = idle_sec
                if idle_sec < 300:
                    _last_activity_ts = time.time()
                return idle_sec
    except Exception as exc:
        print(f"[idle] powershell error: {exc}", file=sys.stderr)

    # Fallback 1: logind session idle state
    logind_idle = _read_logind_idle()
    if logind_idle is not None:
        if not logind_idle:
            _last_activity_ts = time.time()
            _last_idle_sec = 0
            return 0
        else:
            elapsed = int(time.time() - _last_activity_ts)
            _last_idle_sec = elapsed
            return elapsed

    # Fallback 2: network I/O proxy
    net_prev = getattr(get_idle_seconds, "_net_cache", {})
    net_curr = _read_net_io()
    get_idle_seconds._net_cache = net_curr
    if _net_has_activity(net_prev, net_curr):
        _last_activity_ts = time.time()
        _last_idle_sec = 0
        return 0

    # No signal — return last known or conservative estimate
    elapsed = int(time.time() - _last_activity_ts)
    _last_idle_sec = elapsed
    return elapsed


def write_idle_state(idle_seconds: int) -> None:
    """Write idle_state.json for Helios focus module."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    is_idle = idle_seconds >= 300  # 5 minutes = idle

    state = {
        "idle_seconds": idle_seconds,
        "is_idle": is_idle,
        "last_seen": now.isoformat(),
    }

    # Append to daily log for session analysis
    daily_log = DATA_DIR / f"idle_{now.strftime('%Y-%m-%d')}.jsonl"
    with open(daily_log, "a") as f:
        f.write(json.dumps({
            "ts": now.isoformat(),
            "idle_sec": idle_seconds,
            "is_idle": is_idle,
        }) + "\n")

    IDLE_STATE.write_text(json.dumps(state, indent=2))


def main():
    print("[idle] Idle Detector starting (interval=60s)", file=sys.stderr)
    was_idle = False

    while True:
        try:
            idle_sec = get_idle_seconds()
            if idle_sec is not None:
                write_idle_state(idle_sec)

                is_idle = idle_sec >= 300
                if was_idle and not is_idle:
                    print(f"[idle] 🟢 User returned (was idle {idle_sec // 60}min)", file=sys.stderr)
                elif not was_idle and is_idle:
                    print(f"[idle] 🔴 User went idle ({idle_sec // 60}min)", file=sys.stderr)
                was_idle = is_idle

            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[idle] error: {exc}", file=sys.stderr)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
