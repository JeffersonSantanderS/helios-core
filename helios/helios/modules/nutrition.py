"""Helios v6 — Nutrition tracker (markdown log).

Reads and writes daily nutrition data to a structured markdown file
at ~/.hermes/helios/data/nutrition_log.md. Replaces the old
SparkyFitness dependency with self-contained local logging.

Data flow:
  - Manual entry via Hermes /helios nutrition add ...
  - Scheduled cron/Siri Shortcuts POST to health_data
  - Engine tick reads the .md file and populates metric_snapshots
  - Rules fire on nutrition context (calories, protein, macros)
  - Daily intelligence includes nutrition in briefings

Markdown format (one section per day, appended):

    ## 2026-05-21

    | time   | item              | cal | protein | carbs | fat |
    |--------|-------------------|-----|---------|-------|-----|
    | 08:30  | eggs & toast      | 320 | 18      | 24    | 14  |
    | 12:15  | chicken sandwich  | 450 | 35      | 38    | 12  |

    - weight: 168.5
    - workout: lifting, 45min
    - notes: feeling good today
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .base import BaseMod

log = logging.getLogger("helios.modules.nutrition")

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
LOG_FILE = DATA_DIR / "nutrition_log.md"


class NutritionModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "nutrition",
        "version": "1.0.0",
        "description": "Tracks daily calories, macros, weight, and workouts via markdown log",
        "author": "system",
        "collectors": [],
        "dependencies": [],
        "priority": 8,
    }

    # Daily calorie target (configurable)
    DEFAULT_CALORIE_TARGET = 2000
    DEFAULT_PROTEIN_TARGET = 160

    def __init__(self, db_path: Optional[str] = None, config: Optional[Any] = None):
        super().__init__(db_path=db_path, config=config)
        self.calorie_target = self.config.get("calorie_target", self.DEFAULT_CALORIE_TARGET)
        self.protein_target = self.config.get("protein_target", self.DEFAULT_PROTEIN_TARGET)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def tick(self) -> dict[str, Any]:
        """Read today's nutrition log and return context for rules/briefings."""
        today = datetime.now().strftime("%Y-%m-%d")
        nutrition = self._parse_day(today)

        # Also write key metrics to metric_snapshots for the engine
        if self.db_path and nutrition.get("total_cal", 0) > 0:
            self._write_metric_snapshots(today, nutrition)

        # Merge protein data with old protein module context for rule compatibility
        result = {
            "calories_today": nutrition.get("total_cal", 0),
            "calorie_target": self.calorie_target,
            "calorie_pct": round(
                nutrition.get("total_cal", 0) / self.calorie_target * 100, 1
            ) if self.calorie_target else 0,
            "calories_remaining": max(0, self.calorie_target - nutrition.get("total_cal", 0)),
            "protein_today": nutrition.get("total_protein", 0),
            "protein_target": self.protein_target,
            "protein_pct": round(
                nutrition.get("total_protein", 0) / self.protein_target * 100, 1
            ) if self.protein_target else 0,
            "protein_remaining": max(0, self.protein_target - nutrition.get("total_protein", 0)),
            "carbs_today": nutrition.get("total_carbs", 0),
            "fat_today": nutrition.get("total_fat", 0),
            "weight": nutrition.get("weight"),
            "workout": nutrition.get("workout"),
            "workout_duration_min": nutrition.get("workout_duration_min"),
            "entries_today": nutrition.get("entry_count", 0),
            # Backward compat with old protein module context keys
            "today": round(nutrition.get("total_protein", 0), 1),
            "target": self.protein_target,
            "remaining": max(0, self.protein_target - nutrition.get("total_protein", 0)),
            "pct": round(
                nutrition.get("total_protein", 0) / self.protein_target * 100, 1
            ) if self.protein_target else 0,
        }

        return result

    # ── Parsing ──────────────────────────────────────────────────────────

    def _parse_day(self, date_str: str) -> dict[str, Any]:
        """Parse a single day's section from the markdown log."""
        if not LOG_FILE.exists():
            return {"total_cal": 0, "total_protein": 0, "total_carbs": 0,
                    "total_fat": 0, "entry_count": 0}

        content = LOG_FILE.read_text()
        # Find the day section
        day_pattern = re.compile(
            rf"^## {re.escape(date_str)}\s*$",
            re.MULTILINE,
        )
        match = day_pattern.search(content)
        if not match:
            return {"total_cal": 0, "total_protein": 0, "total_carbs": 0,
                    "total_fat": 0, "entry_count": 0}

        # Extract text until the next ## heading or end of file
        start = match.end()
        next_heading = re.compile(r"^## \d{4}-\d{2}-\d{2}", re.MULTILINE)
        next_match = next_heading.search(content, start)
        section = content[start:next_match.start() if next_match else len(content)]

        # Parse table rows: | time | item | cal | protein | carbs | fat |
        total_cal = 0
        total_protein = 0
        total_carbs = 0
        total_fat = 0
        entry_count = 0

        for line in section.split("\n"):
            line = line.strip()
            if not line.startswith("|") or line.startswith("|--") or line.startswith("| -"):
                # Skip separator rows and header rows
                if "time" in line.lower() and "item" in line.lower():
                    continue
                if re.match(r"^\|[-\s|]+\|$", line):
                    continue
            parts = [p.strip() for p in line.split("|")]
            # parts: ['', time, item, cal, protein, carbs, fat, '']
            # Filter out empty strings from leading/trailing pipes
            parts = [p for p in parts if p]
            if len(parts) >= 4:
                try:
                    cal = float(parts[2]) if parts[2] else 0
                    protein = float(parts[3]) if len(parts) > 3 and parts[3] else 0
                    carbs = float(parts[4]) if len(parts) > 4 and parts[4] else 0
                    fat = float(parts[5]) if len(parts) > 5 and parts[5] else 0
                    total_cal += cal
                    total_protein += protein
                    total_carbs += carbs
                    total_fat += fat
                    entry_count += 1
                except (ValueError, IndexError):
                    continue

        # Parse metadata lines: - weight: 168.5, - workout: lifting, 45min
        weight = None
        workout = None
        workout_duration_min = None
        notes = None

        for line in section.split("\n"):
            line = line.strip()
            if line.startswith("- "):
                kv_match = re.match(r"^-\s+(weight|workout|notes):\s*(.+)$", line)
                if kv_match:
                    key, value = kv_match.group(1), kv_match.group(2).strip()
                    if key == "weight":
                        try:
                            weight = float(value)
                        except ValueError:
                            weight = None
                    elif key == "workout":
                        # Extract duration if present: "lifting, 45min" or "lifting 45 min"
                        workout = value
                        dur_match = re.search(r"(\d+)\s*min", value)
                        if dur_match:
                            workout_duration_min = int(dur_match.group(1))
                    elif key == "notes":
                        notes = value

        return {
            "total_cal": round(total_cal, 1),
            "total_protein": round(total_protein, 1),
            "total_carbs": round(total_carbs, 1),
            "total_fat": round(total_fat, 1),
            "entry_count": entry_count,
            "weight": weight,
            "workout": workout,
            "workout_duration_min": workout_duration_min,
            "notes": notes,
        }

    # ── Writing ──────────────────────────────────────────────────────────

    @staticmethod
    def add_entry(
        item: str,
        cal: float,
        protein: float = 0,
        carbs: float = 0,
        fat: float = 0,
        weight: Optional[float] = None,
        workout: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> str:
        """Add a nutrition entry to today's section in the markdown log.

        Creates the day section if it doesn't exist, appends a table row
        and optional metadata lines. Returns a summary string.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%H:%M")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not LOG_FILE.exists():
            LOG_FILE.write_text("# Nutrition Log\n\n")

        content = LOG_FILE.read_text()
        day_header = f"## {today}"

        entry_line = (
            f"| {now} | {item} | {cal:.0f} | {protein:.0f} | {carbs:.0f} | {fat:.0f} |"
        )

        if day_header in content:
            # Find the end of this day's section
            lines = content.split("\n")
            insert_idx = None
            for i, line in enumerate(lines):
                if line.strip() == day_header:
                    insert_idx = i
                    break

            if insert_idx is not None:
                # Find the last table row of this day's section
                last_row_idx = insert_idx
                metadata_lines = []
                for j in range(insert_idx + 1, len(lines)):
                    line = lines[j].strip()
                    if line.startswith("## ") and line != day_header:
                        break
                    if line.startswith("|") and not line.startswith("|--") and not line.startswith("| -"):
                        if "time" not in line.lower():
                            last_row_idx = j
                    if line.startswith("- "):
                        metadata_lines.append(j)

                # Insert the new table row after the last existing row
                new_lines = lines[:last_row_idx + 1] + [entry_line] + lines[last_row_idx + 1:]
                content = "\n".join(new_lines)
        else:
            # Create new day section with table header
            section = f"\n{day_header}\n\n"
            section += "| time | item | cal | protein | carbs | fat |\n"
            section += "|------|------|-----|---------|-------|-----|\n"
            section += entry_line + "\n"
            content = content.rstrip() + "\n" + section

        # Add metadata (weight/workout/notes) as list items
        metadata_additions = []
        if weight is not None:
            # Remove existing weight line for today and add new one
            content = re.sub(r"\n- weight: .+\n", "\n", content)
            metadata_additions.append(f"- weight: {weight}")
        if workout is not None:
            content = re.sub(r"\n- workout: .+\n", "\n", content)
            metadata_additions.append(f"- workout: {workout}")
        if notes is not None:
            content = re.sub(r"\n- notes: .+\n", "\n", content)
            metadata_additions.append(f"- notes: {notes}")

        if metadata_additions:
            # Find where to insert metadata (after table, before next ## heading)
            day_start = content.find(day_header)
            if day_start != -1:
                # Find end of day section
                next_section = re.search(r"\n## \d{4}-\d{2}-\d{2}", content[day_start + len(day_header):])
                if next_section:
                    insert_pos = day_start + len(day_header) + next_section.start()
                    metadata_block = "\n" + "\n".join(metadata_additions) + "\n"
                    content = content[:insert_pos] + metadata_block + content[insert_pos:]
                else:
                    content = content.rstrip() + "\n" + "\n".join(metadata_additions) + "\n"

        LOG_FILE.write_text(content)

        # Build summary
        mod = NutritionModule()
        summary = mod._parse_day(today)
        remaining = max(0, 2000 - summary["total_cal"])
        result = (
            f"Logged: {item} ({cal:.0f} cal, {protein:.0f}g protein, "
            f"{carbs:.0f}g carbs, {fat:.0f}g fat). "
            f"Today: {summary['total_cal']:.0f}/{2000} cal, "
            f"{summary['total_protein']:.0f}g/{160}g protein. "
            f"{remaining:.0f} cal remaining."
        )
        if weight:
            result += f" Weight: {weight} lbs."
        if workout:
            result += f" Workout: {workout}."
        return result

    # ── Metric snapshots ────────────────────────────────────────────────

    def _write_metric_snapshots(self, date_str: str, nutrition: dict[str, Any]) -> None:
        """Write key nutrition metrics to metric_snapshots for rules/daily intel."""
        if not self.db_path:
            return

        try:
            conn = sqlite3.connect(self.db_path)
            metrics = {
                "nutrition.calories_daily": nutrition.get("total_cal", 0),
                "nutrition.protein_daily": nutrition.get("total_protein", 0),
                "nutrition.carbs_daily": nutrition.get("total_carbs", 0),
                "nutrition.fat_daily": nutrition.get("total_fat", 0),
                "nutrition.weight": nutrition.get("weight", 0),
            }
            if nutrition.get("workout_duration_min"):
                metrics["nutrition.workout_minutes"] = nutrition["workout_duration_min"]

            for metric, value in metrics.items():
                if value is not None and value != 0:
                    conn.execute(
                        """INSERT OR REPLACE INTO metric_snapshots
                           (metric, value, date_key, source)
                           VALUES (?, ?, ?, 'nutrition_log')""",
                        (metric, value, date_str),
                    )
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("Failed to write nutrition metric snapshots: %s", exc)

    # ── CLI interface ────────────────────────────────────────────────────

    @staticmethod
    def cli_summary() -> str:
        """Print today's nutrition summary for the /helios nutrition command."""
        mod = NutritionModule()
        today = datetime.now().strftime("%Y-%m-%d")
        data = mod._parse_day(today)

        lines = [
            f"📊 Today's Nutrition ({today})",
            f"   Calories: {data['total_cal']:.0f}/{mod.calorie_target}",
            f"   Protein:  {data['total_protein']:.0f}g/{mod.protein_target}g",
            f"   Carbs:    {data['total_carbs']:.0f}g",
            f"   Fat:      {data['total_fat']:.0f}g",
        ]
        if data.get("weight"):
            lines.append(f"   Weight:   {data['weight']} lbs")
        if data.get("workout"):
            lines.append(f"   Workout:  {data['workout']}")
        if data.get("entry_count", 0) == 0:
            lines.append("   (no entries yet)")

        return "\n".join(lines)