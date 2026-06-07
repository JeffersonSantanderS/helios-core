"""Helios v6 — Active Window Tracker.

Polls Windows foreground window via powershell.exe from WSL2.
Writes focus_state.json and tracked_apps.jsonl.

Key design change (2026-06-01): reads idle_state.json from the idle_detector
for definitive idle time. The idle detector already has proper WSL2 Session-0
validation + fallback chain (logind, net I/O).  This collector ONLY tracks
windows — it does NOT determine whether the user is actually active.
Only actual mouse / keyboard input (low idle_seconds) counts as screen time.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
FOCUS_STATE = DATA_DIR / "focus_state.json"
APP_LOG = DATA_DIR / "tracked_apps.jsonl"
IDLE_STATE = DATA_DIR / "idle_state.json"
POLL_INTERVAL = 30  # seconds

POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# Screen-time limits (safety valves against runaway sessions)
MAX_SESSION_SEC = 14_400  # 4 hours cap on any single session
MIN_ACTIVE_RESUME_SEC = 15  # idle < 15s = definitely active (mouse moved)
IDLE_THRESHOLD_SEC = 300   # 5 minutes = idle / session pauses

_last_activity_ts: float = time.time()

# Category rules
CATEGORY_RULES = {
    "development": ["code", "vscode", "vim", "nvim", "idea", "pycharm", "webstorm", "sublime", "atom", "eclipse", "terminal", "iterm", "alacritty", "konsole", "cmd", "powershell", "bash", "zsh", "dev", "github"],
    "communication": ["slack", "teams", "zoom", "skype", "telegram", "whatsapp", "signal", "outlook", "thunderbird", "mail", "discord", "messenge"],
    "browser": ["chrome", "firefox", "brave", "edge", "safari", "opera", "vivaldi", "chromium", "arc"],
    "media": ["spotify", "vlc", "mpv", "plex", "netflix", "youtube", "mplayer", "celluloid", "music"],
    "productivity": ["obsidian", "notion", "evernote", "word", "excel", "powerpoint", "libreoffice", "onenote", "todoist", "calendar"],
    "gaming": ["steam", "epic", "lutris", "minecraft", "overwatch", "valorant", "league", "mumble", "game"],
    "system": ["explorer", "nautilus", "dolphin", "settings", "control", "taskmgr", "htop", "systemd", "powershell", "settings"],
    "social": ["twitter", "reddit", "facebook", "instagram", "tiktok", "linkedin", "tumblr", "pinterest"],
    "finance": ["bank", "wealthsimple", "coinbase", "binance", "paypal", "stripe", "quickbooks", "mint"],
}


def categorize(title: str) -> str:
    """Classify a window title into a category."""
    t = title.lower()
    for cat, keywords in CATEGORY_RULES.items():
        for kw in keywords:
            if kw in t:
                return cat
    return "unknown"


def read_idle_from_json() -> tuple[int | None, bool]:
    """Read idle seconds from idle_state.json (written by idle_detector.py).

    Returns (idle_seconds, stale) -- stale=True if idle_state.json is
    older than 2 min (collector likely dead).
    """
    if not IDLE_STATE.exists():
        return None, True
    try:
        data = json.loads(IDLE_STATE.read_text())
        idle_sec = int(data.get("idle_seconds", 0))
        last_seen = data.get("last_seen", "")
        if last_seen:
            try:
                from datetime import datetime, timezone
                last_dt = datetime.fromisoformat(last_seen)
                age_sec = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if age_sec > 120:
                    return idle_sec, True
            except Exception:
                pass
        return idle_sec, False
    except Exception:
        return None, True


def get_foreground_window() -> dict | None:
    """Get the currently focused window via powershell.exe from WSL.

    Returns window metadata ONLY -- does NOT determine if the user is idle.
    Idle detection is delegated to idle_detector.py which writes idle_state.json
    with proper WSL2 Session-0 validation and fallback chain.
    """
    ps_script = """
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinAPI {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
}
"@
$hwnd = [WinAPI]::GetForegroundWindow()
$sb = New-Object System.Text.StringBuilder(256)
[WinAPI]::GetWindowText($hwnd, $sb, 256) | Out-Null
$title = $sb.ToString()
$procId = 0
[WinAPI]::GetWindowThreadProcessId($hwnd, [ref]$procId) | Out-Null
$proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
$procName = if ($proc) { $proc.ProcessName } else { "" }
Write-Output "$env:COMPUTERNAME|||$title|||$procName|||$hwnd"
"""
    try:
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=15,
            cwd="/tmp"
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        parts = result.stdout.strip().split("|||", 3)
        if len(parts) < 4:
            return None

        hostname, title, proc_name, hwnd = parts
        return {
            "hostname": hostname,
            "title": title,
            "process": proc_name,
            "hwnd": hwnd,
            "category": categorize(title) or categorize(proc_name),
        }
    except Exception as exc:
        print(f"[tracker] powershell failed: {exc}", file=sys.stderr)
        return None


def write_focus_state(window: dict) -> None:
    """Write current focus state for Helios focus module.

    Screen-time rule change (2026-06-01):
    - Actual "screen time" = time the user is active (moving mouse / typing)
    - We read idle_seconds from idle_state.json (idle_detector.py's output)
    - idle_seconds < 15s  -> definitely active, count screen time
    - idle_seconds > 300s -> idle, PAUSE session counting
    - idle_seconds between -> keep last known state
    - Session CAP: max 4 hours before forced close (prevents runaway)
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Read definitive idle time from idle_detector (has proper WSL2 validation)
    idle_sec, idle_stale = read_idle_from_json()

    # Determine actual activity:
    #   fresh idle < 15s  -> user is at computer right now (mouse moved recently)
    #   fresh idle > 300s -> user walked away, session pauses
    #   stale detector     -> can't tell, assume idle to avoid false screen time
    if idle_stale or idle_sec is None:
        is_active = False
        idle_sec = idle_sec or 0
    elif idle_sec < MIN_ACTIVE_RESUME_SEC:
        is_active = True
    elif idle_sec > IDLE_THRESHOLD_SEC:
        is_active = False
    else:
        # Between 15s and 300s -- preserve last known state (hysteresis)
        # If we were already active, stay active; if idle, stay idle
        is_active = False  # default to idle for safety

    now_ts = time.time()
    existing = {}
    if FOCUS_STATE.exists():
        try:
            existing = json.loads(FOCUS_STATE.read_text())
        except Exception:
            pass

    # Track total screen time (completed sessions only)
    total_minutes = existing.get("screen_time_today_minutes", 0)

    # Day rollover: reset cumulative screen time at midnight
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if existing.get("_screen_time_date") != today_str:
        existing["screen_time_today_minutes"] = 0
        existing["sessions_today"] = 0
        existing["_screen_time_date"] = today_str
        total_minutes = 0

    if is_active:
        _last_activity_ts = time.time()
        if not existing.get("active"):
            # Starting new session (resumed from idle)
            existing["active"] = True
            existing["start_ts"] = now_ts
            existing["app"] = window["title"]
            existing["category"] = window["category"]
            existing["sessions_today"] = existing.get("sessions_today", 0) + 1
            existing["session_seconds"] = 0
        else:
            # Continuing session -- update elapsed
            elapsed = int(now_ts - existing.get("start_ts", now_ts))
            # Safety cap: force-close sessions over 4 hours (prevents runaway)
            if elapsed > MAX_SESSION_SEC:
                print(
                    f"[tracker] Session auto-closed after {MAX_SESSION_SEC}s cap",
                    file=sys.stderr,
                )
                existing["active"] = False
                existing["last_session_minutes"] = elapsed // 60
                existing["last_session_app"] = existing.get("app", "")
                existing["last_session_category"] = existing.get("category", "")
                existing.pop("start_ts", None)
                existing["session_seconds"] = MAX_SESSION_SEC
            else:
                existing["session_seconds"] = elapsed
    else:
        if existing.get("active"):
            # User just went idle -- close the session, add duration to cumulative
            elapsed = int(now_ts - existing.get("start_ts", now_ts))
            existing["active"] = False
            existing["last_session_minutes"] = elapsed // 60
            existing["last_session_app"] = existing.get("app", "")
            existing["last_session_category"] = existing.get("category", "")
            existing.pop("start_ts", None)
            # Cumulative screen time: add completed session minutes
            existing["screen_time_today_minutes"] = existing.get("screen_time_today_minutes", 0) + (elapsed // 60)

        existing["session_seconds"] = existing.get("session_seconds", 0)

    existing["last_seen"] = datetime.now(timezone.utc).isoformat()
    existing["idle_seconds"] = idle_sec

    FOCUS_STATE.write_text(json.dumps(existing, indent=2))


def append_log(window: dict, idle_seconds: int) -> None:
    """Append to tracked apps log (for long-term analysis)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "title": window["title"],
        "process": window["process"],
        "category": window["category"],
        "idle_seconds": idle_seconds,
    }
    with open(APP_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    print("[tracker] Active Window Tracker starting (interval=30s)", file=sys.stderr)
    last_title = ""

    while True:
        try:
            window = get_foreground_window()
            if window:
                write_focus_state(window)

                # Only log when the title actually changes (dedupe)
                if window["title"] != last_title:
                    # Get current idle for the log entry
                    idle_sec, _ = read_idle_from_json()
                    append_log(window, idle_sec if idle_sec is not None else 0)
                    last_title = window["title"]
                    cat = window["category"]
                    short = window["title"][:60]
                    print(f"[tracker] {cat:>15} | {short}", file=sys.stderr)

            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[tracker] error: {exc}", file=sys.stderr)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
