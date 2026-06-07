# Helios

**Autonomous personal-intelligence engine with deterministic collection, rule evaluation, and optional LLM fallback.**

Helios is a self-healing, daemon-driven system that continuously collects data from your digital life — location, health, music, weather, calendar, habits, and more — and turns it into actionable briefings, alerts, and insights. It prefers deterministic scripts and rules first; language models are only invoked as polish or fallback.

---

## Features

- **Deterministic-first** — Rules, scripts, and SQLite power the core. LLM calls are opt-in and capped.
- **Self-healing** — Circuit breakers, health-state tracking, and automatic recovery keep the daemon running unattended.
- **Modular collectors** — Plug in data sources (Spotify, Home Assistant, iCloud, Apple Health, weather APIs) without touching the core loop.
- **Insight engine** — Timeline explorer, trend analysis, correlation detection, and narrative diffs — all with evidence traces.
- **Stable exports** — Atomic JSON contracts for downstream consumers (agents, dashboards, notifications).
- **Privacy by design** — All personal data stays local; no telemetry, no cloud dependency for core operation.

---

## Architecture

```text
┌────────────────────┐  systemd/user  ┌────────────────────┐
│ helios-v6.service   │───────────────>│ helios.engine      │
│ daemon tick loop    │                │ modules + rules    │
└─────────┬──────────┘                └─────────┬──────────┘
          │                                     │
          │ starts/monitors                     │ reads/writes
          ▼                                     ▼
┌────────────────────┐                ┌────────────────────┐
│ Collector scripts  │───────────────>│ ~/.hermes/helios/  │
│ Spotify/focus/idle │ raw JSON/JSONL │ data + SQLite      │
└────────────────────┘                └─────────┬──────────┘
                                                │
                                                │ exports
                                                ▼
                                      ┌────────────────────┐
                                      │ Briefings / alerts │
                                      │ Matrix / Obsidian  │
                                      └────────────────────┘
```

---

## Quick Start

### Prerequisites

- Python 3.10+
- A Linux system with systemd (for daemon mode) or any OS for manual/testing use

### Installation

```bash
git clone https://github.com/jefferson-north/helios-v6.git
cd helios-v6/helios

# Install with dev dependencies
pip install -e ".[dev]"

# Or add optional groups
pip install -e ".[dev,dashboard,collectors]"
```

### Configuration

1. Copy the sample config and edit it:

   ```bash
   mkdir -p ~/.hermes/helios/config
   cp helios/config/config.yaml ~/.hermes/helios/config/config.yaml
   ```

2. Copy `.env.example` to `~/.hermes/.env` and fill in your values:

   ```bash
   cp .env.example ~/.hermes/.env
   ```

3. Review `helios/config/config.yaml` — it contains safe defaults with all secrets referenced from environment variables.

### Running

```bash
# One-shot tick (collect + evaluate + export)
python -m helios.main tick

# Health check
python -m helios.main health

# Daemon mode (systemd)
systemctl --user enable --now helios-v6.service
```

### Running Tests

```bash
cd helios
python -m pytest -q
```

---

## Module Overview

| Module | Source | Description |
|--------|--------|-------------|
| Location | Home Assistant, iCloud Find My | Real-time location with city/province and confidence. |
| Weather | Open-Meteo API | Daily conditions, temperature, precipitation. |
| Health | Health Auto Export / Home Assistant | Steps, heart rate, sleep, activity, vitals. |
| Spotify | Spotify Web API | Current track, listening history, skip detection. |
| Calendar | iCloud sync | Today and upcoming events. |
| Reminders | iCloud sync | Open and overdue reminder items. |
| Mood | Discord / Matrix embed | Daily mood score and check-in scheduling. |
| Focus | Window tracker + idle detector | Foreground app, idle/active windows, app usage. |
| Habits | Local habit state | Streaks and completion tracking. |
| Insight | SQLite + event streams | Timeline exploration, trends, correlations. |

See the full [module table](docs/REPOSITORY_GUIDE.md) for every data source and collector.

---

## Project Structure

```text
.
├── helios/
│   ├── helios/              # Main package
│   ├── tests/               # Test suite
│   ├── config/config.yaml   # Safe sample config
│   └── pyproject.toml       # Package metadata
├── deploy/systemd/          # Systemd unit files
├── scripts/                 # Backfill and utility scripts
├── docs/                    # Design notes, roadmap, repository guide
└── requirements.txt         # Core runtime dependency (pyyaml)
```

---

## Privacy & Safety

- **No data leaves your machine** unless you explicitly configure an integration (Matrix, Discord, etc.).
- Health, contacts, location, and mood data live under `~/.hermes/helios/` — never in the git repo.
- iCloud credentials use session cookies; passwords are never stored in config or command lines.
- See [docs/PRIVACY.md](docs/PRIVACY.md) and [SECURITY.md](SECURITY.md) for full details.

---

## Contributing

We welcome contributions! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on:

- Code style and commit conventions
- How to add a new module or collector
- Testing requirements
- Pull request process

---

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgments

Helios builds on excellent open-source tools including [pyicloud](https://github.com/picklepete/pyicloud), [spotipy](https://github.com/spotipy-dev/spotipy), [Home Assistant](https://www.home-assistant.io/), and [Open-Meteo](https://open-meteo.com/).