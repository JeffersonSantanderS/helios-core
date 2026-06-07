#!/usr/bin/env python3
"""Export Helios v6 live data as JSON for downstream sync.
Run via SSH pipe from Ops — outputs compact JSON to stdout.
Uses daily_intelligence.export_for_downstream() for live v6 data.
"""
import json
import sys
from pathlib import Path

# Add helios package to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

DB = Path.home() / ".hermes" / "helios" / "helios_v6.db"  # active database

if not DB.exists():
    print(json.dumps({"error": "DB not found", "entries": 0}))
    exit(0)

from daily_intelligence import export_for_downstream
result = export_for_downstream(str(DB))
print(json.dumps(result, default=str))