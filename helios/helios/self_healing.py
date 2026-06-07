"""
Helios v6 — Self-Healing Engine (Phase 4).

Monitors collector health, data freshness, and daemon state.
Detects stale data, dead collectors, and escalating failures — then fixes them
or escalates when manual intervention is needed.

Healing tiers:
  1. AUTO: fix silently (restart subprocess, clear stale state)
  2. NOTIFY: fix + push info to Discord
  3. ESCALATE: can't fix — alert the user via configured channel

What it watches:
  - Collector subprocesses (spotify_poller, idle_detector, active_window_tracker)
  - Data file freshness (age of JSON cache files)
  - Module circuit breaker states (any modules stuck OPEN?)
  - Ingestion pipeline (are rows flowing?)
"""

from __future__ import annotations

import json, logging, os, subprocess, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.self_healing")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"

# ── Freshness thresholds ───────────────────────────────────────────────────
STALE_WARN_SECS = 600     # 10 min — warn if data older than this
STALE_CRITICAL_SECS = 1800  # 30 min — escalate
DEAD_SECS = 3600           # 1 hour — collector likely dead

# ── Expected data files and their maximum ages ──────────────────────────────
#
# ONLY files written on a FIXED TIMER regardless of data changes.
# Staleness here DOES mean the collector is dead.
#
# Excluded:
#   - tracked_apps.jsonl  → writes on title change (variable: 30s to hours)
#   - weather_state.json  → writes every tick but values update daily
#   - mood_state.json     → once daily at 7 AM
#   - calendar_state.json → on-demand when events change
#   - idle_state.json     → writes every tick but also verified via today's idle JSONL
#   - spotify_state.json  → covered by spotify_history.jsonl
#
EXPECTED_FILES = {
    "spotify_history.jsonl": STALE_WARN_SECS,        # polled every ~15s — constant writes
    "focus_state.json": 120,                          # written every 30s regardless
    "icloud_location_sync.json": STALE_CRITICAL_SECS, # every tick (5 min)
    "location_history.jsonl": STALE_CRITICAL_SECS,    # every tick (5 min)
}

# ── Collector expected subprocess names ─────────────────────────────────────
EXPECTED_COLLECTORS = [
    "spotify_poller.py",
    "idle_detector.py",
    "active_window_tracker.py",
]


def _find_collector_pids() -> dict[str, int]:
    """Find running collector processes by script name. Returns {name: pid}."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "collectors/"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
        
        collector_map = {}
        for pid in pids:
            try:
                cmdline = open(f"/proc/{pid}/cmdline").read().replace("\x00", " ")
                for name in EXPECTED_COLLECTORS:
                    if name in cmdline:
                        collector_map[name] = pid
                        break
            except (FileNotFoundError, PermissionError):
                pass
        
        return collector_map
    except Exception as exc:
        log.debug("Could not find collector PIDs: %s", exc)
        return {}


def _file_age(path: Path) -> float | None:
    """Return file age in seconds, or None if file doesn't exist."""
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def _restart_collector(script_name: str) -> bool:
    """Attempt to restart a collector subprocess via the daemon.
    Since collectors are daemon subprocesses, we signal the daemon
    to re-spawn via a marker file the engine watches.
    """
    marker = DATA_DIR / f".heal_{script_name}"
    try:
        marker.write_text(str(int(time.time())))
        log.info("Requested collector restart: %s", script_name)
        return True
    except Exception as exc:
        log.warning("Failed to write heal marker for %s: %s", script_name, exc)
        return False


class SelfHealing:
    """Monitors and auto-heals the Helios daemon."""

    def __init__(self, db=None, engine=None):
        self.db = db
        self.engine = engine  # reference to HeliosEngine for collector management
        self._last_check = 0.0
        self._check_interval = 60  # run every 60s
        self._escalated: set[str] = set()
        self._restart_attempts: dict[str, int] = {}
        self._last_ingestion_count = 0
        self._stale_tick_count = 0

    def should_check(self) -> bool:
        """Rate limit self-checks to avoid CPU churn."""
        return time.time() - self._last_check >= self._check_interval

    def tick_check(self, per_tick_rows: int = 0) -> list[dict]:
        """Run all self-healing checks. Returns actionable alerts for Discord.

        Args:
            per_tick_rows: rows ingested this tick (from run_ingestion totals).
                           Used to detect real ingestion stalls vs flat REPLACE counts.
        """
        if not self.should_check():
            return []

        self._last_check = time.time()
        actions: list[dict] = []

        try:
            # 1. Data freshness check
            freshness_actions = self._check_data_freshness()
            actions.extend(freshness_actions)

            # 2. Collector liveliness
            collector_actions = self._check_collectors()
            actions.extend(collector_actions)

            # 3. Ingestion pipeline health
            ingestion_actions = self._check_ingestion(per_tick_rows)
            actions.extend(ingestion_actions)

            # 4. Circuit breaker health
            breaker_actions = self._check_circuit_breakers()
            actions.extend(breaker_actions)

            # 5. Clean heal markers from last cycle
            self._clean_markers()

        except Exception as exc:
            log.warning("Self-healing check failed: %s", exc)

        return actions

    # ── 1. Data Freshness ──────────────────────────────────────────────────

    def _check_data_freshness(self) -> list[dict]:
        actions = []
        now = time.time()

        for filename, max_age in EXPECTED_FILES.items():
            fpath = DATA_DIR / filename
            age = _file_age(fpath)

            if age is None:
                # File missing entirely
                key = f"missing_{filename}"
                if key not in self._escalated:
                    actions.append({
                        "type": "file_missing",
                        "severity": "warning",
                        "auto_heal": False,
                        "title": f"Data file missing: {filename}",
                        "detail": f"{filename} doesn't exist. Module may be disabled or collector not started.",
                        "action": "notify",
                    })
                    self._escalated.add(key)
                continue

            if age > DEAD_SECS:
                key = f"dead_{filename}"
                if key not in self._escalated:
                    actions.append({
                        "type": "file_dead",
                        "severity": "critical",
                        "auto_heal": True,
                        "title": f"Data file dead: {filename} ({age/60:.0f} min old)",
                        "detail": f"{filename} hasn't been updated in {age/60:.0f} minutes. "
                                  f"Collector may need restart.",
                        "action": "escalate",
                        "heal_target": filename.replace("_state.json", "").replace(".json", ""),
                    })
                    self._escalated.add(key)
            elif age > max_age:
                key = f"stale_{filename}"
                if key not in self._escalated:
                    severity = "critical" if age > STALE_CRITICAL_SECS else "warning"
                    actions.append({
                        "type": "file_stale",
                        "severity": severity,
                        "auto_heal": False,
                        "title": f"Stale data: {filename} ({age/60:.0f} min old)",
                        "detail": f"{filename} last updated {age/60:.0f} min ago. Max allowed: {max_age/60:.0f} min.",
                        "action": "notify",
                    })
                    self._escalated.add(key)
            else:
                # Remove from escalated if healthy now
                for prefix in ["missing_", "dead_", "stale_"]:
                    self._escalated.discard(f"{prefix}{filename}")

        return actions

    # ── 2. Collector Liveliness ────────────────────────────────────────────

    def _check_collectors(self) -> list[dict]:
        actions = []
        running = _find_collector_pids()

        for name in EXPECTED_COLLECTORS:
            if name not in running:
                key = f"collector_down_{name}"
                attempts = self._restart_attempts.get(name, 0)

                if key not in self._escalated or attempts < 3:
                    # Try auto-restart
                    healed = _restart_collector(name)
                    self._restart_attempts[name] = attempts + 1

                    if attempts < 3:
                        actions.append({
                            "type": "collector_down",
                            "severity": "warning",
                            "auto_heal": True,
                            "healed": healed,
                            "title": f"Collector down: {name} (attempt {attempts+1}/3)",
                            "detail": f"Restart requested. Daemon will re-spawn on next tick.",
                            "action": "auto",
                        })
                    else:
                        actions.append({
                            "type": "collector_dead",
                            "severity": "critical",
                            "auto_heal": False,
                            "title": f"Collector dead: {name} ({attempts} restart attempts failed)",
                            "detail": f"{name} could not be restarted after {attempts} attempts. "
                                      f"Check daemon logs: journalctl --user -u helios-v6.service",
                            "action": "escalate",
                        })
                        self._escalated.add(key)
            else:
                self._restart_attempts[name] = 0
                self._escalated.discard(f"collector_down_{name}")

        return actions

    # ── 3. Ingestion Pipeline ──────────────────────────────────────────────

    def _check_ingestion(self, per_tick_rows: int = 0) -> list[dict]:
        """Detect real ingestion stalls by tracking per-tick row counts.

        The old approach compared total metric_snapshots rows, but INSERT OR REPLACE
        keeps that flat after initial writes. Now we track per-tick ingestion from
        run_ingestion() — 6 zero-row ticks = 30 min stall.
        """
        actions = []

        if per_tick_rows > 0:
            # Ingestion is flowing
            self._stale_tick_count = 0
            self._escalated.discard("ingestion_stalled")
            return actions

        # Zero rows this tick — possible stall
        self._stale_tick_count += 1

        if self._stale_tick_count >= 6:  # 6 ticks × 5 min = 30 min stall
            key = "ingestion_stalled"
            if key not in self._escalated:
                actions.append({
                    "type": "ingestion_stalled",
                    "severity": "critical",
                    "auto_heal": False,
                    "title": "Data ingestion stalled (30+ min zero rows per tick)",
                    "detail": "Ingestion pipeline has returned 0 rows for 6+ consecutive ticks. "
                              "Check if collector JSONL files are being written.",
                    "action": "escalate",
                })
                self._escalated.add(key)

        return actions

    # ── 4. Circuit Breaker Health ──────────────────────────────────────────

    def _check_circuit_breakers(self) -> list[dict]:
        actions = []

        if not self.engine or not hasattr(self.engine, 'cb'):
            return actions

        try:
            cb = self.engine.cb
            for mod in self.engine.modules:
                state = cb.state(mod.name)
                if state and state != "closed":
                    # Module circuit is tripped
                    key = f"breaker_{mod.name}"
                    if key not in self._escalated:
                        actions.append({
                            "type": "circuit_open",
                            "severity": "warning",
                            "auto_heal": True,
                            "title": f"Circuit open: {mod.name} ({state})",
                            "detail": f"Module {mod.name} has tripped its circuit breaker. "
                                      f"It will auto-retry based on breaker config.",
                            "action": "notify",
                        })
                        self._escalated.add(key)
                else:
                    self._escalated.discard(f"breaker_{mod.name}")
        except Exception as exc:
            log.debug("Circuit breaker check: %s", exc)

        return actions

    # ── 5. Cleanup ─────────────────────────────────────────────────────────

    def _clean_markers(self) -> None:
        """Remove processed heal markers."""
        for marker in DATA_DIR.glob(".heal_*"):
            try:
                age = time.time() - marker.stat().st_mtime
                if age > 300:  # 5 min — engine should have processed by now
                    marker.unlink()
            except Exception:
                pass

    # ── Formatting for Discord ─────────────────────────────────────────────

    @staticmethod
    def format_alerts(actions: list[dict]) -> str | None:
        """Format self-healing actions for Discord push."""
        escalate = [a for a in actions if a.get("action") == "escalate"]
        notify = [a for a in actions if a.get("action") == "notify"]
        auto = [a for a in actions if a.get("action") == "auto"]

        if not (escalate or notify or auto):
            return None

        lines = ["**🛡️ Helios Self-Healing**"]

        for a in escalate:
            lines.append(f"\n🚨 **CRITICAL: {a['title']}**\n_{a['detail']}_")
        
        for a in notify:
            lines.append(f"\n⚠️ {a['title']}\n_{a['detail']}_")

        if auto and not escalate:
            lines.append(f"\n✅ Auto-healed {len(auto)} issue(s)")

        return "\n".join(lines)
