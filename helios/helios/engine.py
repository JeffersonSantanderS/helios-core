"""Helios v6 - Core tick engine.

Runs all enabled modules, evaluates rules, dispatches alerts.
Never calls LLM directly - queues llm_requests via state DB.

Modules are auto-discovered from modules/ directory. No hardcoded registry.
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
import pkgutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import (
    state as state_mod,
    circuit_breaker,
    rules_v2,
    config_loader,
)
from .matrix_pusher import MatrixPusher
from .dispatcher import AlertDispatcher
from .channels import ChannelRouter, AlertEvent, BriefingEvent, CheckinEvent, StatusEvent
from .channels.events import BaseEvent
from .modules.base import BaseMod
from .modules import action_engine as action_engine_mod
from .correlator import CorrelationEngine
from .obsidian_writer import ObsidianWriter
from .llm_bridge import LLMBridge
from .dm_listener import DMListener
from .data_ingestion import run_ingestion
from .mood_handler import MoodHandler, send_mood_checkin, handle_mood_reaction
from .predictor import PredictiveEngine
from .self_healing import SelfHealing
from .outcome_tracker import OutcomeTracker
from .stable_exports import write_all_exports
from .brain_state import BrainStateBuilder
from .narrative_engine import NarrativeEngine
from .reaction_poller import ReactionPoller

# ── Memory + Preference Layer (Phase 1) ──────────────────────────────
from .preference_engine import PreferenceEngine, get_preferences

# ── Phase 2: Module Health + Freshness Tracker ────────────────────────
from .module_health import ModuleHealthTracker, freshness_warning, is_health_alert

# ── Phase 3: Timeline Normalizer ──────────────────────────────────────
from .timeline_normalizer import TimelineNormalizer

# ── Phase 3.5: Timeline Compressor + Salience ─────────────────────────
from .timeline_compressor import TimelineCompressor

# ── Phase 4: Deterministic Narrative Reconstruction ────────────────────
from . import narrative_templates

# ── Phase 5: Operational Insight + Visualization ───────────────────────
from .insight_engine import generate_all_insights

# ── Daily Intelligence Loop (v6 live-data briefings) ───────────────────
from . import daily_intelligence

# ── Priority Engine (Phase 1: shadow-mode candidate ranking) ────────────
from .priority.engine import PriorityEngine
from .priority.summarizer import PrioritySummarizer
from .priority.dispatcher import PriorityDispatcher

# ── Self-Improvement Loop (shadow/apprentice/active) ──────────────────────
from .self_improvement import SelfImprovementIntegration

log = logging.getLogger("helios.engine")

DATA_DIR_NARRATIVE = Path.home() / ".hermes" / "helios" / "data" / "narratives"


class HeliosEngine:
    def __init__(self, db_path: Optional[str] = None, cfg: Optional[config_loader.ConfigLoader] = None,
                 start_services: bool = False):
        self.db = state_mod.HeliosDB(db_path)
        self.cfg = cfg or config_loader.ConfigLoader.load()
        self.cb = circuit_breaker.CircuitBreaker(
            failure_threshold=self.cfg.get("circuit_breaker", "failure_threshold", default=5),
        )
        self.modules: list[BaseMod] = []
        self.rules_engine = rules_v2.RulesEngine(self.db)
        self.matrix_pusher = MatrixPusher(cfg=self.cfg._data)
        # Channel router is the primary delivery path.
        # _emit_* helpers route through ChannelRouter. MatrixPusher is fallback.
        # Must be initialized before AlertDispatcher, action_engine, priority_dispatcher.
        self.channels = ChannelRouter.from_config(self.cfg._data)
        self.alert_dispatcher = AlertDispatcher(
            self.db, self.matrix_pusher, config=self.cfg._data.get("alerts", {}),
            channels=self.channels
        )
        self._consecutive_tick_failures: int = 0

        # Modules
        self.action_engine = action_engine_mod.ActionEngine(
            db_path=self.db.db_path,
            config=self.cfg._data,
        )
        # Phase 3: inject channel router into action engine for nudge delivery
        self.action_engine.channels = self.channels
        self.correlator = CorrelationEngine(
            db_path=self.db.db_path,
            config=self.cfg._data.get("correlator", {}),
        )
        obsidian_cfg = self.cfg._data.get("obsidian", {})
        vault_path = obsidian_cfg.get("vault_path", "") or self.cfg._data.get("obsidian_vault_path", "")
        self.obsidian = ObsidianWriter(
            vault_path=vault_path,
            enabled=obsidian_cfg.get("enabled", True),
            templates=obsidian_cfg.get("templates", True),
        )
        self.llm_bridge = LLMBridge(self.db, cfg=self.cfg)
        self.dm_listener = DMListener(self.db, cfg=self.cfg._data)

        # --- autoDream idle consolidation ---
        from .dream_engine import DreamEngine
        from .skeptical_memory import SkepticalMemory
        self.dream_engine = DreamEngine(self.db, cfg=self.cfg._data)
        self.skeptical_memory = SkepticalMemory(self.db)
        self._dream_notified: set[str] = set()

        # --- Collector subprocess management ---
        self._collector_procs: list[subprocess.Popen] = []

        self._load_modules()

        # --- Phase 4: Predictive Engine ---
        self.predictor = PredictiveEngine(self.db)

        # --- Phase 4: Self-Healing Engine ---
        self.healer = SelfHealing(self.db, engine=self)

        # --- Phase 4+: Outcome Tracker ---
        self.outcomes = OutcomeTracker(self.db)

        # --- Phase 1: Memory + Preference Layer ---
        self.preferences = PreferenceEngine(db_path=self.db.db_path)
        self.preferences.load()

        # --- Phase 2: Module Health + Freshness Tracker ---
        self.health = ModuleHealthTracker()

        # --- Phase 3: Timeline Normalizer ---
        self.timeline = TimelineNormalizer(db_path=self.db.db_path)

        # --- Phase 3.5: Timeline Compressor + Salience ---
        self.compressor = TimelineCompressor(db_path=self.db.db_path)

        # --- Priority Engine: shadow-mode candidate ranking ---
        self.priority = PriorityEngine(
            self.db,
            cfg=self.cfg._data.get("priority", {}),
            preferences=self.preferences,
            health=self.health,
        )
        self.summarizer = PrioritySummarizer(self.db)
        self.priority_dispatcher = PriorityDispatcher(self.matrix_pusher, self.alert_dispatcher, channels=self.channels)
        self._priority_summary_sent_today: str = ""
        self._daily_scan_done_today: str = ""  # date key

        # --- Self-Improvement Loop (shadow/apprentice/active) ---
        si_cfg = self.cfg._data.get("self_improvement", {})
        self.self_improvement = SelfImprovementIntegration(cfg=dict(si_cfg))

        # --- Brain State v1 (unified deterministic export) ---
        self.brain_state = BrainStateBuilder(
            db_path=self.db.db_path,
            health=self.health,
            preferences=self.preferences,
            rules_engine=self.rules_engine,
            config=self.cfg._data,
        )

        # --- Start long-lived services if in daemon mode ---
        self._mood_handler: MoodHandler | None = None
        self._reaction_poller: ReactionPoller | None = None
        self.watcher = None
        if start_services:
            self.start_services()

    def _discover_modules(self) -> dict[str, type]:
        """Scan modules/ directory and find all BaseMod subclasses.
        Returns dict mapping module name to class.
        """
        import helios.modules as mod_pkg
        discovered: dict[str, type] = {}
        pkg_path = str(Path(mod_pkg.__file__).parent)

        for finder, mod_name, is_pkg in pkgutil.iter_modules(
            [pkg_path], prefix="helios.modules."
        ):
            base_name = mod_name.rsplit(".", 1)[-1]
            if base_name.startswith("_") or base_name in ("base", "__init__", "action_engine"):
                continue
            try:
                mod = importlib.import_module(mod_name)
            except Exception as exc:
                log.warning("Failed to import %s: %s", mod_name, exc)
                continue
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    inspect.isclass(attr)
                    and issubclass(attr, BaseMod)
                    and attr is not BaseMod
                    and attr.__module__ == mod_name
                ):
                    manifest = getattr(attr, "MODULE_MANIFEST", {})
                    name = manifest.get("name") or attr_name.lower().replace("module", "")
                    if name:
                        discovered[name] = attr
                        log.debug("Discovered: %s -> %s.%s", name, mod_name, attr_name)
        return discovered

    def _load_modules(self) -> None:
        """Load modules from config, discovered automatically from modules/."""
        mod_cfg = self.cfg.modules
        discovered = self._discover_modules()
        for name, mod_conf in mod_cfg.items():
            if not mod_conf.get("enabled", True):
                log.info("Module %s: disabled, skipping", name)
                continue
            cls = discovered.get(name)
            if not cls:
                log.warning("Module '%s': configured but no implementation found", name)
                continue
            try:
                instance = cls(db_path=self.db.db_path, config=mod_conf)
                self.modules.append(instance)
                info = instance.module_info()
                log.info(
                    "Loaded %s v%s: %s (priority=%d)",
                    name, info.get("version", "?"),
                    info.get("description", ""), info.get("priority", 0),
                )
            except Exception as exc:
                log.warning("Failed to load module %s: %s", name, exc)
                self.cb.record_failure(name)

    # ── Channel emit helpers ─────────────────────────────────────────────
    def _emit(self, event: BaseEvent) -> list:
        """Send an event through the ChannelRouter. Falls back to self.matrix_pusher
        if no channels are configured or all channels fail.

        In shadow mode, only LogChannel receives the event.
        Returns the list of ChannelResult objects.
        """
        try:
            results = self.channels.send(event)
            return results
        except Exception as exc:
            log.warning("ChannelRouter send failed for %s: %s", event.title, exc)
            return []

    def _emit_briefing(self, title: str, priority: int, embed: dict,
                       briefing_type: str = "morning") -> None:
        """Send a briefing event through channels. Primary delivery path."""
        self._emit(BriefingEvent(
            title=title, priority=priority, embed=embed,
            briefing_type=briefing_type, category="briefing",
        ))

    def _emit_status(self, title: str, message: str = "", priority: int = 2,
                     category: str = "system", source: str = "",
                     embed: dict | None = None) -> None:
        """Send a status event through channels. Primary delivery path."""
        self._emit(StatusEvent(
            title=title, message=message, priority=priority,
            category=category, source=source, embed=embed,
        ))

    def _emit_alert(self, title: str, message: str = "", severity: str = "info",
                    priority: int = 2, category: str = "system", source: str = "",
                    embed: dict | None = None) -> None:
        """Send an alert event through channels. Primary delivery path."""
        self._emit(AlertEvent(
            title=title, message=message, severity=severity,
            priority=priority, category=category, source=source, embed=embed,
        ))

    def _emit_checkin(self, title: str, message: str = "", priority: int = 1,
                      checkin_type: str = "mood",
                      prompt_options: list | None = None,
                      metadata: dict | None = None) -> None:
        """Send a check-in event through channels."""
        self._emit(CheckinEvent(
            title=title, message=message, priority=priority,
            category=checkin_type, source="helios",
            checkin_type=checkin_type,
            prompt_options=prompt_options, metadata=metadata,
        ))

    def _dispatch_module_notifications(self, context: dict[str, Any]) -> None:
        """Dispatch review-only notifications requested by deterministic modules.

        Modules own analysis and dedupe keys; the engine owns outbound channels.
        This preserves the module boundary while avoiding direct Matrix coupling
        inside individual modules.
        """
        work_hours = context.get("work_hours") or {}
        if not isinstance(work_hours, dict) or not work_hours.get("should_notify"):
            return

        notify_key = str(work_hours.get("notify_key") or "")
        report_text = str(work_hours.get("report_text") or "").strip()
        if not notify_key or not report_text:
            return

        message = (
            "Bi-weekly job-hours draft is ready for review. "
            "This is a draft only — approve or correct it before submitting.\n\n"
            f"```text\n{report_text}\n```\n"
            f"Total drafted paid hours: {work_hours.get('paid_hours_total', 0)}\n"
            f"Confidence: {work_hours.get('confidence_summary', {})}"
        )
        self._emit_status(
            "🧾 Work Hours Draft",
            message=message,
            priority=1,
            category="work_hours",
            source="work_hours",
        )

        for mod in self.modules:
            if getattr(mod, "name", "") == "work_hours":
                marker = getattr(mod, "mark_notification_sent", None)
                if callable(marker):
                    try:
                        marker(notify_key)
                    except Exception as exc:
                        log.warning("Failed to mark work-hours notification sent: %s", exc)
                break

    def _dispatch_arrival_notification(self, context: dict[str, Any]) -> str:
        """Dispatch arrival notification on away→home zone transition.

        Window: 15:00-23:00 MDT. Once per day via intelligence_state dedup.
        Returns status string for logging.
        """
        _MDT = ZoneInfo("America/Edmonton")

        location = context.get("location") or {}
        zone_transition = location.get("zone_transition", "")

        # Only trigger on arrival
        if zone_transition != "arrival":
            return "no_arrival"

        # Time window: 15:00-22:59 MDT (inclusive)
        now_mdt = datetime.now(_MDT)
        hour_mdt = now_mdt.hour
        if not (15 <= hour_mdt <= 22):
            return f"outside_window(hour={hour_mdt})"

        # Dedup: once per calendar day
        today_str = now_mdt.strftime("%Y-%m-%d")
        dedup_key = f"arrival_{today_str}"

        state_path = Path.home() / ".hermes" / "helios" / "data" / "intelligence_state.json"
        li_state = {}
        if state_path.exists():
            try:
                li_state = json.loads(state_path.read_text())
            except Exception:
                li_state = {}

        if li_state.get(dedup_key):
            return f"already_sent_{today_str}"

        # Build rich arrival message with context
        health = context.get("health") or {}
        sleep_hours = health.get("sleep_hours", "?")
        steps = health.get("steps", "?")
        spotify = context.get("spotify") or {}
        spotify_mins = spotify.get("minutes_today", 0)

        # Module health summary
        try:
            mh_summary = self.health.summary()
            unhealthy = [k for k, v in mh_summary.items()
                         if isinstance(v, dict) and v.get("state") != "healthy"]
            if unhealthy:
                health_note = f"⚠️ {len(unhealthy)} collector issue(s)"
            else:
                health_note = "✅ All collectors healthy"
        except Exception:
            health_note = "Health check unavailable"

        # Format values nicely
        sleep_str = f"{sleep_hours}h" if sleep_hours and sleep_hours != "?" else "?"
        steps_str = str(steps) if steps and steps != "?" else "?"

        from_zone = location.get("from_zone", "?")
        to_zone = location.get("to_zone", "home")

        message = (
            f"🏠 Arrived home at {now_mdt.strftime('%I:%M %p')} MDT "
            f"({from_zone} → {to_zone}).\n\n"
            f"📋 Context:\n"
            f"  • Sleep: {sleep_str} · Steps: {steps_str}\n"
            f"  • Spotify today: {spotify_mins:.0f} min\n"
            f"  • {health_note}"
        )

        self._emit_status(
            "🏠 Arrived Home",
            message=message,
            priority=1,
            category="arrival",
            source="location",
        )

        # Mark dedup key
        li_state[dedup_key] = True
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(li_state))
        except Exception as exc:
            log.warning("Failed to write arrival dedup state: %s", exc)

        return f"sent_{today_str}"

    def tick(self) -> dict[str, Any]:
        """Run one engine tick."""
        # Drain pending event-driven triggers before full tick
        pending_modules = []
        if hasattr(self, 'watcher') and self.watcher:
            pending_modules = self.watcher.drain()
        if pending_modules:
            self.tick_targeted(pending_modules)

        ts = datetime.now(timezone.utc).isoformat()
        log.info("=== tick %s ===", ts)
        context: dict[str, Any] = {}

        for mod in self.modules:
            breaker_name = mod.name
            if not self.cb.should_attempt(breaker_name):
                log.info("Skipping %s: circuit %s", breaker_name, self.cb.state(breaker_name))
                context[breaker_name] = {"_status": self.cb.state(breaker_name)}
                self.health.record_skipped(breaker_name, reason=self.cb.state(breaker_name))
                continue
            try:
                result = mod._run_tick(self.db)
                self.cb.record_success(breaker_name)
                context[breaker_name] = result
                self.health.record_tick(breaker_name, result)
            except Exception as exc:
                log.exception("Module %s failed: %s", breaker_name, exc)
                self.cb.record_failure(breaker_name)
                context[breaker_name] = {"_error": str(exc)}
                self.health.record_tick(breaker_name, {"_error": str(exc)}, error=exc)

        self._dispatch_module_notifications(context)

        # ── Arrival notification on zone transition ──
        try:
            arrival_result = self._dispatch_arrival_notification(context)
            if arrival_result and "sent" in arrival_result:
                log.info("Arrival notification: %s", arrival_result)
        except Exception as exc:
            log.warning("Arrival notification dispatch failed: %s", exc)

        # Run data ingestion — populates focus/mood/metric_snapshots from raw files
        try:
            ingestion_counts = run_ingestion(self.db.db_path, self.cfg)
        except Exception as exc:
            log.warning("Data ingestion failed: %s", exc)
            ingestion_counts = {}

        # --- Phase 3: Normalize timeline events from fresh data ---
        try:
            new_events = self.timeline.normalize(self.db._conn())
            if new_events:
                log.info("Timeline: %d new events normalized", new_events)
        except Exception as exc:
            log.warning("Timeline normalization failed: %s", exc)

        # --- Phase 3.5: Compress timeline into sessions + score salience ---
        try:
            sessions = self.compressor.compress(self.db._conn())
            if sessions:
                log.info("Compressor: %d sessions from timeline events", sessions)
        except Exception as exc:
            log.warning("Timeline compression failed: %s", exc)

        # --- Phase 4: Generate deterministic narrative from timeline ---
        try:
            now_mdt = datetime.now(timezone.utc) - timedelta(hours=6)
            today_str = now_mdt.strftime("%Y-%m-%d")
            narrative_statements = narrative_templates.generate_narrative(
                self.db._conn(), today_str
            )
            if narrative_statements:
                # Save to disk for future delivery layers
                md = narrative_templates.format_narrative_markdown(
                    narrative_statements, today_str
                )
                j = narrative_templates.format_narrative_json(narrative_statements)
                out_file = DATA_DIR_NARRATIVE / f"daily_narrative_{today_str}.json"
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_text(json.dumps(j, indent=2))
                md_file = DATA_DIR_NARRATIVE / f"daily_narrative_{today_str}.md"
                md_file.write_text(md)
                log.info("Narrative: %d statements saved", len(narrative_statements))
        except Exception as exc:
            log.warning("Narrative generation failed: %s", exc)

        # --- Phase 5: Generate operational insight exports (once per 6h) ---
        try:
            now_ts = datetime.now(timezone.utc).timestamp()
            if not hasattr(self, '_last_insight_ts') or (
                now_ts - self._last_insight_ts > 21600):
                insights = generate_all_insights(self.db._conn(), window_days=7)
                self._last_insight_ts = now_ts
                log.info("Insight engine: exports refreshed")
        except Exception as exc:
            log.warning("Insight engine failed: %s", exc)

        # --- Send mood check-in embed once per day (first tick after 7 AM MDT) ---
        self._maybe_send_mood_checkin()

        # --- Poll Matrix reactions on bot messages (emoji mood, etc.) ---
        self.tick_reactions()

        # Evaluate rules against collected context
        rule_hits = self.rules_engine.evaluate(context)

        # ── Shadow Priority Engine: observe and score without blocking ──
        priority_result = None
        try:
            mode = self.cfg._data.get("priority", {}).get("mode", "shadow")
            if mode != "disabled":
                context["_module_health"] = self.health.summary()
                priority_result = self.priority.evaluate_tick(
                    context=context,
                    rule_hits=rule_hits,
                    mode=mode,
                )
                log.info(
                    "Priority Engine: generated=%d scored=%d selected=%d mode=%s",
                    priority_result.generated_count,
                    priority_result.scored_count,
                    priority_result.selected_count,
                    priority_result.mode,
                )
        except Exception as exc:
            log.warning("Priority Engine failed: %s", exc)

        # ── Phase 4: Soft Control — suppress duplicates/low-score non-critical ──
        suppressed_slugs: set[str] = set()
        if priority_result is not None:
            pri_cfg = self.cfg._data.get("priority", {})
            soft_enabled = pri_cfg.get("soft_control", {}).get("enabled", False)
            if soft_enabled and priority_result.mode in ("shadow", "soft_control"):
                suppressed_slugs = self.priority.get_suppressed_rule_slugs(priority_result)
                if suppressed_slugs:
                    log.info("Soft control: suppressing %d rule hits: %s", len(suppressed_slugs), suppressed_slugs)

        # ── Phase 5: Priority-Controlled Dispatch ────────────────────
        if priority_result is not None:
            if priority_result.mode == "priority_dispatch":
                log.info("Priority dispatch: dispatching %d selected candidates", priority_result.selected_count)
                try:
                    dispatched = self.priority_dispatcher.dispatch_result(priority_result, context)
                    if dispatched:
                        log.info("Priority dispatch: sent %d candidates", len(dispatched))
                except Exception as exc:
                    log.warning("Priority dispatch failed: %s", exc)
            elif priority_result.mode == "shadow":
                # In shadow mode, run dispatch for side effects (summary queue persistence,
                # metrics) without actually sending Matrix messages.
                try:
                    dispatched = self.priority_dispatcher.dispatch_result(
                        priority_result, context, shadow_mode=True
                    )
                    if dispatched:
                        log.debug("Priority shadow dispatch: tracked %d candidates", len(dispatched))
                except Exception as exc:
                    log.warning("Priority shadow dispatch failed: %s", exc)
        else:
            # Legacy rule dispatch (soft_control / disabled)
            if rule_hits:
                log.info("Rule hits: %d", len(rule_hits))
                for hit in rule_hits:
                    slug = hit.get("slug", "")
                    if slug in suppressed_slugs:
                        log.info("Soft control: skipped dispatch for %s (suppressed by Priority Engine)", slug)
                        continue
                    self._dispatch_action(hit, context)

        # ── Self-Improvement: Record learning events from priority decisions ──
        if priority_result is not None and priority_result.decisions:
            try:
                si_event_ids = self.self_improvement.record_priority_decision(
                    priority_result, context
                )
                if si_event_ids:
                    log.debug("Self-improvement: recorded %d learning events", len(si_event_ids))
            except Exception as exc:
                log.warning("Self-improvement recording failed: %s", exc)

        # ── Daily Intelligence Loop ─────────────────────────────────
        now_mdt = datetime.now(timezone.utc) - timedelta(hours=6)
        current_hour = now_mdt.hour

        # Refresh inferred patterns once per tick cycle if stale
        try:
            self.preferences.maybe_refresh_patterns()
        except Exception:
            pass

        # Prefs for adaptive behaviour
        quiet = self.preferences.is_quiet_hours()
        morning_hour = int(self.preferences.get("morning_hour_start", 7))
        evening_hour = int(self.preferences.get("evening_hour_start", 21))

        # Check if morning/evening already sent today via state file
        from pathlib import Path as _Path
        li_state_file = _Path.home() / ".hermes" / "helios" / "data" / "intelligence_state.json"
        li_state = {}
        if li_state_file.exists():
            try:
                li_state = json.loads(li_state_file.read_text())
            except Exception:
                pass
        today_mdt_str = now_mdt.strftime("%Y-%m-%d")

        # Morning briefing: first tick after configured morning hour MDT
        # Suppress during quiet hours
        if (current_hour >= morning_hour
                and not quiet
                and not li_state.get("morning_sent_" + today_mdt_str)):
            try:
                morning = daily_intelligence.generate_morning(
                    self.db.db_path, context,
                    prefs=self.preferences,
                    health=self.health,
                )
                if morning["embed"]:
                    log.info("Morning briefing generated via embed")
                    # Phase 5: primary delivery through channel system
                    self._emit_briefing(
                        "☀️ Morning Briefing", priority=1, embed=morning["embed"],
                        briefing_type="morning",
                    )
                    li_state["morning_sent_" + today_mdt_str] = True
            except Exception as exc:
                log.warning("Morning briefing failed: %s", exc)

        # Evening wrap: first tick after configured evening hour MDT
        if current_hour >= evening_hour and not li_state.get("evening_sent_" + today_mdt_str):
            try:
                evening = daily_intelligence.generate_evening(
                    self.db.db_path, context,
                    prefs=self.preferences,
                    health=self.health,
                )
                if evening["embed"]:
                    log.info("Evening wrap generated via embed")
                    # Phase 5: primary delivery through channel system
                    self._emit_briefing(
                        "🌙 Evening Wrap", priority=1, embed=evening["embed"],
                        briefing_type="evening",
                    )
                    li_state["evening_sent_" + today_mdt_str] = True
            except Exception as exc:
                log.warning("Evening wrap failed: %s", exc)

        # ── Phase 3: Optional daily priority summary (advisory only) ──
        pri_cfg = self.cfg._data.get("priority", {})
        if pri_cfg.get("summarizer", {}).get("enabled", False):
            summary_hour = int(pri_cfg.get("summarizer", {}).get("hour", evening_hour))
            if current_hour >= summary_hour and not self._priority_summary_sent_today == today_mdt_str:
                try:
                    summary = self.summarizer.generate(hours=pri_cfg.get("summarizer", {}).get("window_hours", 24))
                    self.summarizer.write(summary, tag=today_mdt_str)
                    if pri_cfg.get("summarizer", {}).get("matrix_enabled", False):
                        embed = self.summarizer.matrix_embed(summary)
                        log.info("Priority summary generated")
                        # Phase 5: primary delivery through channel system
                        self._emit_status(
                            "📊 Priority Summary", priority=1, embed=embed,
                            category="priority",
                        )
                    self._priority_summary_sent_today = today_mdt_str
                except Exception as exc:
                    log.warning("Priority summarizer failed: %s", exc)

        if li_state:
            try:
                li_state_file.parent.mkdir(parents=True, exist_ok=True)
                li_state_file.write_text(json.dumps(li_state))
            except Exception:
                pass

        # ── Phase 2: Save module health + run retention ────────────
        try:
            self.health.save()
        except Exception:
            pass

        # Write stable JSON exports (atomic writes)
        try:
            ts_iso = datetime.now(timezone.utc).isoformat()
            write_all_exports(self.db.db_path, self.health, last_tick_at=ts_iso)
        except Exception as exc:
            log.warning("Stable exports failed: %s", exc)

        # ── Brain v1: unified deterministic state export ──
        try:
            self.brain_state.export()
        except Exception as exc:
            log.warning("Brain state export failed: %s", exc)

        # Phase 1.5: periodic focus retention (every 6h)
        try:
            now_ts = datetime.now(timezone.utc).timestamp()
            if not hasattr(self, '_last_retention_ts'):
                self._last_retention_ts = 0.0
            if now_ts - self._last_retention_ts > 21600:
                self._run_focus_retention()
                self._last_retention_ts = now_ts
        except Exception:
            pass

        # Log tick summary
        self.db.insert_decision(
            decision_type="tick_summary",
            source="script_engine",
            action="engine_tick",
            context={"modules": list(context.keys()), "rule_hits": len(rule_hits)},
            module="engine",
        )
        # --- autoDream: check if we should consolidate ---
        if hasattr(self, 'dream_engine') and self.dream_engine.should_dream():
            try:
                dream_result = self.dream_engine.run_dream_cycle()
                last_dream_ts = dream_result.get("ts", "")
                if last_dream_ts not in self._dream_notified:
                    summary = self.dream_engine.get_idle_summary()
                    if summary:
                        self._emit_status(f"🧠 {summary}", priority=1, category="dream", source="dream_engine")
                        self._dream_notified.add(last_dream_ts)
                        if len(self._dream_notified) > 10:
                            self._dream_notified = set(sorted(self._dream_notified)[-10:])

                    # Phase 3: Push proactive recommendations
                    proactive = dream_result.get("proactive", {})
                    recs = proactive.get("recommendations", [])
                    if recs:
                        rec_text = self.dream_engine.proactive.format_recommendations(recs)
                        if rec_text:
                            self._emit_status("Dream Recommendations", message=rec_text, priority=2, category="dream", source="dream_engine")

                    # Phase 3: Push health score if notable (max 1 per local day)
                    hlth = proactive.get("health_score")
                    if hlth:
                        local_tz = ZoneInfo("America/Edmonton")
                        today_local = datetime.now(local_tz).strftime("%Y-%m-%d")

                        # Skip if already pushed today
                        if getattr(self, '_health_score_pushed_date', None) == today_local:
                            log.debug("Health score already pushed today (%s)", today_local)
                        elif hlth.get("is_partial") and len(hlth.get("missing_metrics", [])) >= 2:
                            log.info("Health score skipped: %d missing core metrics",
                                     len(hlth["missing_metrics"]))
                        elif hlth.get("total", 100) < 45:
                            health_msg = self.dream_engine.proactive.format_health_score(hlth)
                            self._emit_status(
                                f"📊 Daily Health", message=health_msg,
                                priority=1, category="health", source="dream_engine",
                            )
                            self._health_score_pushed_date = today_local

                    # Phase 3: Push weekly digest on Sundays
                    weekly = proactive.get("weekly_digest")
                    if weekly:
                        self._emit_status(
                            "Weekly Dream Digest", message=weekly["body"],
                            priority=2, category="dream", source="dream_engine",
                        )
            except Exception as exc:
                log.warning("Dream cycle failed: %s", exc)

        # Phase 3: Tick-level proactive alerts (urgent only, with daily dedup)
        if hasattr(self, 'dream_engine') and hasattr(self.dream_engine, 'proactive'):
            try:
                tick_alerts = self.dream_engine.proactive.tick_check()
                if tick_alerts:
                    _MDT = ZoneInfo("America/Edmonton")
                    today_key = datetime.now(_MDT).strftime("%Y-%m-%d")
                    _li_path = Path.home() / ".hermes" / "helios" / "data" / "intelligence_state.json"
                    _li_state = {}
                    if _li_path.exists():
                        try:
                            _li_state = json.loads(_li_path.read_text())
                        except Exception:
                            _li_state = {}
                    for alert in tick_alerts[:2]:  # max 2 per tick
                        alert_type = alert.get("type", "unknown")
                        dedup_key = f"proactive_{alert_type}_{today_key}"
                        if _li_state.get(dedup_key):
                            continue  # already sent this alert today
                        self._emit_alert(
                            title=alert['title'], message=alert.get('detail', ''),
                            severity="warning", priority=2, category="dream",
                            source="dream_engine",
                        )
                        _li_state[dedup_key] = True
                    try:
                        _li_path.parent.mkdir(parents=True, exist_ok=True)
                        _li_path.write_text(json.dumps(_li_state))
                    except Exception as exc:
                        log.warning("Failed to write proactive alert dedup state: %s", exc)
            except Exception as exc:
                log.debug("Proactive tick check: %s", exc)

        # Phase 4: Predictive alerts — project trends, warn on danger zones
        try:
            pred_alerts = self.predictor.tick_check()
            if pred_alerts:
                pred_text = PredictiveEngine.format_alerts(pred_alerts)
                if pred_text:
                    self._emit_alert(
                        title="Predictive Alert", message=pred_text,
                        severity="warning", priority=2, category="prediction",
                        source="predictive_engine",
                    )
                # Log each prediction for outcome tracking
                for alert in pred_alerts:
                    self.outcomes.log_prediction(alert)
        except Exception as exc:
            log.warning("Predictive check failed: %s", exc)

        # Phase 4+: Daily correlation re-scan (once per day)
        today_mdt = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%d")
        if self._daily_scan_done_today != today_mdt:
            try:
                self.correlator.run_daily_scan()
                self._daily_scan_done_today = today_mdt
            except Exception as exc:
                log.warning("Daily correlation scan failed: %s", exc)

        # Phase 4+: Evaluate pending prediction outcomes
        try:
            self.outcomes.evaluate_predictions()
        except Exception as exc:
            log.warning("Outcome evaluation failed: %s", exc)

        # Phase 4: Self-healing — monitor collectors, data freshness, ingestion
        try:
            per_tick_rows = sum(ingestion_counts.values()) if ingestion_counts else 0
            heal_actions = self.healer.tick_check(per_tick_rows)
            if heal_actions:
                heal_text = SelfHealing.format_alerts(heal_actions)
                if heal_text:
                    # Phase 5: primary delivery through channel system
                    self._emit_status(
                        "Self-Healing Alert", message=heal_text, priority=2,
                        category="system_health", source="self_healing",
                    )
            # Check for heal markers and restart dead collectors
            self._heal_collectors()
        except Exception as exc:
            log.warning("Self-healing failed: %s", exc)

        # Phase 4: Weekly digest — push every Sunday, decoupled from dream cycle
        self._maybe_push_weekly()

        # Write daily note to Obsidian
        try:
            if hasattr(self, "obsidian") and self.obsidian.enabled:
                self.obsidian.write_daily(context, db_path=self.db.db_path)
                self.obsidian.write_status(context)
        except Exception as exc:
            log.warning("Obsidian write failed: %s", exc)

        # Fire mood check-in if due (every 6 hours)
        mood_cfg = self.cfg._data.get("matrix", {}).get("mood", {})
        if mood_cfg.get("enabled", True):
            try:
                import json as _json
                from pathlib import Path as _Path
                checkin_path = _Path.home() / ".hermes" / "helios" / "data" / "checkin_state.json"
                checkin = {}
                if checkin_path.exists():
                    checkin = _json.loads(checkin_path.read_text())
                
                from datetime import datetime as _dt, timezone as _tz
                now = _dt.now(_tz.utc)
                interval = mood_cfg.get("interval", 21600)
                last = checkin.get("last_mood_checkin")
                
                should_checkin = False
                if last:
                    try:
                        last_dt = _dt.fromisoformat(last.replace("Z", "+00:00"))
                        should_checkin = (now - last_dt).total_seconds() >= interval
                    except Exception:
                        should_checkin = True
                else:
                    should_checkin = True
                
                    if should_checkin:
                        emoji_scale = mood_cfg.get("emoji_scale", [
                            {"emoji": "😄", "score": 9},
                            {"emoji": "🙂", "score": 7},
                            {"emoji": "😐", "score": 5},
                            {"emoji": "😔", "score": 3},
                            {"emoji": "😢", "score": 1},
                        ])
                        emojis = " ".join(e["emoji"] for e in emoji_scale)
                        msg = f"Mood check-in! How are you feeling?\n{emojis}\nReact with your emoji!"
                        
                        # Phase 5: route through channel system
                        self._emit_checkin(
                            title="Mood Check-in",
                            message=msg,
                            priority=1,
                            checkin_type="mood",
                            prompt_options=[(e["score"], e["emoji"]) for e in emoji_scale],
                            metadata={"source": "engine_tick"},
                        )
                    
                    checkin["last_mood_checkin"] = now.isoformat()
                    checkin["checkin_count_today"] = checkin.get("checkin_count_today", 0) + 1
                    checkin["checkin_due"] = False
                    checkin_path.parent.mkdir(parents=True, exist_ok=True)
                    checkin_path.write_text(_json.dumps(checkin, indent=2))
                    log.info("Mood check-in sent to Matrix")
            except Exception as exc:
                log.warning("Mood check-in failed: %s", exc)

        # Process any pending LLM requests
        try:
            if hasattr(self, "llm_bridge"):
                results = self.llm_bridge.process_pending(limit=2)
                if results:
                    log.info("LLM bridge processed %d requests", len(results))
        except Exception as exc:
            log.warning("LLM bridge failed: %s", exc)

        # Process any pending DM queries (poll mode)
        try:
            if hasattr(self, "dm_listener"):
                dm_results = self.dm_listener.poll_once()
                if dm_results:
                    log.info("DM listener processed %d queries", len(dm_results))
        except Exception as exc:
            log.warning("DM listener failed: %s", exc)

        return context

    def _dispatch_action(self, hit: dict, context: dict) -> None:
        # ── Interrupt filter: defer non-urgent alerts during work hours ──
        alert_priority = hit.get("priority", 1)
        should_push, reason = daily_intelligence.InterruptFilter.should_push(
            self.db.db_path, hit,
            prefs=self.preferences,
            health=self.health,
        )
        if not should_push:
            log.debug("Alert deferred: %s (%s)", hit.get("slug", "?"), reason)
            return

        action_cfg = hit.get("action_config")
        if not action_cfg:
            return
        # Parse JSON string if needed (SQLite stores as TEXT)
        if isinstance(action_cfg, str):
            import json as _json
            action_cfg = _json.loads(action_cfg)
        try:
            action_name = action_cfg.get("action", "notify")

            # Matrix push actions — route through AlertDispatcher for rate limiting
            if action_name in ("push_dm", "push", "matrix_push", "push_routed"):
                # v6: use AlertDispatcher for rate-limited dispatch
                self.alert_dispatcher.dispatch(hit, context)
                result = {"status": "dispatched"}
            else:
                result = self.action_engine.execute(action_name, action_cfg)

            self.db.insert_decision(
                decision_type="rule_trigger",
                source="script_engine",
                action=hit["slug"],
                context={"result": result},
                module=hit.get("module"),
                rule_id=hit["slug"],
            )
        except Exception as exc:
            log.exception("Action dispatch failed for %s: %s", hit["slug"], exc)

    def run_brain(self) -> dict[str, Any]:
        """Run the correlation engine's weekly scan.

        Discovers patterns between module metrics and generates
        rule suggestions for strong correlations.
        """
        correlations = self.correlator.run_weekly_scan()
        top_corrs = self.correlator.get_top_correlations(limit=5)

        self.db.insert_decision(
            decision_type="brain_scan",
            source="script_engine",
            action="correlation_scan",
            context={
                "correlations_found": len(correlations),
                "top_correlations": [
                    {"pair": f"{c['metric_a']}↔{c['metric_b']}", "r": c["pearson_r"], "strength": c["strength"]}
                    for c in top_corrs
                ],
            },
            module="correlator",
        )

        return {
            "status": "ok",
            "correlations_found": len(correlations),
            "top_correlations": top_corrs,
        }

    def run_daily(self) -> dict[str, Any]:
        """Generate morning + evening briefings."""
        mod = None
        for m in self.modules:
            if isinstance(m, briefing_mod.BriefingModule):
                mod = m
                break
        if not mod:
            log.warning("No briefing module loaded")
            return {"status": "no_briefing_module"}
        morning = mod.generate_morning()
        evening = mod.generate_evening()
        return {"morning": morning, "evening": evening}

    def run_shadow(self) -> dict[str, Any]:
        """Shadow mode: dry run — modules + rules, zero external side effects."""
        log.info("Shadow tick (dry run)")
        # Save and disable side-effect paths
        _push, _push_dm = self.matrix_pusher.push, self.matrix_pusher.push_dm
        _obs_enabled = self.obsidian.enabled
        _channels_shadow = self.channels.shadow
        try:
            self.matrix_pusher.push = lambda *a, **kw: True   # swallow
            self.matrix_pusher.push_dm = lambda *a, **kw: True
            self.obsidian.enabled = False
            self.channels.shadow = True  # suppress all non-log channel output
            return self.tick()
        finally:
            self.matrix_pusher.push = _push
            self.matrix_pusher.push_dm = _push_dm
            self.obsidian.enabled = _obs_enabled
            self.channels.shadow = _channels_shadow

    # ── Phase 1.5: Focus Retention ──────────────────────────────────
    def _run_focus_retention(self) -> int:
        """Upsert focus_daily_summary then delete old focus rows (blocker 4)."""
        import sqlite3
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
        conn = sqlite3.connect(self.db.db_path)
        try:
            # Upsert aggregates for rows about to be deleted
            conn.execute("""
                INSERT INTO focus_daily_summary (date_key, state, total_secs, session_count, first_seen, last_seen)
                SELECT substr(ts, 1, 10), state,
                       COALESCE(SUM(duration_secs), 0), COUNT(*),
                       MIN(ts), MAX(ts)
                FROM focus WHERE ts < ?
                GROUP BY substr(ts, 1, 10), state
                ON CONFLICT(date_key, state) DO UPDATE SET
                    total_secs = excluded.total_secs,
                    session_count = excluded.session_count,
                    first_seen = excluded.first_seen,
                    last_seen = excluded.last_seen
            """, (cutoff,))
            deleted = conn.execute("DELETE FROM focus WHERE ts < ?", (cutoff,)).rowcount
            if deleted:
                log.info("Focus retention: upserted summaries + deleted %d rows older than %s", deleted, cutoff)
                conn.execute("PRAGMA optimize")
            return deleted
        except Exception as exc:
            log.warning("Focus retention failed: %s", exc)
            return 0
        finally:
            conn.close()

    def tick_targeted(self, module_names: list[str]) -> dict[str, Any]:
        """Run tick for specific modules only (event-driven)."""
        ts = datetime.now(timezone.utc).isoformat()
        log.info(">>> targeted tick %s: %s", ts, module_names)
        context: dict[str, Any] = {}

        target_set = set(module_names)
        for mod in self.modules:
            if mod.name not in target_set:
                last = self.db.get_latest_context(module=mod.name)
                if last:
                    context[mod.name] = last
                continue

            breaker_name = mod.name
            if not self.cb.should_attempt(breaker_name):
                context[breaker_name] = {"_status": self.cb.state(breaker_name)}
                self.health.record_skipped(breaker_name, reason=self.cb.state(breaker_name))
                continue
            try:
                result = mod._run_tick(self.db)
                self.cb.record_success(breaker_name)
                context[breaker_name] = result
                self.health.record_tick(breaker_name, result)
            except Exception as exc:
                log.exception("Targeted module %s failed: %s", breaker_name, exc)
                self.cb.record_failure(breaker_name)
                context[breaker_name] = {"_error": str(exc)}
                self.health.record_tick(breaker_name, {"_error": str(exc)}, error=exc)

        rule_hits = self.rules_engine.evaluate(context)
        
        # ── Shadow Priority Engine for targeted ticks too ──
        priority_result = None
        try:
            mode = self.cfg._data.get("priority", {}).get("mode", "shadow")
            if mode != "disabled":
                context["_module_health"] = self.health.summary()
                priority_result = self.priority.evaluate_tick(
                    context=context,
                    rule_hits=rule_hits,
                    mode=mode,
                )
        except Exception as exc:
            log.warning("Priority Engine (targeted) failed: %s", exc)

        # ── Phase 4: Soft Control — suppress duplicates/low-score non-critical ──
        suppressed_slugs: set[str] = set()
        if priority_result is not None:
            pri_cfg = self.cfg._data.get("priority", {})
            soft_enabled = pri_cfg.get("soft_control", {}).get("enabled", False)
            if soft_enabled and priority_result.mode in ("shadow", "soft_control"):
                suppressed_slugs = self.priority.get_suppressed_rule_slugs(priority_result)
                if suppressed_slugs:
                    log.info("Soft control (targeted): suppressing %d rule hits: %s", len(suppressed_slugs), suppressed_slugs)

        # ── Phase 5: Priority-Controlled Dispatch (targeted) ──────
        if priority_result is not None:
            if priority_result.mode == "priority_dispatch":
                log.info("Priority dispatch (targeted): dispatching %d selected candidates", priority_result.selected_count)
                try:
                    dispatched = self.priority_dispatcher.dispatch_result(priority_result, context)
                    if dispatched:
                        log.info("Priority dispatch (targeted): sent %d candidates", len(dispatched))
                except Exception as exc:
                    log.warning("Priority dispatch (targeted) failed: %s", exc)
            elif priority_result.mode == "shadow":
                try:
                    dispatched = self.priority_dispatcher.dispatch_result(
                        priority_result, context, shadow_mode=True
                    )
                    if dispatched:
                        log.debug("Priority shadow dispatch (targeted): tracked %d candidates", len(dispatched))
                except Exception as exc:
                    log.warning("Priority shadow dispatch (targeted) failed: %s", exc)
        else:
            if rule_hits:
                log.info("Targeted tick rule hits: %d", len(rule_hits))
                for hit in rule_hits:
                    slug = hit.get("slug", "")
                    if slug in suppressed_slugs:
                        log.info("Soft control (targeted): skipped dispatch for %s (suppressed by Priority Engine)", slug)
                        continue
                    self._dispatch_action(hit, context)

        return context

    def start_services(self) -> None:
        """Start long-lived services: collectors, mood handler, watcher.
        Only called in daemon mode — never from CLI/list/shadow commands."""
        # Collectors
        self._start_collectors()

        # Mood button handler
        from .mood_handler import MOOD_REACTION_TAG
        self._mood_handler = MoodHandler()
        self._mood_handler.start()

        # Reaction poller — polls room for emoji reactions on Helios messages
        self._reaction_poller = ReactionPoller(cfg=self.cfg._data)
        self._reaction_poller.register_handler(MOOD_REACTION_TAG, handle_mood_reaction)

        # File watcher (imported lazily to avoid watchdog dep in CLI mode)
        from .watcher import FileWatcher
        watcher_cfg = self.cfg._data.get("watcher", {})
        obsidian_cfg = self.cfg._data.get("obsidian", {})
        if watcher_cfg.get("enabled", True):
            vault = obsidian_cfg.get("vault_path", "") or self.cfg._data.get("obsidian_vault_path", "")
            self.watcher = FileWatcher(
                obsidian_vault=vault or "",
                health_data_dir=str(Path.home() / ".hermes" / "helios" / "data" / "health_data"),
                collector_data_dir=str(Path.home() / ".hermes" / "helios" / "data"),
                cooldown=watcher_cfg.get("cooldown_secs", 30),
            )
            self.watcher.start()
        else:
            self.watcher = None

    def tick_reactions(self) -> None:
        """Poll for and process Matrix reactions. Called from daemon tick."""
        if self._reaction_poller is not None:
            try:
                self._reaction_poller.tick()
            except Exception as exc:
                log.warning("Reaction poll failed: %s", exc)

    def _start_collectors(self) -> None:
        """Launch collector subprocesses so data files stay current."""
        py3 = "/usr/bin/python3"
        collector_dir = Path(__file__).resolve().parent / "collectors"

        collectors = {
            "spotify_poller": "spotify_poller.py",
            # location_sync now INLINE in modules/location.py — no subprocess
            "idle_detector": "idle_detector.py",
            "active_window_tracker": "active_window_tracker.py",
        }

        for name, script in collectors.items():
            script_path = collector_dir / script
            if not script_path.exists():
                log.warning("Collector script not found: %s", script_path)
                continue
            try:
                proc = subprocess.Popen(
                    [py3, str(script_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._collector_procs.append(proc)
                log.info("Started collector: %s (pid=%d)", name, proc.pid)
            except Exception as exc:
                log.warning("Failed to start collector %s: %s", name, exc)

    def _shutdown_collectors(self) -> None:
        """Gracefully terminate all collector subprocesses."""
        for proc in self._collector_procs:
            try:
                proc.terminate()
                log.info("Terminated collector pid=%d", proc.pid)
            except Exception as exc:
                log.warning("Error terminating collector pid=%d: %s", proc.pid, exc)
        # Wait briefly for clean exit, then force-kill stragglers
        for proc in self._collector_procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                log.warning("Collector pid=%d still alive — force killing", proc.pid)
                try:
                    proc.kill()
                except Exception:
                    pass
        self._collector_procs.clear()
        log.info("All collectors shut down")

    def _heal_collectors(self) -> None:
        """Check for heal markers and restart dead collectors. Dedup-aware."""
        data_dir = Path.home() / ".hermes" / "helios" / "data"
        py3 = "/usr/bin/python3"
        collector_dir = Path(__file__).resolve().parent / "collectors"

        # Track alive collectors by identity (script name → pid)
        alive: dict[str, list[int]] = {}
        for proc in self._collector_procs:
            if proc.poll() is None:  # still running
                cmdline = " ".join(proc.args) if hasattr(proc, 'args') else str(proc.pid)
                for script in ["spotify_poller.py", "idle_detector.py", "active_window_tracker.py"]:
                    if script in cmdline:
                        alive.setdefault(script, []).append(proc.pid)
                        break
            else:
                # Dead process — reap it
                self._collector_procs.remove(proc)

        for marker in data_dir.glob(".heal_*"):
            script_name = marker.name.replace(".heal_", "")
            
            # Skip if collector is already alive
            if script_name in alive:
                log.debug("Collector %s already running (pids=%s) — skipping restart", script_name, alive[script_name])
                try:
                    marker.unlink()
                except Exception:
                    pass
                continue

            try:
                log.info("Healing collector: %s", script_name)
                script_path = collector_dir / script_name
                if script_path.exists():
                    proc = subprocess.Popen(
                        [py3, str(script_path)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self._collector_procs.append(proc)
                    log.info("Re-launched collector: %s (pid=%d)", script_name, proc.pid)
                marker.unlink()
            except Exception as exc:
                log.warning("Heal marker failed for %s: %s", marker.name, exc)

    def _maybe_push_weekly(self) -> None:
        """Push weekly digest every Sunday, independent of dream cycle."""
        from datetime import datetime as _dt, timedelta as _td
        
        now = _dt.now()
        mdt_now = now - _td(hours=6)
        
        # Only Sunday
        if mdt_now.weekday() != 6:
            return
        
        # Only after 10 AM MDT
        if mdt_now.hour < 10:
            return
        
        # Check if already pushed today
        state_file = Path.home() / ".hermes" / "helios" / "data" / "weekly_push_state.json"
        today = mdt_now.strftime("%Y-%m-%d")
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                if state.get("date") == today:
                    return
            except Exception:
                pass
        
        try:
            # Use the narrative engine for a readable prose digest
            narrative = NarrativeEngine(self.db)
            digest = narrative.generate()
            # Phase 5: primary delivery through channel system
            self._emit_status(
                "Weekly Narrative Digest", message=digest, priority=2,
                category="narrative", source="weekly_digest",
            )
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({"date": today, "pushed": now.isoformat()}))
            log.info("Weekly narrative digest sent to Matrix")
        except Exception as exc:
            log.warning("Weekly digest push failed: %s", exc)

    def close(self) -> None:
        self._shutdown_collectors()
        if getattr(self, 'watcher', None) is not None:
            self.watcher.stop()
        if getattr(self, '_mood_handler', None) is not None:
            self._mood_handler.stop()
        self.db.close()

    def _maybe_send_mood_checkin(self) -> None:
        """Send mood check-in once per day — first tick after 7 AM MDT."""
        from datetime import datetime, timezone, timedelta
        import json, os

        now = datetime.now(timezone.utc)
        mdt_now = now - timedelta(hours=6)  # MDT = UTC-6

        # Only after 7 AM
        if mdt_now.hour < 7:
            return

        today = mdt_now.strftime("%Y-%m-%d")
        state_file = os.path.expanduser("~/.hermes/helios/data/mood_message_state.json")

        # Check if already sent today
        if os.path.exists(state_file):
            try:
                with open(state_file) as f:
                    state = json.load(f)
                if state.get("date") == today:
                    return  # Already sent today
            except Exception:
                pass

        # Send it
        try:
            send_mood_checkin(self.cfg._data, channels=self.channels)
            # Record that we sent it
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            with open(state_file, "w") as f:
                json.dump({"date": today, "sent_at": now.isoformat()}, f)
        except Exception as exc:
            log.warning("Failed to send mood check-in: %s", exc)

