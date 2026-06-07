"""Helios v5 — Gaming detection module.

Detects active gaming sessions via Windows PowerShell (WSL can't see Windows
processes via pgrep). Also reads focus_state.json from active_window_tracker
for categorized gaming detection.
"""
from .base import BaseMod
from typing import Any
import subprocess, json, os

POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

class GamingModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "gaming",
        "version": "1.1.0",
        "description": "Detects active gaming sessions via Windows PowerShell + focus tracker",
        "author": "system",
        "collectors": ['focus_state.json'],
        "dependencies": [],
        "priority": 5,
    }

    # Process names to detect (lowercased for matching)
    GAME_PROCESSES = [
        "steam", "steamwebhelper", "cs2", "csgo",
        "league", "leagueclient", "league of legends",
        "minecraft", "javaw",
        "valorant",
        "fortnite", "fortniteclient",
        "overwatch",
        "destiny2",
        "call of duty", "cod",
        "apex", "r5apex",
        "baldur", "bg3",
        "elden", "eldenring",
        "valheim",
        "factorio",
        "witcher",
        "cyberpunk",
        "gta",
        "pubg",
        "rainbow", "r6",
        "warzone",
        "diablo",
        "pathofexile", "poe",
        "terraria",
        "stardew",
        "skyrim",
        "fallout",
        "dota",
        "rocket league",
        "deadlock",
        "marvel rivals", "marvelrivals",
        "helldivers",
    ]

    def tick(self) -> dict[str, Any]:
        active = []
        
        # Strategy 1: Check focus_state.json (from active_window_tracker)
        focus_path = os.path.expanduser("~/.hermes/helios/data/focus_state.json")
        if os.path.exists(focus_path):
            try:
                with open(focus_path) as f:
                    focus = json.load(f)
                if focus.get("category") == "gaming" and focus.get("active"):
                    active.append(focus.get("app", "game"))
            except Exception:
                pass

        # Strategy 2: PowerShell Get-Process (more reliable for background games)
        if os.path.exists(POWERSHELL):
            try:
                result = subprocess.run(
                    [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                     "Get-Process | Select-Object -ExpandProperty ProcessName"],
                    capture_output=True, text=True, timeout=5,
                    cwd="/tmp"
                )
                if result.returncode == 0:
                    processes_lower = result.stdout.lower()
                    for game in self.GAME_PROCESSES:
                        if game in processes_lower and game not in active:
                            active.append(game)
            except Exception:
                pass

        return {
            "active_games": active,
            "count": len(active),
            "is_gaming": len(active) > 0,
        }

