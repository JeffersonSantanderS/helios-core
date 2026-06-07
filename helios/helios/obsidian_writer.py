"""Helios v5 — Obsidian daily note writer.

Writes structured daily markdown notes to the Obsidian vault
at the configured vault_path + Daily/ folder.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("helios.obsidian")


class ObsidianWriter:
    def __init__(self, vault_path: str = "", enabled: bool = True, templates: bool = True):
        self.vault_path = Path(vault_path) if vault_path else None
        self.enabled = enabled and self.vault_path is not None
        self.templates = templates

    @property
    def daily_dir(self) -> Optional[Path]:
        if not self.vault_path:
            return None
        return self.vault_path / "Daily"

    def write_daily(self, context: dict[str, Any], db_path: str = "") -> bool:
        """Write the daily note from current context."""
        if not self.enabled or not self.daily_dir:
            log.warning("Obsidian writer disabled or no vault path")
            return False

        self.daily_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_name = datetime.now(timezone.utc).strftime("%a")
        date_display = datetime.now(timezone.utc).strftime("%Y %B %d, %Y-%m-%d")

        # Pull data from context
        weather = context.get("weather", {})
        calendar = context.get("calendar", {})
        protein = context.get("protein", {})
        gaming = context.get("gaming", {})
        habits = context.get("habits", {})
        mood = context.get("mood", {})
        focus = context.get("focus", {})
        tasks = context.get("tasks", {})
        health = context.get("health", {})
        reminders = context.get("reminders", {})

        # Build the markdown
        lines = [
            f"# {date_display} ({day_name})",
            "",
            "## Morning",
            "- Woke up: ____",
        ]

        # Weather
        if weather and not weather.get("_error"):
            lines.append(f"- Weather: {weather.get('condition', '?')}, {weather.get('temp_c', '?')}°C")
        else:
            lines.append("- Weather: ____")

        lines.append("- Mood (1-10): ____")
        lines.append("")

        # Briefing section
        lines.append("## Briefing")
        event_count = calendar.get("events_today", 0) if not isinstance(calendar.get("events_today"), dict) else 0
        reminder_count = reminders.get("count", 0) if not isinstance(reminders, dict) or "count" not in reminders else reminders.get("count", 0)
        lines.append(f"- [ ] Calendar ({event_count} events)")
        lines.append(f"- [ ] Reminders ({reminder_count})")

        if health and not health.get("_error"):
            lines.append(f"- [ ] Health data ({health.get('records', 0)} records)")
        else:
            lines.append("- [ ] Health data (No data)")

        protein_today = protein.get("today", 0) if isinstance(protein, dict) else 0
        protein_target = protein.get("target", 160) if isinstance(protein, dict) else 160
        protein_pct = protein.get("pct", 0) if isinstance(protein, dict) else 0
        lines.append(f"- [ ] Protein ({protein_today}g / {protein_target}g ({protein_pct}%))")
        lines.append("")

        # Work
        lines.append("## Work")
        lines.append("<!-- HVAC, servers, etc -->")
        lines.append("- ")
        lines.append("")

        # Personal
        lines.append("## Personal")
        lines.append("<!-- Family, pets, errands -->")
        lines.append("- ")
        lines.append("")

        # Health
        lines.append("## Health")
        lines.append(f"- Protein: ____ / {protein_target}g")
        lines.append(f"- Water:   ____ / 3000 ml")
        lines.append(f"- Steps:   ____ / 10000")
        lines.append(f"- Sleep:   ____ hrs")
        lines.append("")

        # Spotify (placeholder — module currently returns stub)
        lines.append("## Spotify")
        lines.append("No session")
        lines.append("")

        # Gaming
        lines.append("## Gaming")
        active_games = gaming.get("active_games", []) if isinstance(gaming, dict) else []
        if active_games:
            lines.append(f"Active: {', '.join(active_games)}")
        else:
            lines.append("No game")
        lines.append("")

        # Habits
        lines.append("## Habits")
        habit_list = habits.get("habits", []) if isinstance(habits, dict) else []
        completed = habits.get("completed_today", 0) if isinstance(habits, dict) else 0
        total_habits = habits.get("total", 0) if isinstance(habits, dict) else 0
        if habit_list:
            for h in habit_list:
                if isinstance(h, dict):
                    check = "✅" if h.get("done", False) else "⬜"
                    lines.append(f"- {check} {h.get('name', h.get('slug', '?'))}")
        else:
            completed_check = f" ({completed}/{total_habits})" if total_habits else ""
            lines.append(f"- Habits{completed_check}")
        lines.append("")

        # Tasks
        lines.append("## Tasks")
        task_count = tasks.get("count", 0) if isinstance(tasks, dict) else 0
        task_completed = tasks.get("completed_today", 0) if isinstance(tasks, dict) else 0
        task_list = tasks.get("tasks", []) if isinstance(tasks, dict) else []
        if task_list:
            for t in task_list[:10]:
                if isinstance(t, dict):
                    check = "✅" if t.get("done", False) else "⬜"
                    lines.append(f"- {check} {t.get('title', t.get('name', '?'))}")
        else:
            lines.append(f"- {task_completed}/{task_count} completed")
        lines.append("")

        # Focus
        lines.append("## Focus")
        focus_active = focus.get("active", False) if isinstance(focus, dict) else False
        focus_mins = focus.get("session_minutes", 0) if isinstance(focus, dict) else 0
        focus_sessions = focus.get("sessions_today", 0) if isinstance(focus, dict) else 0
        if focus_active:
            lines.append(f"- Active session ({focus_mins} min, {focus_sessions} today)")
        else:
            lines.append(f"- {focus_sessions} sessions today ({focus_mins} min)")
        lines.append("")

        # Evening Review
        lines.append("## Evening Review")
        lines.append("- Best part: ")
        lines.append("- Worst part: ")
        lines.append("- Tomorrow: ")
        lines.append("")

        # Smart Insights (from Helios)
        lines.append("## Smart Insights")
        if protein_pct < 50 and protein_today > 0:
            lines.append(f"- 💡 Protein at {protein_pct}% (target {protein_target}g). Consider meal prepping.")
        elif protein_today == 0:
            lines.append(f"- 💡 Protein avg 0g (target {protein_target}g). Consider meal prepping.")
        else:
            lines.append(f"- 💡 Protein at {protein_pct}% of {protein_target}g target.")

        if gaming.get("count", 0) > 0:
            games_str = ", ".join(active_games) if active_games else "game"
            lines.append(f"- 🎮 Gaming: {games_str} active")

        # Weather advisory
        if weather.get("temp_c", 0) < 0:
            lines.append(f"- ❄️ Cold day ahead ({weather.get('temp_c')}°C)")
        elif weather.get("temp_c", 0) > 28:
            lines.append(f"- 🌡️ Hot day ahead ({weather.get('temp_c')}°C)")

        lines.append("")
        lines.append("---")
        lines.append("*Helios v6 — autonomous life management*")

        note_path = self.daily_dir / f"{today}.md"
        content = "\n".join(lines)

        try:
            note_path.write_text(content)
            log.info("Daily note written to %s", note_path)
            return True
        except Exception as exc:
            log.error("Failed to write daily note: %s", exc)
            return False

    def write_status(self, context: dict[str, Any]) -> bool:
        """Write latest_status.md summary."""
        if not self.enabled or not self.vault_path:
            return False

        weather = context.get("weather", {})
        protein = context.get("protein", {})
        gaming = context.get("gaming", {})

        lines = [
            "# Helios v5 Status",
            f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "## Current",
        ]

        if weather and not weather.get("_error"):
            lines.append(f"- 🌤️ {weather.get('condition')} — {weather.get('temp_c')}°C (feels like {weather.get('feels_like_c')}°C)")

        if isinstance(protein, dict):
            lines.append(f"- 🥩 Protein: {protein.get('today', 0)}g / {protein.get('target', 160)}g ({protein.get('pct', 0)}%)")

        if isinstance(gaming, dict) and gaming.get("active_games"):
            lines.append(f"- 🎮 Gaming: {', '.join(gaming['active_games'])}")

        lines.append("")

        path = self.vault_path / "latest_status.md"
        try:
            path.write_text("\n".join(lines))
            log.info("Status written to %s", path)
            return True
        except Exception as exc:
            log.error("Failed to write status: %s", exc)
            return False
