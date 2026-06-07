# Helios Brain v1 — Deterministic Brain State Export

> **Status:** Active
> **File:** `helios/helios/brain_state.py`
> **Tests:** `tests/test_brain_state.py` (46 tests)
> **Export:** `~/.hermes/helios/brain_state.json`

---

## What It Is

Brain v1 is a **deterministic, no-LLM** export that aggregates the outputs of all Helios brain modules into a single stable JSON contract. Other agents and services can consume this file to understand Helios's current state without guessing from scattered sources.

## Design Principles

1. **Deterministic first** — No LLM calls anywhere. Pure Python + SQLite reads.
2. **Aggregate, don't compute** — Reads from existing tables and module outputs. Never re-implements logic.
3. **Evidence-linked** — Every belief, rule status, and suggestion traces back to source data.
4. **Confidence-aware** — Stale or missing data is represented honestly (`_stale` suffix, `unknown` values, degraded confidence). Never silently accepted as valid.
5. **Atomic write** — Uses temp-file + rename pattern (mirrors `stable_exports._write_json_atomic`).
6. **Graceful degradation** — If any module raises or is `None`, that section fills safe defaults. The export never crashes.

## Export Location

```
~/.hermes/helios/brain_state.json
```

Co-located with `latest_status.json`, `context_export.json`, and `alerts_recent.json`.

## JSON Schema

```json
{
  "schema_version": "1.0",
  "generated_at": "2025-05-26T12:00:00+00:00",
  "runtime": {
    "daemon_state": "running",
    "data_freshness": {
      "ingestion": 120.5,
      "rules": 45.2,
      "insights": 21600.0,
      "preferences": 1200.0
    },
    "overall_confidence": 0.85,
    "module_health": {
      "ingestion": {
        "state": "healthy",
        "confidence": 0.92,
        "freshness_secs": 120.0,
        "freshness_label": "fresh",
        "last_ok_ts": "..."
      }
    }
  },
  "current_state": {
    "location": "home",
    "activity": "active",
    "focus": "productive",
    "energy": "well_rested",
    "calendar": "3_events",
    "health": "good",
    "mood": "7.0",
    "protein": "120g",
    "system": "unknown"
  },
  "beliefs": [
    {
      "key": "preference.sleep_avg",
      "value": 7.2,
      "confidence": 0.85,
      "sources": ["preference_engine"],
      "freshness_seconds": 3600,
      "evidence": ["28-day average"]
    }
  ],
  "active_rules": [
    {
      "rule_id": "late_night_gaming",
      "status": "cooldown",
      "priority": "normal",
      "reason": "in cooldown (2847s remaining)",
      "confidence": 0.72
    }
  ],
  "pattern_deviations": [
    {
      "pattern": "sleep_avg",
      "baseline": 7.2,
      "current": 4.5,
      "deviation": "z=-3.38",
      "confidence": 0.89,
      "sample_count": 28
    }
  ],
  "suggestions": [
    {
      "id": "alert_late_night_gaming_2025-05-26T...",
      "type": "alert_dispatch",
      "priority": "high",
      "message": "Late-night gaming detected",
      "reason": "Rule late_night_gaming triggered",
      "action_candidate": "late_night_gaming",
      "requires_confirmation": true
    }
  ],
  "suppressed_alerts": [
    {
      "rule_id": "*",
      "reason": "quiet_hours"
    },
    {
      "rule_id": "module_health",
      "reason": "low_confidence:degraded"
    }
  ],
  "evidence_trace": [
    {
      "source": "data_ingestion",
      "source_id": "metric:mood.score_daily",
      "timestamp": "2025-05-26T12:00:00+00:00",
      "used_for": "current_state,beliefs"
    }
  ]
}
```

## Section Reference

| Section | Source | Description |
|---|---|---|
| `runtime` | `ModuleHealthTracker` + daemon state | Module health, freshness, overall confidence |
| `current_state` | `metric_snapshots`, `focus`, `context` tables | Current snapshot of life dimensions; `"unknown"` when stale/missing |
| `beliefs` | `PreferenceEngine.all_patterns()` + `correlations` table | Inferred facts with confidence, sources, evidence |
| `active_rules` | `RulesEngine.evaluate()` + `rules` table | Rule eval results with status (triggered/cooldown/inactive) |
| `pattern_deviations` | `PreferenceEngine` patterns vs `metric_snapshots` | Where current values deviate from baseline (|z| > 1.5) |
| `suggestions` | `alert_history` + `context` (predictor) | Actionable suggestions with `requires_confirmation` flag |
| `suppressed_alerts` | Quiet hours + cooldown + degraded modules | Alerts intentionally held back, with reasons |
| `evidence_trace` | `context`, `metric_snapshots`, `rules`, `alert_history` | Chain of custody for every data point |

## Stale Data Handling

Dimensions in `current_state` get a `_stale` suffix when the corresponding module's freshness exceeds 1 hour (3600s). For example:

- `"focus": "productive_stale"` — focus data exists but is >1h old
- `"mood": "unknown"` — no mood data at all

This makes it impossible for consumers to silently accept stale data as current.

## Confidence Scoring

Overall confidence is the mean of all module confidences from `ModuleHealthTracker`. Individual beliefs and rules carry their own confidence scores (0.0–1.0), clamped and rounded to 3 decimals.

## ⚠️ Status: Library Only / Not Live

`brain_state.py` is **fully implemented and tested** but is **NOT yet wired into `HeliosEngine.tick()`**. To make it live:

1. Import `BrainStateBuilder` in `engine.py`
2. Instantiate in `HeliosEngine.__init__()` alongside other modules
3. Call `brain_state.export()` after `write_all_exports()` in the tick loop (after line ~593)
4. Wrap in try/except so brain_state failure never blocks the tick

This is a separate PR. The current branch delivers the library + tests + contract.

## Integration with Engine

`BrainStateBuilder` is instantiated in `HeliosEngine.__init__()` alongside other modules and called via `brain_state.export()` after `write_all_exports()` in the tick loop. It reads from the same `HeliosDB` instance and existing module objects — no new SQLite tables, no new dependencies.

## Testing

46 tests in `tests/test_brain_state.py` covering:

- Valid JSON export and schema version
- ISO 8601 timestamp generation
- Stale data representation (never silently accepted)
- Missing optional integrations don't crash (graceful degradation)
- Suggestions include `requires_confirmation` flag
- Suppressed alerts include reasons
- Confidence values bounded to [0.0, 1.0]
- Atomic write pattern (no `.tmp` files left)
- No LLM dependency
- Seeded data populates correct state dimensions
- Beliefs from preferences and correlations
- Evidence trace from DB sources