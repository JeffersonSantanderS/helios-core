"""Helios v5 — iCloud Find My Location Sync.

Polls iCloud Find My for device location using pyicloud.
Writes icloud_location_sync.json for Helios location module to read.
Supports 2FA via stored session cookies.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
LOCATION_FILE = DATA_DIR / "icloud_location_sync.json"
CREDS_FILE = DATA_DIR / "icloud_creds.json"
POLL_INTERVAL = 900  # 15 minutes (rate limited to 3/hr to avoid lockout)
MAX_CALLS_PER_HOUR = 3

# Default: configured city
DEFAULT_CITY = os.environ.get("HELIOS_DEFAULT_CITY", "")
DEFAULT_PROVINCE = os.environ.get("HELIOS_DEFAULT_PROVINCE", "")


def load_creds() -> dict:
    """Load iCloud credentials from data dir."""
    if not CREDS_FILE.exists():
        return {}

    try:
        return json.loads(CREDS_FILE.read_text())
    except Exception:
        return {}


COOKIE_DIR = os.path.expanduser("~/.hermes/helios-v6/.icloud_session")


def poll_icloud(creds: dict) -> dict | None:
    """Poll iCloud Find My for device location.

    Uses saved session cookies from ~/.helios/.icloud_session/ (no password needed
    once session is established). Falls back to fresh auth with password if session
    is expired.
    """
    apple_id = creds.get("apple_id", "")
    password = creds.get("password", "")

    if not apple_id:
        return None

    # Build the python snippet that polls iCloud
    # Password passed via AID env var (NOT embedded in cmdline) to avoid ps aux leak
    def _build_snippet(aid: str, cookie_dir: str | None) -> str:
        cookie_arg = f"'{cookie_dir}'" if cookie_dir else "None"
        return f"""
import json, os, sys
try:
    from pyicloud import PyiCloudService
    from pyicloud.exceptions import PyiCloudAPIResponseException
    
    pw = os.environ.get('AID', '')
    cookie_dir = {cookie_arg}
    if cookie_dir:
        api = PyiCloudService('{aid}', pw, cookie_directory=cookie_dir)
    else:
        api = PyiCloudService('{aid}', pw)
    
    # Check if session is valid
    if api.requires_2fa or api.requires_2sa:
        print(json.dumps({{'error': 'session expired — re-run icloud_login.py', 'needs_2fa': True}}))
        sys.exit(0)
    
    # Try to find iPhone — .location is a PROPERTY in v2.5.0+, not a method
    devices = []
    try:
        for d in api.devices:
            loc = d.location  # property, not d.location()
            if loc:
                devices.append({{
                    'name': str(d),
                    'lat': loc.get('latitude'),
                    'lon': loc.get('longitude'),
                    'accuracy': loc.get('horizontalAccuracy'),
                    'ts': loc.get('timeStamp'),
                    'is_iphone': 'iPhone' in str(d),
                }})
    except Exception as e:
        print(json.dumps({{'error': str(e), 'devices_found': 0}}))
        sys.exit(0)
    
    # Prefer iPhone
    iphone = [d for d in devices if d['is_iphone']]
    target = iphone[0] if iphone else (devices[0] if devices else None)
    
    if target:
        target['devices_found'] = len(devices)
        target['source'] = 'pyicloud'
        print(json.dumps(target))
    else:
        print(json.dumps({{'error': 'no devices with location', 'devices_found': len(devices)}}))
except ImportError:
    print(json.dumps({{'error': 'pyicloud not installed'}}))
except Exception as e:
    print(json.dumps({{'error': str(e)}}))
"""

    # Strategy 1: Use saved session cookies
    if os.path.isdir(COOKIE_DIR):
        try:
            result = subprocess.run(
                ["python3", "-c", _build_snippet(apple_id, COOKIE_DIR)],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "PYTHONWARNINGS": "ignore", "AID": password}
            )
            data = json.loads(result.stdout)
            if "error" not in data:
                return data
            elif data.get("needs_2fa"):
                print(f"[location] Session expired — falling back to password auth", file=sys.stderr)
            else:
                print(f"[location] Saved session failed: {data['error']}", file=sys.stderr)
        except Exception as exc:
            print(f"[location] Saved session error: {exc}", file=sys.stderr)

    # Strategy 2: Fresh auth with password (will need 2FA if session cookies expired)
    if password and password != "***":
        try:
            result = subprocess.run(
                ["python3", "-c", _build_snippet(apple_id, None)],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "PYTHONWARNINGS": "ignore", "AID": password}
            )
            data = json.loads(result.stdout)
            if "error" not in data:
                return data
            else:
                print(f"[location] Password auth failed: {data['error']}", file=sys.stderr)
        except Exception as exc:
            print(f"[location] Password auth error: {exc}", file=sys.stderr)

    return None


def geocode_to_city_province(lat: float, lon: float) -> dict:
    """Basic reverse geocode via OpenStreetMap Nominatim (free, no API key)."""
    try:
        result = subprocess.run(
            ["curl", "-s",
             f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&zoom=10",
             "-H", f"User-Agent: Helios-v5/1.0 ({os.environ.get('HELIOS_CONTACT_EMAIL', 'helios@localhost')})",
             "--connect-timeout", "5", "--max-time", "10"],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(result.stdout)
        address = data.get("address", {})
        return {
            "city": address.get("city", address.get("town", address.get("municipality", "Unknown"))),
            "province": address.get("state", address.get("region", "Unknown")),
        }
    except Exception:
        return {"city": "Unknown", "province": "Unknown"}


def write_location(data: dict) -> None:
    """Write icloud_location_sync.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    output = {
        "source": data.get("source", "fallback"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "city": DEFAULT_CITY,
        "province": DEFAULT_PROVINCE,
    }

    if data.get("lat") and data.get("lon"):
        output["lat"] = data["lat"]
        output["lon"] = data["lon"]
        output["accuracy"] = data.get("accuracy")
        output["device"] = data.get("name", "unknown")

        # Geocode lat/lon → city/province
        geo = geocode_to_city_province(data["lat"], data["lon"])
        output["city"] = geo["city"]
        output["province"] = geo["province"]
    elif data.get("error"):
        output["error"] = data["error"]

    LOCATION_FILE.write_text(json.dumps(output, indent=2))


def append_location_history(data: dict) -> None:
    """Log location to location_history.jsonl for timeline analysis."""
    if not data.get("lat") or not data.get("lon"):
        return
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "lat": data["lat"],
        "lon": data["lon"],
        "accuracy": data.get("accuracy"),
        "city": data.get("city", "Unknown"),
        "province": data.get("province", "Unknown"),
        "source": data.get("source", "unknown"),
    }
    history_file = DATA_DIR / "location_history.jsonl"
    with open(history_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    print("[location] iCloud Location Sync starting", file=sys.stderr)

    creds = load_creds()
    if not creds:
        print("[location] No iCloud credentials — writing default location and sleeping", file=sys.stderr)
        write_location({"source": "default", "lat": 40.7128, "lon": -74.0060})
        # Wait for creds
        while True:
            time.sleep(3600)

    calls_this_hour = 0
    hour_start = time.time()

    while True:
        try:
            # Rate limiting
            now = time.time()
            if now - hour_start >= 3600:
                calls_this_hour = 0
                hour_start = now

            if calls_this_hour >= MAX_CALLS_PER_HOUR:
                wait = int(3600 - (now - hour_start))
                print(f"[location] Rate limited — waiting {wait}s", file=sys.stderr)
                time.sleep(min(wait, POLL_INTERVAL))
                continue

            print("[location] Polling iCloud Find My...", file=sys.stderr)
            calls_this_hour += 1

            data = poll_icloud(creds)
            if data and data.get("lat"):
                write_location(data)
                append_location_history(data)  # log to timeline
                print(f"[location] ✅ {data.get('city', data.get('lat'))}", file=sys.stderr)
            else:
                print(f"[location] ⚠️ No location data: {data}", file=sys.stderr)
                # Still write a fallback with default
                write_location({"source": "fallback", "error": "no_data"})

            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[location] error: {exc}", file=sys.stderr)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
