# Helios Context Endpoint

A **local-first personal context layer** for AI agents — not a memory engine, not a knowledge base, not a vector store.

Helios Context Endpoint provides structured, real-time **situational context** that an AI agent can query to ground its reasoning in a user's current environment, preferences, and intent. It is a read-heavy, low-latency endpoint that answers the question: *"What is relevant about this user *right now*?"*

---

## Table of Contents

1. [What It Is (and Isn't)](#what-it-is-and-isnt)
2. [Architecture Overview](#architecture-overview)
3. [API Reference](#api-reference)
4. [Fake Data Examples](#fake-data-examples)
5. [Privacy Guarantees](#privacy-guarantees)
6. [Privacy Exclusions](#privacy-exclusions)
7. [Running & Testing](#running--testing)
8. [Configuration Reference](#configuration-reference)

---

## What It Is (and Isn't)

| It Is ✅ | It Isn't ❌ |
|---|---|
| A real-time context provider | A long-term memory engine |
| A structured query endpoint | A conversational history store |
| User-scoped and session-scoped | A global shared knowledge base |
| Designed for agent grounding | A RAG / vector search system |
| Read-optimized, low-latency | A write-heavy analytics pipeline |

Think of it as the **sensory layer** — what an agent "sees" about the user's world at the moment of interaction, not what it "remembers" from past interactions.

---

## Architecture Overview

```
┌──────────────┐      ┌─────────────────────┐      ┌──────────────────┐
│  AI Agent    │─────▶│  Context Endpoint    │─────▶│  Context Sources  │
│  (consumer)  │◀─────│  localhost:7421       │◀─────│  (adapters)       │
└──────────────┘      └─────────────────────┘      └──────────────────┘
                              │                            │
                              ▼                            ▼
                      ┌──────────────┐            ┌──────────────────┐
                      │  Policy Gate │            │ Calendar, FS,    │
                      │  (filtering) │            │ Preferences,     │
                      └──────────────┘            │ Activity, etc.   │
                                                  └──────────────────┘
```

All data stays on the user's machine. The Policy Gate applies per-field redaction rules before any context is returned, ensuring the agent never receives data the user hasn't explicitly allowed.

---

## API Reference

### Base URL

```
http://localhost:7421
```

### Endpoints

#### `GET /context`

Returns the full available context for the authenticated session.

**Request:**

```http
GET /context HTTP/1.1
Host: localhost:7421
Authorization: Bearer <session-token>
Accept: application/json
```

**Response:** `200 OK`

```json
{
  "session_id": "s_7f3a9c2e",
  "user_id": "u_a1b2c3d4",
  "timestamp": "2026-06-07T14:32:18Z",
  "context": { ... }
}
```

#### `GET /context/:domain`

Returns context scoped to a single domain (e.g., `calendar`, `activity`, `preferences`).

**Request:**

```http
GET /context/calendar HTTP/1.1
Host: localhost:7421
Authorization: Bearer <session-token>
```

**Response:** `200 OK`

```json
{
  "domain": "calendar",
  "items": [ ... ],
  "as_of": "2026-06-07T14:32:18Z"
}
```

**Available domains:** `calendar`, `activity`, `preferences`, `environment`, `focus`

#### `GET /health`

Liveness check.

```json
{ "status": "ok", "uptime_s": 43891 }
```

---

## Fake Data Examples

> **Note:** All data below is entirely synthetic. No real user information is represented.

### Full Context Response

```json
{
  "session_id": "s_7f3a9c2e",
  "user_id": "u_a1b2c3d4",
  "timestamp": "2026-06-07T14:32:18Z",
  "context": {
    "calendar": {
      "upcoming_events": [
        {
          "title": "Team standup",
          "start": "2026-06-07T15:00:00Z",
          "end": "2026-06-07T15:30:00Z",
          "location": "Room 4B",
          "attendees": 6
        },
        {
          "title": "Dentist appointment",
          "start": "2026-06-09T09:00:00Z",
          "end": "2026-06-09T09:45:00Z",
          "location": "Downtown Dental",
          "attendees": 1
        }
      ],
      "all_day_events": [
        {
          "title": "Conference: DevForward 2026",
          "date": "2026-06-12"
        }
      ]
    },
    "activity": {
      "recent_apps": [
        { "name": "VS Code", "last_active": "2026-06-07T14:20:00Z", "window_title": "helios-api.ts" },
        { "name": "Slack", "last_active": "2026-06-07T14:15:00Z", "window_title": "#engineering" },
        { "name": "Chrome", "last_active": "2026-06-07T14:01:00Z", "window_title": "MDN — Fetch API" }
      ],
      "active_project": "helios-context-endpoint",
      "screen_time_today_min": 187
    },
    "preferences": {
      "communication_style": "concise",
      "timezone": "America/Los_Angeles",
      "locale": "en-US",
      "preferred_name": "Jordan",
      "theme": "dark"
    },
    "environment": {
      "os": "Linux 6.5.0",
      "shell": "zsh",
      "editor": "vscode",
      "working_directory": "/home/jordan/projects/helios",
      "battery_level_pct": 72,
      "network": "home-wifi"
    },
    "focus": {
      "do_not_disturb": false,
      "current_task": "writing API documentation",
      "estimated_focus_score": 0.84,
      "interruptions_today": 3
    }
  }
}
```

### Calendar Domain Only

```json
{
  "domain": "calendar",
  "items": {
    "upcoming_events": [
      {
        "title": "Sprint planning",
        "start": "2026-06-08T10:00:00Z",
        "end": "2026-06-08T11:00:00Z",
        "location": "Zoom",
        "attendees": 9
      }
    ],
    "all_day_events": []
  },
  "as_of": "2026-06-07T14:32:18Z"
}
```

### Activity Domain Only

```json
{
  "domain": "activity",
  "items": {
    "recent_apps": [
      { "name": "Figma", "last_active": "2026-06-07T13:58:00Z", "window_title": "Dashboard v3" },
      { "name": "Terminal", "last_active": "2026-06-07T13:45:00Z", "window_title": "npm run dev" }
    ],
    "active_project": "dashboard-redesign",
    "screen_time_today_min": 134
  },
  "as_of": "2026-06-07T14:32:18Z"
}
```

### Preferences Domain Only

```json
{
  "domain": "preferences",
  "items": {
    "communication_style": "detailed",
    "timezone": "Europe/Berlin",
    "locale": "de-DE",
    "preferred_name": "Alex",
    "theme": "light"
  },
  "as_of": "2026-06-07T14:32:18Z"
}
```

### Environment Domain Only

```json
{
  "domain": "environment",
  "items": {
    "os": "macOS 15.4",
    "shell": "fish",
    "editor": "nvim",
    "working_directory": "/Users/alex/code/dashboard",
    "battery_level_pct": 45,
    "network": "office-ethernet"
  },
  "as_of": "2026-06-07T14:32:18Z"
}
```

### Focus Domain Only

```json
{
  "domain": "focus",
  "items": {
    "do_not_disturb": true,
    "current_task": "debugging auth middleware",
    "estimated_focus_score": 0.91,
    "interruptions_today": 1
  },
  "as_of": "2026-06-07T14:32:18Z"
}
```

### Error Response — Domain Not Found

```json
{
  "error": "domain_not_found",
  "message": "No context source registered for domain 'payroll'",
  "available_domains": ["calendar", "activity", "preferences", "environment", "focus"]
}
```

### Error Response — Unauthorized

```json
{
  "error": "unauthorized",
  "message": "Invalid or expired session token"
}
```

---

## Privacy Guarantees

The Helios Context Endpoint enforces the following privacy guarantees:

1. **Local-first — no cloud dependency.** All context data is stored and served from the user's local machine. No context is transmitted to any remote server by the endpoint itself. Network calls are made only by individual adapters (e.g., a calendar adapter reading from a local CalDAV cache) and never leave the machine without explicit user consent.

2. **Field-level redaction via Policy Gate.** Every context field passes through a configurable redaction policy before being returned. Fields can be set to `allow`, `redact` (replaced with `[REDACTED]`), or `drop` (omitted entirely). Default policy redacts all PII-adjacent fields.

3. **Session-scoped tokens.** Access requires a session token issued per agent session. Tokens expire after a configurable TTL (default: 90 minutes). Tokens cannot be renewed — a new session must be established.

4. **Domain-level access control.** Session tokens can be scoped to specific domains. An agent requesting `/context/calendar` with a token scoped only to `environment` and `focus` will receive a `403 Forbidden`.

5. **No persistent logging of queries.** The endpoint does not log which domains or fields an agent queries. Request metrics (response time, status code) are emitted as in-memory counters only and are never written to disk.

6. **No cross-user data leakage.** Each endpoint instance is single-user by design. There is no multi-tenancy, no shared state, and no possibility of one user's context leaking to another.

7. **Auditable policy files.** The redaction and access-control policy is defined in a human-readable YAML file (`~/.config/helios/policy.yaml`). Users can inspect and modify it at any time. Changes take effect immediately without restart.

8. **Right to wipe.** Users can delete their local context store at any time via `helios context wipe`, which irreversibly removes all cached context data. Adapters re-ingest fresh data only on the next query cycle.

9. **Minimal data retention.** Context data is ephemeral by default. Calendar items older than 7 days are purged. Activity history is capped at 24 hours. Preferences and environment data are refreshed on every query, not persisted.

10. **Consent-first adapter model.** No adapter (calendar, activity, etc.) is enabled unless the user explicitly opts in via `helios adapter enable <name>`. Disabled adapters return empty results — no errors, no fallback data.

---

## Privacy Exclusions

The following are **explicitly out of scope** and are **not protected** by the Helios Context Endpoint:

| Exclusion | Reason |
|---|---|
| **Agent-side behavior after receipt** | Once context is delivered to an agent, the endpoint has no control over how the agent stores, transmits, or processes it. Privacy of agent behavior is the responsibility of the agent runtime. |
| **Third-party API data** | If an adapter pulls data from a third-party API (e.g., a cloud calendar service), that API's own privacy policy applies. Helios only controls what happens on the local machine. |
| **End-to-end encryption in transit** | When the agent and endpoint communicate over `localhost`, transport is not encrypted (plain HTTP). This is acceptable for local-only traffic but must not be assumed secure if exposed over a network. |
| **Operating system-level access** | A compromised OS or root-level adversary can bypass all local privacy controls. Helios assumes a trusted execution environment. |
| **Memory persistence by consuming agents** | Agents that maintain conversational memory or state may retain context indefinitely. Helios cannot enforce deletion of data once it leaves the endpoint. |
| **Biometric or health data** | The endpoint does not ingest, store, or serve biometric or health data. Any such data appearing in context is a misconfiguration and should be reported. |
| **Legal compliance (GDPR, CCPA, etc.)** | While Helios aligns with privacy-first principles, it does not constitute a compliance solution. Organizations must independently verify regulatory requirements. |

---

## Running & Testing

### Prerequisites

- Go 1.22+ (for building from source) **or** the pre-built binary from the [releases page](https://github.com/example/helios/releases)
- A configured adapter (at least one enabled) — see `helios adapter list`

### Build from Source

```bash
git clone https://github.com/example/helios.git
cd helios
make build
./bin/helios --version
# helios v0.4.1 (mock-example)
```

### Quick Start

```bash
# Initialize config directory and default policy
helios init

# Enable at least one adapter
helios adapter enable calendar
helios adapter enable environment

# Start the endpoint (foreground)
helios serve --port 7421
```

### Verify It's Running

```bash
curl -s http://localhost:7421/health | jq .
# {
#   "status": "ok",
#   "uptime_s": 12
# }
```

### Query Context

```bash
# Full context (requires session token)
export HELIOS_TOKEN=$(helios session create --domains calendar,environment,focus)

curl -s -H "Authorization: Bearer $HELIOS_TOKEN" \
  http://localhost:7421/context | jq .

# Single domain
curl -s -H "Authorization: Bearer $HELIOS_TOKEN" \
  http://localhost:7421/context/calendar | jq .
```

### Run Tests

```bash
# Unit tests
make test

# Integration tests (spins up a temporary server)
make test-integration

# Test with fake data (no real adapters needed)
HELIOS_MOCK_ADAPTERS=true make test-integration
```

### Run with Mock Data Only

For development and testing without real adapters:

```bash
helios serve --port 7421 --mock-adapters
```

This returns the fake data examples shown above. No real user data is accessed.

### Docker (Optional)

```bash
docker build -t helios-context .
docker run --rm -p 7421:7421 \
  -v ~/.config/helios:/root/.config/helios \
  helios-context
```

---

## Configuration Reference

Main config file: `~/.config/helios/config.yaml`

```yaml
server:
  host: "127.0.0.1"       # Bind address (do NOT set to 0.0.0.0 unless you understand the risk)
  port: 7421
  session_ttl_min: 90

adapters:
  calendar:
    enabled: true
    source: "caldav-local"
    retention_days: 7
  activity:
    enabled: false        # Opt-in only
    retention_hours: 24
  preferences:
    enabled: true
  environment:
    enabled: true
  focus:
    enabled: true

policy:
  # Field-level redaction rules
  rules:
    calendar.title: allow
    calendar.location: redact
    calendar.attendees: drop
    activity.window_title: redact
    activity.active_project: allow
    preferences.preferred_name: allow
    environment.working_directory: allow
    focus.current_task: allow
    focus.focus_score: allow

logging:
  level: "warn"           # debug | info | warn | error
  persist: false          # Never write logs to disk when false
```

Policy file: `~/.config/helios/policy.yaml` (same `policy` section, can be split out for clarity)

---

## License

Helios Context Endpoint is released under the MIT License. See [LICENSE](../LICENSE) for details.