"""Helios v6 — Spotify Poller (Imperial Edition).

Polls Spotify /me/player/currently-playing every 15 seconds.
Logs EVERY poll to spotify_history.jsonl — not just track changes.
Records progress_ms so actual listen time can be reconstructed.

This is surveillance. We know what you played, when you skipped,
when you paused, and exactly how long you listened.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
SPOTIFY_STATE = DATA_DIR / "spotify_state.json"
SPOTIFY_HISTORY = DATA_DIR / "spotify_history.jsonl"
CREDS_FILE = Path.home() / ".hermes" / "helios" / "config" / "spotify_creds.json"
POLL_INTERVAL = 15  # seconds
TOKEN_REFRESH_INTERVAL = 3300  # ~55 min (token lives 1hr)


def load_creds() -> dict:
    if not CREDS_FILE.exists():
        print("[spotify] No creds file at", CREDS_FILE, file=sys.stderr)
        return {}
    try:
        return json.loads(CREDS_FILE.read_text())
    except Exception as exc:
        print(f"[spotify] Failed to load creds: {exc}", file=sys.stderr)
        return {}


def refresh_token(creds: dict) -> str | None:
    refresh = creds.get("refresh_token", "")
    client_id = creds.get("client_id", "")
    client_secret = creds.get("client_secret", "")
    if not refresh or not client_id or not client_secret:
        print("[spotify] Missing credentials", file=sys.stderr)
        return None
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://accounts.spotify.com/api/token",
             "-d", f"grant_type=refresh_token&refresh_token={refresh}",
             "-H", f"Authorization: Basic {_b64(f'{client_id}:{client_secret}')}",
             "-H", "Content-Type: application/x-www-form-urlencoded",
             "--connect-timeout", "5", "--max-time", "15"],
            capture_output=True, text=True, timeout=20
        )
        data = json.loads(result.stdout)
        token = data.get("access_token", "")
        if token:
            if data.get("refresh_token"):
                creds["refresh_token"] = data["refresh_token"]
                CREDS_FILE.write_text(json.dumps(creds, indent=2))
            return token
        else:
            print(f"[spotify] Token refresh failed: {data.get('error', 'unknown')}", file=sys.stderr)
            return None
    except Exception as exc:
        print(f"[spotify] Token refresh error: {exc}", file=sys.stderr)
        return None


def _b64(s: str) -> str:
    import base64
    return base64.b64encode(s.encode()).decode()


def poll_spotify(token: str) -> dict | None:
    try:
        result = subprocess.run(
            ["curl", "-s", "https://api.spotify.com/v1/me/player/currently-playing",
             "-H", f"Authorization: Bearer {token}",
             "--connect-timeout", "5", "--max-time", "10"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return None
        if result.stdout.strip() == "":
            return {"is_playing": False}
        data = json.loads(result.stdout)
        return data
    except json.JSONDecodeError:
        return {"is_playing": False}
    except Exception as exc:
        print(f"[spotify] Poll error: {exc}", file=sys.stderr)
        return None


def write_state(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if data is None:
        data = {"is_playing": False}
    data["_last_poll"] = datetime.now(timezone.utc).isoformat()
    SPOTIFY_STATE.write_text(json.dumps(data, indent=2))


def log_poll(data: dict) -> None:
    """Log EVERY poll to history — track ID + progress_ms for listen-time reconstruction."""
    ts = datetime.now(timezone.utc).isoformat()
    is_playing = data.get("is_playing", False)

    entry = {
        "ts": ts,
        "is_playing": is_playing,
    }

    if is_playing:
        item = data.get("item", {})
        entry.update({
            "track_id": item.get("id"),
            "track": item.get("name"),
            "artists": [a.get("name") for a in item.get("artists", [])],
            "album": item.get("album", {}).get("name"),
            "duration_ms": item.get("duration_ms"),
            "progress_ms": data.get("progress_ms"),
        })
    else:
        entry["track_id"] = None
        entry["track"] = None
        entry["artists"] = []
        entry["progress_ms"] = None
        entry["duration_ms"] = None

    with open(SPOTIFY_HISTORY, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    print("[spotify] Spotify Poller (Imperial) starting", file=sys.stderr)

    creds = load_creds()
    if not creds:
        print("[spotify] No Spotify credentials — sleeping", file=sys.stderr)
        write_state({"is_playing": False, "configured": False})
        while True:
            time.sleep(3600)

    access_token = refresh_token(creds)
    if not access_token:
        print("[spotify] Could not get access token — retrying in 60s", file=sys.stderr)
        write_state({"is_playing": False, "configured": True, "_error": "no_token"})
        time.sleep(60)
        access_token = refresh_token(creds)
        if not access_token:
            print("[spotify] Still no token — sleeping", file=sys.stderr)
            while True:
                time.sleep(3600)

    last_refresh = time.time()
    last_track_id = None

    while True:
        try:
            if time.time() - last_refresh > TOKEN_REFRESH_INTERVAL:
                new_token = refresh_token(creds)
                if new_token:
                    access_token = new_token
                    last_refresh = time.time()
                    print("[spotify] Token refreshed", file=sys.stderr)
                else:
                    print("[spotify] Token refresh failed — will retry", file=sys.stderr)

            data = poll_spotify(access_token)
            if data is None:
                print("[spotify] Poll failed — retrying in 30s", file=sys.stderr)
                time.sleep(30)
                continue

            write_state(data)
            log_poll(data)  # Log EVERY poll — imperial surveillance

            if data.get("is_playing"):
                item = data.get("item", {})
                track_id = item.get("id")
                if track_id != last_track_id:
                    track = item.get("name", "?")
                    artists = ", ".join(a["name"] for a in item.get("artists", []))
                    print(f"[spotify] 🎵 {artists} — {track}", file=sys.stderr)
                    last_track_id = track_id
            else:
                if last_track_id:
                    print("[spotify] ⏹️ paused/stopped", file=sys.stderr)
                    last_track_id = None

            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[spotify] error: {exc}", file=sys.stderr)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
