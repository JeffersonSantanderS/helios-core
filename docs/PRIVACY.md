# Privacy Policy

Helios is a **personal, self-hosted** intelligence engine. Privacy is not an afterthought — it is a core design principle.

---

## Data Ownership

**You own all your data.** Helios collects, processes, and stores your personal information locally on your machine. No data is sent to external services unless you explicitly configure an integration.

All runtime data lives under `~/.hermes/helios/` on your local filesystem. This directory is excluded from git by default.

---

## What data Helios collects

Helios can collect the following categories of personal data, depending on which modules you enable:

| Category | Examples | Storage |
|----------|----------|---------|
| **Location** | GPS coordinates, city/province, presence detection | `location_state.json`, `location_history.jsonl` |
| **Health** | Steps, heart rate, sleep duration, activity type | Health JSON files, SQLite |
| **Music** | Tracks played, listening history, skip counts | `spotify_state.json`, `spotify_history.jsonl` |
| **Calendar** | Events, schedules | `calendar_state.json` |
| **Reminders** | Tasks, due dates, completion status | `reminders_state.json` |
| **Contacts** | Names, phone numbers, email addresses | `contacts_state.json` |
| **Notes** | Note titles and metadata | `notes_state.json` |
| **Mood** | Self-reported mood scores (1–9) | `mood_state.json` |
| **Focus** | Foreground application, idle/active windows | `focus_state.json`, `idle_state.json` |
| **Habits** | Habit streaks and completion status | `habits_state.json` |
| **Nutrition** | Protein intake, nutrition logs | `protein_log.json` |

All of these are stored locally. None are uploaded to any cloud service by default.

---

## Where data lives

```text
~/.hermes/helios/
├── helios_v5.db              # Primary SQLite database (despite v5 name)
├── latest_status.json        # Module health summary
├── context_export.json       # Rolling context for downstream agents
├── data/                     # Module state files and histories
│   ├── spotify_state.json
│   ├── idle_state.json
│   ├── focus_state.json
│   ├── location_history.jsonl
│   ├── health/
│   └── insights/
│       └── *.json
├── config/
│   └── config.yaml           # Your local configuration
└── ~/.hermes/helios-v6/.icloud_session/   # Session cookies (never committed)
```

The `.gitignore` file excludes all runtime data, databases, logs, and session files from version control.

---

## Data sharing and external services

Helios is designed to work **offline and locally first**. External service connections are **opt-in** and require you to provide your own API keys:

| Service | Purpose | Data sent |
|---------|---------|-----------|
| **Open-Meteo** | Weather data | Location coordinates for forecasts |
| **Spotify Web API** | Music tracking | Read-only access to currently playing and history |
| **iCloud** | Calendar, reminders, contacts, notes, location | Apple ID credentials; session cookies stored locally |
| **Home Assistant** | Location, health sensors, smart home state | Local network API calls |
| **Matrix** | Briefings and alerts delivery | Formatted messages to your configured room |
| **LLM providers** | Optional briefing polish and analysis | Text context sent to your configured API endpoint |

You control every integration. Disable any service in `config.yaml` by setting `enabled: false` or removing its credentials.

---

## Credential safety

- **No credentials in git.** All secrets are loaded from environment variables or local config files under `~/.hermes/`, which is excluded from version control.
- **iCloud session cookies** are stored in `.icloud_session/`, which is gitignored. Passwords are never embedded in command lines or committed files.
- **API keys** are referenced via environment variables (see `.env.example`), never hardcoded.
- **OAuth tokens** (e.g., Spotify) are stored locally under `~/.hermes/helios/config/`.

---

## LLM usage

When LLM fallback is enabled:

- The configured LLM provider receives **text context** from your local data (e.g., a briefing draft).
- No raw health, location, or contact data is sent unless you explicitly configure a prompt that includes it.
- LLM calls are **capped** by a daily budget (`daily_cap` in config) and only invoked as polish/fallback, not as the primary decision engine.
- You can disable LLM entirely by setting `llm.enabled: false` in your config.

---

## Data retention

- **State files** (JSON) are overwritten each tick — only the current state is kept.
- **History files** (JSONL) are appended. You control retention by rotating or pruning these files.
- **SQLite database** accumulates metrics over time. You can back up or purge it as needed.
- **No remote telemetry.** Helios does not phone home or send analytics.

---

## Data deletion

To delete all Helios data:

```bash
# Remove all runtime state
rm -rf ~/.hermes/helios/

# Remove iCloud session cookies
rm -rf ~/.hermes/helios-v6/.icloud_session/

# Remove local configuration
rm -rf ~/.hermes/helios/config/
```

There is no remote data to delete — everything is local.

---

## Third-party services

Each third-party service you connect has its own privacy policy:

- [Spotify Privacy Policy](https://www.spotify.com/us/privacy/)
- [Apple Privacy Policy](https://www.apple.com/legal/privacy/)
- [Open-Meteo](https://open-meteo.com/en/terms) — free weather API, no registration required
- [Home Assistant Privacy](https://www.home-assistant.io/privacy/)
- Your chosen LLM provider's privacy policy

Helios itself does not act as an intermediary or proxy for any of these services.

---

## Changes to this policy

This privacy policy may be updated. Changes will be committed to this file in the repository. Review `git log docs/PRIVACY.md` for the history.

---

## Contact

For privacy questions or concerns, please open a GitHub issue or contact the maintainer directly.