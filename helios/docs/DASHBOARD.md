# Helios Dashboard — Design & Implementation

## Status: Phase 6B — Hardened

The dashboard is implemented and serving locally at `http://127.0.0.1:8199/`.

## Running

### Manual start

```bash
cd ~/.hermes/helios-v6/helios
HELIOS_HOME=~/.hermes/helios python3 -m helios.dashboard.app
```

### Systemd user service (optional)

```bash
# Install the unit file
cp deploy/systemd/helios-dashboard.service ~/.config/systemd/user/

# Enable and start
systemctl --user enable helios-dashboard.service
systemctl --user start helios-dashboard.service

# Check status
systemctl --user status helios-dashboard.service

# View logs
journalctl --user -u helios-dashboard.service -f

# Stop
systemctl --user stop helios-dashboard.service

# Disable (stop + remove from autostart)
systemctl --user disable helios-dashboard.service
```

### Quick test (no server needed)

```bash
cd ~/.hermes/helios-v6/helios
python3 -c "
from helios.dashboard.app import app
from fastapi.testclient import TestClient
client = TestClient(app)
print(client.get('/api/status').json()['runtime_status'])
print(client.get('/health').json())
"
```

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard HTML page |
| `GET /api/status` | Full sanitized dashboard snapshot (JSON) |
| `GET /health` | Health check: status, uptime, mode, missing sources |
| `GET /static/*` | Static assets

## Goal

Make Helios visible. Open a local page and see what Helios knows and whether anything is stale/broken. Read-only at first.

## Design Principles

1. **Local-first, no auth required** — served on localhost, no public exposure
2. **Privacy-safe defaults** — no raw contacts, exact location, health data, or secrets visible without explicit toggle
3. **Deterministic data only** — dashboard reads Helios SQLite + JSON exports, no LLM calls
4. **Lightweight** — Pure HTML + CSS + vanilla JS, served by the existing Helios daemon via a `/dashboard` endpoint on its tick port (or a simple Python HTTP server if preferred)

## Dashboard Sections

### 1. System Overview (top of page)

| Field | Source | Privacy |
|-------|--------|---------|
| Last tick time | `latest_status.json` → `last_tick` | PUBLIC_SAFE |
| Tick count (24h) | `latest_status.json` → `tick_count_24h` | PUBLIC_SAFE |
| Daemon uptime | `systemctl --user status helios-v6.service` | PUBLIC_SAFE |
| Mode | `config.yaml` → `priority.mode` | PUBLIC_SAFE |
| Active channels | `ChannelRouter.channel_names` | PUBLIC_SAFE |

### 2. Module Health Grid

| Column | Source | Privacy |
|--------|--------|---------|
| Module name | `latest_status.json` → `modules` | PUBLIC_SAFE |
| Health state | `module_health` table | PUBLIC_SAFE |
| Freshness | `freshness_seconds` per module | PUBLIC_SAFE |
| Last update | Module's `last_updated` timestamp | PUBLIC_SAFE |

Color-coded:
- 🟢 HEALTHY (< 300s stale)
- 🟡 DEGRADED (300-600s stale)
- 🔴 FAILED (> 600s stale or circuit breaker open)

### 3. Collector Freshness

| Column | Source | Privacy |
|--------|--------|---------|
| Collector | `data/*.json` → mtime | PUBLIC_SAFE |
| Last poll | mtime of JSON state file | PUBLIC_SAFE |
| State | HEALTHY/STALE/EMPTY based on age thresholds | PUBLIC_SAFE |

### 4. Recent Alerts

| Column | Source | Privacy |
|--------|--------|---------|
| Time | `alert_history` table | PUBLIC_SAFE |
| Rule slug | `rule_slug` | PUBLIC_SAFE |
| Severity | `severity` | PUBLIC_SAFE |
| Category | `category` | PUBLIC_SAFE |
| Sent | `sent` (bool) | PUBLIC_SAFE |
| Message preview | `message` (first 80 chars, AGENT_SAFE) | AGENT_SAFE |

### 5. Priority Engine State

| Field | Source | Privacy |
|-------|--------|---------|
| Mode | `config.yaml` → `priority.mode` | PUBLIC_SAFE |
| Candidates (24h) | `priority_candidates` table | PUBLIC_SAFE |
| Decisions (24h) | `priority_decisions` table | PUBLIC_SAFE |
| Route breakdown | `priority_decisions` → route column | PUBLIC_SAFE |
| Summary queue | `priority_engine/summary_queue.jsonl` existence check | PUBLIC_SAFE |

### 6. Circuit Breaker State

| Module | State | Source | Privacy |
|--------|-------|--------|---------|
| Module name | Module ID | `circuit_breaker` in-memory | PUBLIC_SAFE |
| State | OPEN/CLOSED/HALF_OPEN | In-memory, exported to `latest_status.json` | PUBLIC_SAFE |
| Failure count | Count | In-memory | PUBLIC_SAFE |

### 7. Data Export Status

| Export | Last updated | Source | Privacy |
|--------|-------------|--------|---------|
| `latest_status.json` | mtime | Filesystem | PUBLIC_SAFE |
| `context_export.json` | mtime | Filesystem | PUBLIC_SAFE |
| `alerts_recent.json` | mtime | Filesystem | PUBLIC_SAFE |
| `channel_log.jsonl` | mtime | Filesystem | PUBLIC_SAFE |

### 8. High-Level Context Summary

| Field | Source | Privacy |
|-------|--------|---------|
| Current city | `location_state` → `city` (sanitized) | AGENT_SAFE |
| Current temperature | `weather_state` → `temp` | PUBLIC_SAFE |
| Steps today | `health_state` → `steps` (count only) | AGENT_SAFE |
| Sleep quality | `health_state` → `sleep_hours` (bucketed: low/normal/high) | AGENT_SAFE |
| Top artist today | `spotify_state` → artist | PUBLIC_SAFE |
| Active app | `focus_state` → app name | AGENT_SAFE |

**NOT shown by default** (behind toggle):
- Exact GPS coordinates (HIGHLY_SENSITIVE)
- Raw health samples (HIGHLY_SENSITIVE)
- Raw contacts (NEVER_EXPORT)
- Full email bodies (NEVER_EXPORT)
- Access tokens / API keys (NEVER_EXPORT)

## Privacy Classes

| Class | Description | Dashboard default |
|-------|-------------|-------------------|
| `PUBLIC_SAFE` | Non-personal system data, timestamps, counts | Always visible |
| `AGENT_SAFE` | Summarized personal context visible to authorized agents, not external | Shown with toggle |
| `PRIVATE` | Personal data that needs explicit consent | Hidden by default, toggle required |
| `HIGHLY_SENSITIVE` | Raw location, health samples, contacts | Never shown on dashboard |
| `NEVER_EXPORT` | Tokens, passwords, room IDs, full email bodies | Never stored or displayed |

## Technical Implementation

### Backend API

A lightweight `/api/dashboard` endpoint served by the Helios daemon (or a standalone script) that returns a single JSON blob:

```json
{
  "system": {
    "last_tick": "2026-05-23T14:05:00Z",
    "tick_count_24h": 288,
    "daemon_uptime_seconds": 86400,
    "priority_mode": "shadow",
    "active_channels": ["matrix", "log"]
  },
  "modules": [
    {"name": "location", "health": "healthy", "freshness_seconds": 45, "last_updated": "..."},
    ...
  ],
  "collectors": [
    {"name": "spotify", "last_poll": "2026-05-23T14:04:30Z", "state": "healthy"},
    ...
  ],
  "recent_alerts": [
    {"ts": "...", "slug": "rule_spare_room_hot", "severity": "warning", "category": "home", "sent": true},
    ...
  ],
  "priority_engine": { ... },
  "circuit_breakers": { ... },
  "exports": { ... },
  "context_summary": { ... }
}
```

### Frontend

Single HTML file with embedded CSS + JS. No build step. No framework.

- Auto-refresh every 30 seconds
- Collapsible sections
- Privacy toggle slider (PUBLIC_SAFE / AGENT_SAFE)
- Color-coded health indicators
- Responsive layout (works on phone or desktop)

### Endpoint

```
GET http://localhost:8147/dashboard        → HTML page
GET http://localhost:8147/api/dashboard    → JSON blob
```

The port (8147) matches the existing Helios tick/status port or is configurable.

### Not in Phase 1

- Authentication (local-only for now)
- WebSocket live updates (polling is fine)
- Write operations (read-only dashboard)
- Mobile app
- External access

## File Layout

```text
helios/helios/dashboard/
├── __init__.py
├── server.py          # Lightweight HTTP server
├── data.py            # Data collection from SQLite + JSON exports
├── privacy.py         # Privacy class filtering and sanitization
└── static/
    └── index.html      # Self-contained dashboard page
```

## Implementation Phases

### Phase 1: Data layer + JSON API
- `data.py` — Read SQLite, JSON exports, config
- `privacy.py` — Filter fields by privacy class
- `/api/dashboard` endpoint returning the JSON blob above

### Phase 2: Static dashboard page
- `index.html` with all sections above
- Auto-refresh, color coding, collapsible sections

### Phase 3: Integration
- Add `/dashboard` route to Helios daemon's HTTP handler
- Or: Standalone `python -m helios.dashboard` command
- Health check integration into `python -m helios.main health`