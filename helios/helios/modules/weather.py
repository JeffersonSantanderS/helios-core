"""Helios v6 — Weather module (wttr.in)."""
from .base import BaseMod
import json, urllib.request
from typing import Any

class WeatherModule(BaseMod):
    MODULE_MANIFEST = {
        **BaseMod.MODULE_MANIFEST,
        "name": "weather",
        "version": "1.0.0",
        "description": "Tracks local weather via wttr.in API",
        "author": "system",
        "collectors": ['weather_cache.json'],
        "dependencies": [],
        "priority": 5,
    }

    def tick(self) -> dict[str, Any]:
        loc = self.config.get("location", "")
        try:
            url = f"https://wttr.in/{loc.replace(' ', '+')}?format=j1"
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.load(resp)
            current = data["current_condition"][0]
            forecast = data["weather"][0]
            # Freshness from observation time (wttr.in format: 2026-05-11 09:20 PM)
            from datetime import datetime, timezone
            parsed = current.get("localObsDateTime", "")
            freshness_secs = None
            try:
                dt = datetime.strptime(parsed, "%Y-%m-%d %H:%M %p").replace(tzinfo=timezone.utc)
                freshness_secs = int((datetime.now(timezone.utc) - dt).total_seconds())
            except Exception:
                pass
            result = {
                "temp_c": int(current.get("temp_C", 0)),
                "feels_like_c": int(current.get("FeelsLikeC", 0)),
                "humidity": int(current.get("humidity", 0)),
                "condition": current.get("weatherDesc", [{}])[0].get("value", "unknown"),
                "wind_kph": int(current.get("windspeedKmph", 0)),
                "forecast_high": int(forecast.get("maxtempC", 0)),
                "forecast_low": int(forecast.get("mintempC", 0)),
            }
            if freshness_secs is not None:
                result["freshness_secs"] = freshness_secs
                result["last_updated"] = parsed
            return result
        except Exception as exc:
            return {"_error": str(exc)}

